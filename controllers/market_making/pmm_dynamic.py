import math
from decimal import Decimal
from typing import List

import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from controllers._shared.candle_freshness import DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, get_freshness_gate
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig


class PMMDynamicControllerConfig(MarketMakingControllerConfigBase):
    controller_name: str = "pmm_dynamic"
    buy_spreads: List[float] = Field(
        default="1,2,4",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy spreads measured in units of volatility(e.g., '1, 2'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_spreads: List[float] = Field(
        default="1,2,4",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell spreads measured in units of volatility(e.g., '1, 2'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
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
        default="3m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True})
    # CLA-014: non-positive / mis-ordered indicator periods crash or NaN the
    # indicators every tick.
    macd_fast: int = Field(
        default=21, gt=0,
        json_schema_extra={"prompt": "Enter the MACD fast period: ", "prompt_on_new": True})
    macd_slow: int = Field(
        default=42, gt=0,
        json_schema_extra={"prompt": "Enter the MACD slow period: ", "prompt_on_new": True})
    macd_signal: int = Field(
        default=9, gt=0,
        json_schema_extra={"prompt": "Enter the MACD signal period: ", "prompt_on_new": True})
    natr_length: int = Field(
        default=14, gt=0,
        json_schema_extra={"prompt": "Enter the NATR length: ", "prompt_on_new": True})
    # CDX-001 / CLA-406: interval-relative max age for the newest candle before
    # the controller stops publishing a reference price (nothing is quoted while
    # data is stale). Per-market tunable; 0 disables (legacy behavior).
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

    @model_validator(mode="after")
    def validate_macd_ordering(self):
        # CLA-014: some pandas_ta versions silently swap fast/slow instead of
        # raising, so enforce the ordering at config time.
        if self.macd_fast >= self.macd_slow:
            raise ValueError(
                f"macd_fast ({self.macd_fast}) must be strictly less than macd_slow ({self.macd_slow})")
        return self


class PMMDynamicController(MarketMakingControllerBase):
    """
    This is a dynamic version of the PMM controller.It uses the MACD to shift the mid-price and the NATR
    to make the spreads dynamic. It also uses the Triple Barrier Strategy to manage the risk.
    """

    def __init__(self, config: PMMDynamicControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = max(config.macd_slow, config.macd_fast, config.macd_signal, config.natr_length) + 100
        super().__init__(config, *args, **kwargs)
        self._degenerate_indicator_warning_ts: float = 0.0

    def _warn_degenerate_indicators(self, reasons: List[str], action: str):
        now = self.market_data_provider.time()
        if now - self._degenerate_indicator_warning_ts >= 30.0:
            self._degenerate_indicator_warning_ts = now
            self.logger().warning(
                f"Degenerate indicators for {self.config.candles_trading_pair} "
                f"({', '.join(reasons)}) — {action}. "
                f"Rate-limited to one warning per 30s.")

    def _publish_data_unavailable(self, candles):
        # CLA-406: do NOT retain a previously published reference/spread — quoting
        # must not continue on an obsolete market view. The market-making base
        # treats a missing reference_price as nothing-to-quote.
        self.processed_data = {
            "features": candles if candles is not None else pd.DataFrame()
        }

    async def update_processed_data(self):
        candles = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                           trading_pair=self.config.candles_trading_pair,
                                                           interval=self.config.interval,
                                                           max_records=self.max_records)
        # CDX-001 / CLA-406: a frozen-but-full candle feed kept the last reference
        # price/spread live indefinitely (quoting crossing LIMITs as the market
        # moved away). Gate on interval-relative bar age and stop quoting on stale
        # data instead of reusing an obsolete reference.
        freshness = get_freshness_gate(self).check(
            df=candles, interval=self.config.interval, now=self.market_data_provider.time(),
            max_age_intervals=self.config.stale_candle_max_age_intervals)
        if not freshness.fresh:
            self._publish_data_unavailable(candles)
            return
        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100
        macd_output = ta.macd(candles["close"], fast=self.config.macd_fast,
                              slow=self.config.macd_slow, signal=self.config.macd_signal)
        macd = macd_output[f"MACD_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"]

        # Flat closes on an illiquid pair make macd.std() zero (or NaN on degenerate
        # data) — the original math then poisons reference_price/spread_multiplier
        # with NaN/inf. The MACD price shift degrades to 0 (quote around mid); a
        # degenerate NATR pauses quoting entirely (see below).
        degenerate_reasons = []
        macd_std = float(macd.std())
        if not math.isfinite(macd_std) or macd_std == 0:
            degenerate_reasons.append(f"macd_std={macd_std}")
            price_multiplier = 0.0
        else:
            macd_signal = - (macd - macd.mean()) / macd_std
            macdh = macd_output[f"MACDh_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"]
            macdh_signal = macdh.apply(lambda x: 1 if x > 0 else -1)
            max_price_shift = natr / 2
            price_multiplier = float(((0.5 * macd_signal + 0.5 * macdh_signal) * max_price_shift).iloc[-1])
            if not math.isfinite(price_multiplier):
                degenerate_reasons.append(f"price_multiplier={price_multiplier}")
                price_multiplier = 0.0

        latest_natr = float(natr.iloc[-1])
        if not math.isfinite(latest_natr) or latest_natr <= 0:
            # CLA-016: buy/sell spreads are configured in UNITS OF VOLATILITY, so
            # the old fallback of spread_multiplier=1 turned "1,2,4" into
            # 100/200/400% spreads while the log claimed "plain mid quoting".
            # With no usable NATR there is no spread unit — pause quoting instead
            # of inventing one.
            degenerate_reasons.append(f"natr={latest_natr}")
            self._warn_degenerate_indicators(
                degenerate_reasons,
                action="no usable NATR spread unit; pausing quoting until volatility data recovers")
            self._publish_data_unavailable(candles)
            return
        spread_multiplier = Decimal(str(latest_natr))

        reference_price = float(candles["close"].iloc[-1]) * (1 + price_multiplier)
        if not math.isfinite(reference_price) or reference_price <= 0:
            # CLA-406: no usable close — clear (not retain) any previous reference
            # so proposals pause rather than quoting an obsolete price.
            degenerate_reasons.append(f"reference_price={reference_price}")
            self._warn_degenerate_indicators(
                degenerate_reasons, action="no usable reference price; pausing quoting")
            self._publish_data_unavailable(candles)
            return
        if degenerate_reasons:
            self._warn_degenerate_indicators(
                degenerate_reasons,
                action="MACD price shift disabled; quoting around mid with NATR spreads")

        candles["spread_multiplier"] = natr
        candles["reference_price"] = candles["close"] * (1 + price_multiplier)
        self.processed_data = {
            "reference_price": Decimal(str(reference_price)),
            "spread_multiplier": spread_multiplier,
            "features": candles
        }

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )

    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(
            connector=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )]
