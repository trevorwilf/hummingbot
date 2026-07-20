# filename: controllers/directional_trading/mean_reversion_bb_rsi_v1.py
# Mean-reversion BB/RSI directional controller for NonKYC spot.
# Runtime config: conf/controllers/nonkyc_xmr_usdt_mean_reversion_bb_rsi_v1.yml
# Loader:         conf/scripts/conf_v2_with_controllers_nonkyc_xmr_usdt_mr.yml

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from controllers._shared.trade_ledger import TradeLedger
from hummingbot.core.data_type.common import PriceType, TradeType
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.utils.ta_utils import (
    atr_wilder,
    bollinger_percent_b,
    ema,
    rolling_volume_quantile_ok,
    rsi_wilder,
)


class MeanReversionBBRSIV1Config(DirectionalTradingControllerConfigBase):
    controller_name: str = "mean_reversion_bb_rsi_v1"

    candles_connector: str = Field(default=None)
    candles_trading_pair: str = Field(default=None)
    interval: str = Field(default="5m")

    bb_length: int = Field(default=80, gt=10)
    bb_std: float = Field(default=2.0, gt=0.1)
    bbp_entry_threshold: float = Field(default=0.20, ge=0.0, le=1.0)

    rsi_length: int = Field(default=14, gt=2)
    rsi_entry_threshold: float = Field(default=40.0, ge=0.0, le=100.0)

    use_trend_filter: bool = Field(default=True)
    trend_ema_length: int = Field(default=200, gt=20)
    min_trend_slope: float = Field(default=0.0)

    atr_length: int = Field(default=14, gt=2)
    max_atr_pct_for_entry: float = Field(default=0.10, gt=0.0)

    volume_filter_window: int = Field(default=288, ge=0)
    min_volume_quantile: float = Field(default=0.30, ge=0.0, le=1.0)

    max_spread_pct: float = Field(default=0.006, ge=0.0)
    max_trades_per_day: int = Field(default=6, ge=0)

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

    @property
    def required_records(self) -> int:
        return max(
            self.bb_length,
            self.trend_ema_length,
            self.rsi_length,
            self.atr_length,
        ) + 500

    @property
    def candles_config(self) -> List[CandlesConfig]:
        return [
            CandlesConfig(
                connector=self.candles_connector,
                trading_pair=self.candles_trading_pair,
                interval=self.interval,
                max_records=self.required_records,
            )
        ]


