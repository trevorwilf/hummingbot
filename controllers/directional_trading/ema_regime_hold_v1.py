# filename: controllers/directional_trading/ema_regime_hold_v1.py
# EMA regime-hold directional controller for NonKYC spot.
# Runtime config: conf/controllers/nonkyc_xmr_usdt_ema_regime_hold_v1.yml
# Loader:         conf/scripts/conf_v2_with_controllers_nonkyc_xmr_usdt_ema.yml

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from controllers._shared.candle_freshness import DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, get_freshness_gate
from controllers._shared.trade_ledger import TradeLedger
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.utils.ta_utils import adx_wilder, ema, rolling_volume_quantile_ok


class EMARegimeHoldV1Config(DirectionalTradingControllerConfigBase):
    controller_name: str = "ema_regime_hold_v1"

    candles_connector: str = Field(default=None)
    candles_trading_pair: str = Field(default=None)

    signal_interval: str = Field(default="5m")
    regime_interval: str = Field(default="4h")

    regime_ema_fast: int = Field(default=50, gt=5)
    regime_ema_slow: int = Field(default=200, gt=10)
    regime_adx_length: int = Field(default=14, gt=2)
    regime_adx_threshold: float = Field(default=20.0, ge=0.0)

    volume_filter_window: int = Field(default=288, ge=0)
    min_volume_quantile: float = Field(default=0.30, ge=0.0, le=1.0)

    # CDX-001 / CLA-405: interval-relative max age for the newest candle (checked
    # per feed against its own interval) before the signal is gated to 0.
    # Per-market tunable; sparse pairs legitimately have old closed bars, so keep
    # this generous. 0 disables the gate (legacy behavior).
    stale_candle_max_age_intervals: float = Field(
        default=DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, gt=0,
        json_schema_extra={"is_updatable": True})

    hold_mode: str = Field(
        default="reentry",
        description="'reentry' (Option A): re-enter on every eligible bar when flat; TP/SL/time_limit handle exits. "
                    "'hold'    (Option B): enter once, hold until trend_on flips False, then force-close. "
                    "For 'hold' mode set take_profit and time_limit to \"\" in YAML so the triple barrier "
                    "does not fight the regime-hold intent.",
    )

    @field_validator("hold_mode", mode="before")
    @classmethod
    def validate_hold_mode(cls, v):
        if v is None:
            return "reentry"
        v_norm = str(v).strip().lower()
        if v_norm not in ("reentry", "hold"):
            raise ValueError(f"hold_mode must be 'reentry' or 'hold', got {v!r}")
        return v_norm

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return validation_info.data.get("trading_pair")
        return v

    @model_validator(mode="after")
    def _normalize_omitted_candle_aliases(self):
        # CLA-001: pydantic v2 does not run mode="before" field validators on
        # OMITTED fields (the base omits validate_default), so a config that never
        # mentions candles_connector/candles_trading_pair reached the controller
        # with the raw None default. Normalize post-construction so the aliases
        # always hold real values. object.__setattr__ avoids the validate_assignment
        # re-entry of a plain assignment.
        if self.candles_connector is None or str(self.candles_connector).strip() == "":
            object.__setattr__(self, "candles_connector", self.connector_name)
        if self.candles_trading_pair is None or str(self.candles_trading_pair).strip() == "":
            object.__setattr__(self, "candles_trading_pair", self.trading_pair)
        return self

    @property
    def candles_config(self) -> List[CandlesConfig]:
        return [
            CandlesConfig(connector=self.candles_connector, trading_pair=self.candles_trading_pair, interval=self.signal_interval, max_records=6000),
            CandlesConfig(connector=self.candles_connector, trading_pair=self.candles_trading_pair, interval=self.regime_interval, max_records=3000),
        ]


