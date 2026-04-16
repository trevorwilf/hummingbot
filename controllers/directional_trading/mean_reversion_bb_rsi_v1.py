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

from hummingbot.core.data_type.common import PriceType
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
    def candles_config(self) -> List[CandlesConfig]:
        max_records = max(self.bb_length, self.trend_ema_length, self.rsi_length, self.atr_length) + 400
        return [
            CandlesConfig(
                connector=self.candles_connector,
                trading_pair=self.candles_trading_pair,
                interval=self.interval,
                max_records=max_records,
            )
        ]


class MeanReversionBBRSIV1(DirectionalTradingControllerBase):
    def __init__(self, config: MeanReversionBBRSIV1Config, *args, **kwargs):
        self.config = config
        self.max_records = max(config.bb_length, config.trend_ema_length, config.rsi_length, config.atr_length) + 500
        super().__init__(config, *args, **kwargs)

    def get_candles_config(self) -> List[CandlesConfig]:
        return self.config.candles_config

    def _safe_spread_ok(self) -> bool:
        if self.config.max_spread_pct <= 0:
            return True
        try:
            bid = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.BestBid)
            ask = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.BestAsk)
            bid_f = float(bid)
            ask_f = float(ask)
            mid = (bid_f + ask_f) / 2.0
            if mid <= 0:
                return False
            spread_pct = (ask_f - bid_f) / mid
            return spread_pct <= float(self.config.max_spread_pct)
        except Exception:
            return False

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records,
        )

        if df is None or df.empty:
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}
            return

        df = df.copy().sort_values("timestamp")

        if len(df) > 2:
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

    def can_create_executor(self, signal: int) -> bool:
        if not super().can_create_executor(signal):
            return False

        if not self._safe_spread_ok():
            return False

        if self.config.max_trades_per_day and self.config.max_trades_per_day > 0:
            now = self.market_data_provider.time()
            cutoff = now - 86400
            recent = [e for e in self.executors_info if getattr(e, "timestamp", 0) >= cutoff]
            if len(recent) >= self.config.max_trades_per_day:
                return False

        return True
