from typing import List

import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from controllers._shared.candle_freshness import DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, get_freshness_gate
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)


class BollingerV1ControllerConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "bollinger_v1"
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ",
            "prompt_on_new": True})
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ",
            "prompt_on_new": True})
    interval: str = Field(
        default="5m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True})
    # CLA-014: non-positive indicator periods crash/NaN pandas_ta every tick.
    bb_length: int = Field(
        default=100, gt=0,
        json_schema_extra={"prompt": "Enter the Bollinger Bands length: ", "prompt_on_new": True})
    bb_std: float = Field(default=2.0, gt=0)
    bb_long_threshold: float = Field(default=0.0)
    bb_short_threshold: float = Field(default=1.0)
    # CDX-001 / CLA-405: interval-relative max age for the newest candle before the
    # signal is gated to 0. Per-market tunable; sparse pairs legitimately have old
    # closed bars, so keep this generous. 0 disables the gate (legacy behavior).
    stale_candle_max_age_intervals: float = Field(
        default=DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, gt=0,
        json_schema_extra={"is_updatable": True})

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v


class BollingerV1Controller(DirectionalTradingControllerBase):
    def __init__(self, config: BollingerV1ControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = self.config.bb_length + 20
        super().__init__(config, *args, **kwargs)

    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(
            connector=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )]

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                      trading_pair=self.config.candles_trading_pair,
                                                      interval=self.config.interval,
                                                      max_records=self.max_records)
        # CDX-001 / CLA-405: candle `ready` is length-only, so a frozen-but-full
        # deque keeps replaying its last bar as a live signal. Fail closed to
        # signal=0 when the newest bar is older than the configured bound.
        freshness = get_freshness_gate(self).check(
            df=df, interval=self.config.interval, now=self.market_data_provider.time(),
            max_age_intervals=self.config.stale_candle_max_age_intervals)
        if not freshness.fresh:
            self.processed_data["signal"] = 0
            self.processed_data["features"] = df if df is not None else pd.DataFrame()
            return
        # Add indicators
        df.ta.bbands(length=self.config.bb_length, lower_std=self.config.bb_std, upper_std=self.config.bb_std, append=True)
        bbp = df[f"BBP_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"]

        # Generate signal
        long_condition = bbp < self.config.bb_long_threshold
        short_condition = bbp > self.config.bb_short_threshold

        # Generate signal
        df["signal"] = 0
        df.loc[long_condition, "signal"] = 1
        df.loc[short_condition, "signal"] = -1

        # Update processed data
        self.processed_data["signal"] = df["signal"].iloc[-1]
        self.processed_data["features"] = df