class MeanReversionBBRSIV1(DirectionalTradingControllerBase):
    def __init__(self, config: MeanReversionBBRSIV1Config, *args, **kwargs):
        self.config = config
        self.max_records = config.required_records
        super().__init__(config, *args, **kwargs)
        # CDX-011 / CLA-305: persisted, controller-owned fill ledger. The daily
        # trade cap counts this ledger (not the transient bot-wide executors_info
        # buffer, which evicts under co-deployed churn and resets on restart).
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

    def _safe_spread_ok(self) -> bool:
        """
        Return True iff (ask-bid)/mid is within the configured max. Logs the
        reason when gating fails so operators can distinguish a wide-spread
        reject from a missing-data reject.
        """
        if self.config.max_spread_pct <= 0:
            return True
        try:
            bid = self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.BestBid)
            ask = self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.BestAsk)
            bid_f = float(bid)
            ask_f = float(ask)
        except Exception as e:
            self.logger().warning(f"MR spread gate: could not read bid/ask for "
                                  f"{self.config.connector_name}:{self.config.trading_pair}: {e}")
            return False

        if not (bid_f > 0 and ask_f > 0):
            self.logger().warning(f"MR spread gate: non-positive bid/ask "
                                  f"(bid={bid_f}, ask={ask_f}) — treating as gate=fail")
            return False

        mid = (bid_f + ask_f) / 2.0
        if mid <= 0:
            return False
        spread_pct = (ask_f - bid_f) / mid
        if spread_pct > float(self.config.max_spread_pct):
            return False
        return True

    async def update_processed_data(self):
        # CDX-011 / CLA-305: capture fills into the persisted ledger every tick,
        # independent of whether a signal is produced this cycle.
        self._observe_fills()
        df = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records,
        )

        if df is None or df.empty:
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}
            self._emit_decision_trace(None)
            return

        df = df.copy().sort_values("timestamp")

        # Only drop the last bar if it's still forming. On sparse NonKYC feeds,
        # the newest available bar may already be closed even when no newer bar
        # has appeared yet; in that case we must KEEP it.
        if len(df) > 2:
            interval_seconds = CandlesBase.interval_to_seconds.get(self.config.interval)
            if interval_seconds is None:
                df = df.iloc[:-1].copy()
            else:
                last_ts = float(df["timestamp"].iloc[-1])
                now = self.market_data_provider.time()
                if last_ts + interval_seconds > now:
                    df = df.iloc[:-1].copy()

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.dropna(subset=["open", "high", "low", "close", "volume", "timestamp"], inplace=True)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        vol = df["volume"]

        bbp, bb_upper, bb_mid, bb_lower = bollinger_percent_b(close, self.config.bb_length, self.config.bb_std)
        df["bbp"] = bbp
        df["bb_upper"] = bb_upper
        df["bb_mid"] = bb_mid
        df["bb_lower"] = bb_lower

        df["rsi"] = rsi_wilder(close, self.config.rsi_length)

        atr = atr_wilder(high, low, close, self.config.atr_length)
        df["atr"] = atr
        df["atr_pct"] = df["atr"] / df["close"].replace(0, np.nan)

        df["ema_trend"] = ema(close, self.config.trend_ema_length)
        df["ema_slope"] = df["ema_trend"].diff()

        df["volume_ok"] = rolling_volume_quantile_ok(vol, self.config.volume_filter_window, self.config.min_volume_quantile)

        entry = (
            (df["bbp"] <= self.config.bbp_entry_threshold)
            & (df["rsi"] <= self.config.rsi_entry_threshold)
            & (df["atr_pct"] <= self.config.max_atr_pct_for_entry)
            & (df["volume_ok"])
        )

        if self.config.use_trend_filter:
            entry = entry & (df["ema_slope"] >= self.config.min_trend_slope)

        df["signal"] = 0
        df.loc[entry, "signal"] = 1

        self.processed_data["signal"] = int(df["signal"].iloc[-1]) if len(df) else 0
        self.processed_data["features"] = df
        self._emit_decision_trace(df)

    def _emit_decision_trace(self, df) -> None:
        """
        Emit one INFO-level structured summary per controller update. Designed so
        operators can diagnose live signal behavior from plain logs without DEBUG.
        """
        if df is None or len(df) == 0:
            self.logger().info("MR tick: features_empty=true signal=0")
            return
        last = df.iloc[-1]
        ts = last.get("timestamp")
        bar_age_s = None
        try:
            bar_age_s = self.market_data_provider.time() - float(ts)
        except Exception:
            pass

        def _f(x):
            try:
                fx = float(x)
                return f"{fx:.4f}"
            except Exception:
                return "nan"

        bar_age_str = f"{bar_age_s:.1f}" if bar_age_s is not None else "na"
        self.logger().info(
            f"MR tick: signal={int(last.get('signal', 0))} "
            f"bar_ts={ts} bar_age_s={bar_age_str} "
            f"bbp={_f(last.get('bbp'))} rsi={_f(last.get('rsi'))} "
            f"atr_pct={_f(last.get('atr_pct'))} ema_slope={_f(last.get('ema_slope'))} "
            f"volume_ok={bool(last.get('volume_ok', False))} "
            f"rows={len(df)}"
        )

    def _log_gate_reason(self, signal: int, gate: str, ok: bool, **extra) -> None:
        side = "BUY" if signal > 0 else "SELL"
        if ok:
            self.logger().debug(f"MR gate OK [{side}]: {gate}")
        else:
            extras = " ".join(f"{k}={v}" for k, v in extra.items())
            self.logger().debug(f"MR gate BLOCKED [{side}]: {gate} {extras}")

    def _last_same_side_reference_ts(self, signal: int) -> float:
        """
        Return the timestamp of the most recent same-side executor event,
        preferring close_timestamp for closed executors and falling back
        to timestamp (creation) for executors that are still active.
        Returns 0.0 if no same-side executor is present in executors_info.

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
        return max((e.close_timestamp if e.close_timestamp is not None else e.timestamp)
                   for e in relevant)

    def can_create_executor(self, signal: int) -> bool:
        # The base class correctly enforces max_executors_per_side via
        # active_executors_condition, but its cooldown check collapses to
        # "now - 0 > cooldown_time" once no executors are active. We honor
        # the max-executors gate and impose our own cooldown on top.
        if not super().can_create_executor(signal):
            self._log_gate_reason(signal, gate="super", ok=False)
            return False

        now = self.market_data_provider.time()
        # CDX-011 / CLA-305: make sure any fill visible this tick is recorded
        # before the gates below read the ledger.
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
            self._log_gate_reason(signal, gate="cooldown", ok=False,
                                  last_ts=last_ts,
                                  remaining=self.config.cooldown_time - (now - last_ts))
            return False

        if not self._safe_spread_ok():
            self._log_gate_reason(signal, gate="spread", ok=False)
            return False

        if self.config.max_trades_per_day and self.config.max_trades_per_day > 0:
            # CDX-011 / CLA-305: the daily cap counts actually-FILLED trades from
            # the persisted ledger. The old executors_info count both reset on
            # restart / co-deployed churn (fail-open) and included never-filled
            # executors (over-restrictive).
            cutoff = now - 86400
            recent_fills = self._trade_ledger.count_fills_since(cutoff)
            if recent_fills >= self.config.max_trades_per_day:
                self._log_gate_reason(signal, gate="daily_cap", ok=False,
                                      count=recent_fills, cap=self.config.max_trades_per_day)
                return False

        self._log_gate_reason(signal, gate="all", ok=True)
        return True
