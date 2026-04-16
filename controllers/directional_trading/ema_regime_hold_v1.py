# filename: controllers/directional_trading/ema_regime_hold_v1.py
# EMA regime-hold directional controller for NonKYC spot.
# Runtime config: conf/controllers/nonkyc_xmr_usdt_ema_regime_hold_v1.yml
# Loader:         conf/scripts/conf_v2_with_controllers_nonkyc_xmr_usdt_ema.yml

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
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
        return [
            CandlesConfig(connector=self.candles_connector, trading_pair=self.candles_trading_pair, interval=self.signal_interval, max_records=6000),
            CandlesConfig(connector=self.candles_connector, trading_pair=self.candles_trading_pair, interval=self.regime_interval, max_records=3000),
        ]


class EMARegimeHoldV1(DirectionalTradingControllerBase):
    def __init__(self, config: EMARegimeHoldV1Config, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)

    def get_candles_config(self) -> List[CandlesConfig]:
        return self.config.candles_config

    async def update_processed_data(self):
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
            return

        df_fast = df_fast.copy().sort_values("timestamp")
        df_slow = df_slow.copy().sort_values("timestamp")

        if len(df_fast) > 2:
            df_fast = df_fast.iloc[:-1].copy()
        if len(df_slow) > 2:
            df_slow = df_slow.iloc[:-1].copy()

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
        merged["signal"] = ((~trend.shift(1).fillna(False)) & trend).astype(int)

        vol_ok = rolling_volume_quantile_ok(df_fast["volume"], self.config.volume_filter_window, self.config.min_volume_quantile)
        merged["signal"] = (merged["signal"] & vol_ok).astype(int)
        merged["regime"] = np.where(trend, "trend", "off")

        self.processed_data["signal"] = int(merged["signal"].iloc[-1]) if len(merged) else 0
        self.processed_data["features"] = merged