class EMARegimeHoldV1(DirectionalTradingControllerBase):
    def __init__(self, config: EMARegimeHoldV1Config, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)
        # CDX-011 / CLA-305: persisted, controller-owned fill ledger. The
        # same-side cooldown used to be derived ONLY from executors_info, a
        # bot-wide archival buffer that a co-deployed churning controller can
        # evict in minutes and that starts empty on restart — silently dropping
        # the cooldown. The ledger provides a durable last-fill floor.
        self._trade_ledger = TradeLedger(
            ledger_id=f"{config.controller_name}_{config.id}",
            logger=self.logger(),
        )

    def _observe_fills(self):
        """Feed the persisted trade ledger from the current executor snapshot.

        Runs every tick so fills are captured while their executors are still
        in the buffer (before bot-wide archival eviction can drop them).
        TradeLedger.observe_executors is idempotent and never raises.
        """
        try:
            now = self.market_data_provider.time()
        except Exception:
            return
        self._trade_ledger.observe_executors(self.executors_info, now)

    def get_candles_config(self) -> List[CandlesConfig]:
        return self.config.candles_config

    def _drop_incomplete_last_bar(self, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """
        Drop the last bar only when it's still forming. On sparse feeds
        (e.g., NonKYC at 4h on a low-volume pair), the newest available bar
        may already be closed even when no newer bar exists yet; keep it.
        """
        if len(df) <= 2:
            return df
        interval_seconds = CandlesBase.interval_to_seconds.get(interval)
        if interval_seconds is None:
            return df.iloc[:-1].copy()
        last_ts = float(df["timestamp"].iloc[-1])
        now = self.market_data_provider.time()
        if last_ts + interval_seconds > now:
            return df.iloc[:-1].copy()
        return df

    async def update_processed_data(self):
        # CDX-011 / CLA-305: capture fills into the persisted ledger every tick,
        # independent of whether a signal is produced this cycle.
        self._observe_fills()
        df_fast = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.signal_interval,
            max_records=6000,
        )
        df_slow = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.regime_interval,
            max_records=3000,
        )

        if df_fast is None or df_fast.empty or df_slow is None or df_slow.empty:
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}
            self._emit_decision_trace(None)
            return

        # CDX-001 / CLA-405: a frozen-but-full feed keeps replaying its last bar
        # as a live signal. Gate BOTH feeds on interval-relative bar age and fail
        # closed to signal=0. The bound is generous and per-market configurable
        # so sparse pairs with legitimately old closed bars are not hard-failed.
        gate = get_freshness_gate(self)
        now = self.market_data_provider.time()
        fast_fresh = gate.check(
            df=df_fast, interval=self.config.signal_interval, now=now,
            max_age_intervals=self.config.stale_candle_max_age_intervals)
        slow_fresh = gate.check(
            df=df_slow, interval=self.config.regime_interval, now=now,
            max_age_intervals=self.config.stale_candle_max_age_intervals)
        if not (fast_fresh.fresh and slow_fresh.fresh):
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}
            self._emit_decision_trace(None)
            return

        df_fast = df_fast.copy().sort_values("timestamp")
        df_slow = df_slow.copy().sort_values("timestamp")

        df_fast = self._drop_incomplete_last_bar(df_fast, self.config.signal_interval)
        df_slow = self._drop_incomplete_last_bar(df_slow, self.config.regime_interval)

        for col in ["open", "high", "low", "close", "volume"]:
            df_fast[col] = pd.to_numeric(df_fast[col], errors="coerce")
            df_slow[col] = pd.to_numeric(df_slow[col], errors="coerce")
        df_fast.dropna(subset=["open", "high", "low", "close", "volume", "timestamp"], inplace=True)
        df_slow.dropna(subset=["open", "high", "low", "close", "volume", "timestamp"], inplace=True)

        close_s = df_slow["close"]
        high_s = df_slow["high"]
        low_s = df_slow["low"]

        df_slow["ema_fast"] = ema(close_s, self.config.regime_ema_fast)
        df_slow["ema_slow"] = ema(close_s, self.config.regime_ema_slow)
        df_slow["adx"] = adx_wilder(high_s, low_s, close_s, self.config.regime_adx_length)
        df_slow.dropna(inplace=True)

        df_slow["trend_on"] = (df_slow["ema_fast"] >= df_slow["ema_slow"]) & (df_slow["adx"] >= self.config.regime_adx_threshold)
        slow_ind = df_slow[["timestamp", "trend_on"]].copy()

        fast = df_fast[["timestamp", "volume"]].copy()
        merged = pd.merge_asof(fast.sort_values("timestamp"), slow_ind.sort_values("timestamp"), on="timestamp", direction="backward")
        merged.index = df_fast.index

        trend = merged["trend_on"].fillna(False)
        vol_ok = rolling_volume_quantile_ok(
            df_fast["volume"], self.config.volume_filter_window, self.config.min_volume_quantile
        )
        # Eligibility is stateful: true whenever we WANT a long position, regardless
        # of whether we have one yet.
        eligible = (trend & vol_ok).astype(bool)

        merged["trend_on_bool"] = trend.astype(bool).values
        merged["vol_ok"] = vol_ok.astype(bool).values
        merged["eligible"] = eligible.astype(int).values
        merged["signal"] = eligible.astype(int).values  # stateful long signal
        merged["regime"] = np.where(trend, "trend", "off")

        self.processed_data["signal"] = int(merged["signal"].iloc[-1]) if len(merged) else 0
        self.processed_data["features"] = merged
        self._emit_decision_trace(merged)

    def _emit_decision_trace(self, merged) -> None:
        if merged is None or len(merged) == 0:
            self.logger().info(
                f"EMA tick: features_empty=true signal=0 hold_mode={self.config.hold_mode}"
            )
            return
        last = merged.iloc[-1]
        ts = last.get("timestamp")
        bar_age_s = None
        try:
            bar_age_s = self.market_data_provider.time() - float(ts)
        except Exception:
            pass
        bar_age_str = f"{bar_age_s:.1f}" if bar_age_s is not None else "na"
        self.logger().info(
            f"EMA tick: signal={int(last.get('signal', 0))} "
            f"hold_mode={self.config.hold_mode} "
            f"trend_on={bool(last.get('trend_on_bool', False))} "
            f"vol_ok={bool(last.get('vol_ok', False))} "
            f"eligible={int(last.get('eligible', 0))} "
            f"bar_ts={ts} bar_age_s={bar_age_str} "
            f"rows={len(merged)}"
        )

    def _log_gate_reason(self, signal: int, gate: str, ok: bool, **extra) -> None:
        side = "BUY" if signal > 0 else "SELL"
        if ok:
            self.logger().debug(f"EMA gate OK [{side}]: {gate}")
        else:
            extras = " ".join(f"{k}={v}" for k, v in extra.items())
            self.logger().debug(f"EMA gate BLOCKED [{side}]: {gate} {extras}")

    def _last_same_side_reference_ts(self, signal: int) -> float:
        """
        Timestamp of the most recent same-side executor event, preferring
        close_timestamp for closed executors and falling back to timestamp
        for still-active ones. Returns 0.0 if no same-side history exists.

        Note (CDX-011 / CLA-305): executors_info is a bot-wide rolling buffer
        (closed_executors_buffer, default 30 in v2_with_controllers.py) that a
        co-deployed churning controller can evict in minutes and that starts
        empty on restart. It is therefore only ONE input to the cooldown gate:
        can_create_executor takes the max of this value and the persisted trade
        ledger's last same-side fill, so the cooldown survives eviction and
        restarts without weakening the creation-armed component below.
        """
        target_side = TradeType.BUY if signal > 0 else TradeType.SELL
        relevant = [e for e in self.executors_info if e.side == target_side]
        if not relevant:
            return 0.0
        return max(
            (e.close_timestamp if e.close_timestamp is not None else e.timestamp)
            for e in relevant
        )

    def can_create_executor(self, signal: int) -> bool:
        # super() correctly gates max_executors_per_side; its cooldown clause is vacuous
        # after executor close, so we re-enforce with close_timestamp.
        if not super().can_create_executor(signal):
            self._log_gate_reason(signal, gate="super", ok=False)
            return False
        now = self.market_data_provider.time()
        # CDX-011 / CLA-305: make sure any fill visible this tick is recorded
        # before the cooldown below reads the ledger.
        self._trade_ledger.observe_executors(self.executors_info, now)
        target_side = TradeType.BUY if signal > 0 else TradeType.SELL
        # Cooldown: max of the buffer-derived reference (keeps the existing
        # creation-armed behavior for still-buffered executors — never weaker
        # than before) and the persisted ledger's last same-side fill (which
        # survives buffer eviction and bot restarts).
        last_ts = max(
            self._last_same_side_reference_ts(signal),
            self._trade_ledger.last_fill_timestamp(side=target_side),
        )
        cooldown_ok = (last_ts == 0.0) or (now - last_ts > self.config.cooldown_time)
        if not cooldown_ok:
            self._log_gate_reason(
                signal, gate="cooldown", ok=False,
                last_ts=last_ts, remaining=self.config.cooldown_time - (now - last_ts),
            )
            return False
        self._log_gate_reason(signal, gate="all", ok=True)
        return True

    def stop_actions_proposal(self) -> List[ExecutorAction]:
        if self.config.hold_mode != "hold":
            return []

        features = self.processed_data.get("features")
        if features is None or len(features) == 0:
            return []
        last_trend_on = (
            bool(features["trend_on_bool"].iloc[-1])
            if "trend_on_bool" in features.columns
            else False
        )
        if last_trend_on:
            return []

        target_side = TradeType.BUY  # EMA is long-only by design
        actions: List[ExecutorAction] = []
        for ex in self.executors_info:
            if not ex.is_active:
                continue
            if ex.side != target_side:
                continue
            if ex.status == RunnableStatus.SHUTTING_DOWN:
                continue
            actions.append(
                StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=ex.id,
                    keep_position=False,
                )
            )
        if actions:
            self.logger().info(
                f"EMA hold mode: regime turned off — issuing early_stop for "
                f"{len(actions)} active executor(s)."
            )
        return actions
