import math
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from controllers._shared.candle_freshness import DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, get_freshness_gate
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop


class DManV3ControllerConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "dman_v3"
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
    # signal is gated to 0. Per-market tunable; 0 disables (legacy behavior).
    stale_candle_max_age_intervals: float = Field(
        default=DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS, ge=0,
        json_schema_extra={"is_updatable": True})
    # CLA-001: typed default. The old default was the raw string "0.015,0.005";
    # pydantic v2 does not run mode="before" validators on omitted fields, so an
    # omitted trailing_stop reached the executor config unparsed and threw on the
    # first signal.
    trailing_stop: Optional[TrailingStop] = Field(
        default_factory=lambda: TrailingStop(
            activation_price=Decimal("0.015"), trailing_delta=Decimal("0.005")),
        json_schema_extra={
            "prompt": "Enter the trailing stop parameters (activation_price, trailing_delta) as a comma-separated list: ",
            "prompt_on_new": True,
        }
    )
    # CLA-001: typed default for the same reason (the old comma-string default
    # reached len()/iteration unparsed when the field was omitted).
    dca_spreads: List[Decimal] = Field(
        default_factory=lambda: [Decimal("0.001"), Decimal("0.018"), Decimal("0.15"), Decimal("0.25")],
        json_schema_extra={
            "prompt": "Enter the spreads for each DCA level (comma-separated) if dynamic_spread=True this value "
                      "will multiply BBB/200 (half the BB width fraction), e.g. if the Bollinger Bands width is "
                      "10 (10%) and the spread is 0.2, the distance of the order to the current price will be "
                      "0.01 (1%) ",
            "prompt_on_new": True},
    )
    min_spread_multiplier: Decimal = Field(
        default=Decimal("0.01"), gt=0,
        json_schema_extra={
            "prompt": "Enter the floor for the dynamic spread multiplier (e.g., 0.01): "},
    )
    dca_amounts_pct: List[Decimal] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the amounts for each DCA level (as a percentage of the total balance, "
                      "comma-separated). Don't worry about the final sum, it will be normalized. ",
            "prompt_on_new": True},
    )
    dynamic_order_spread: bool = Field(
        default=None,
        json_schema_extra={"prompt": "Do you want to make the spread dynamic? (Yes/No) ", "prompt_on_new": True})
    dynamic_target: bool = Field(
        default=None,
        json_schema_extra={"prompt": "Do you want to make the target dynamic? (Yes/No) ", "prompt_on_new": True})
    activation_bounds: Optional[List[Decimal]] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the activation bounds for the orders (e.g., 0.01 activates the next order when the price is closer than 1%): ",
            "prompt_on_new": True,
        }
    )

    @field_validator("activation_bounds", mode="before")
    @classmethod
    def parse_activation_bounds(cls, v):
        if isinstance(v, str):
            if v == "":
                return None
            return [Decimal(val) for val in v.split(",")]
        if isinstance(v, list):
            return [Decimal(val) for val in v]
        return v

    @field_validator('dca_spreads', mode="before")
    @classmethod
    def validate_spreads(cls, v):
        # CDX-010 / CLA-010: validate after every parse form — nonempty and
        # per-element positive, with parse failures surfaced as clean ValueErrors.
        if isinstance(v, str):
            if v.strip() == "":
                raise ValueError("dca_spreads must not be empty")
            try:
                v = [Decimal(val.strip()) for val in v.split(",")]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_spreads contains a non-numeric entry: {v!r}")
        if isinstance(v, list):
            if len(v) == 0:
                raise ValueError("dca_spreads must not be empty")
            try:
                spreads = [Decimal(str(val)) for val in v]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_spreads contains a non-numeric entry: {v!r}")
            if any(spread <= 0 for spread in spreads):
                raise ValueError("All DCA spreads must be positive")
            return spreads
        return v

    @field_validator('dca_amounts_pct', mode="before")
    @classmethod
    def validate_amounts(cls, v, validation_info: ValidationInfo):
        # CDX-010 / CLA-010: .get() so a failed dca_spreads surfaces its own error
        # instead of a masking KeyError; per-element positive (the old sum-only
        # check let individual NEGATIVE weights through -> negative order amounts).
        spreads = validation_info.data.get("dca_spreads")
        if isinstance(v, str):
            if v.strip() == "":
                v = None
            else:
                try:
                    v = [Decimal(val.strip()) for val in v.split(",")]
                except (InvalidOperation, ValueError):
                    raise ValueError(f"dca_amounts_pct contains a non-numeric entry: {v!r}")
        if v is None:
            if not spreads:
                # dca_spreads failed its own validation; let its error surface.
                return None
            return [Decimal('1.0') / len(spreads) for _ in spreads]
        if isinstance(v, list):
            try:
                amounts = [Decimal(str(val)) for val in v]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_amounts_pct contains a non-numeric entry: {v!r}")
            if len(amounts) == 0:
                raise ValueError("dca_amounts_pct must not be empty")
            if spreads is not None and len(amounts) != len(spreads):
                raise ValueError("Amounts and spreads must have the same length")
            if any(amount <= 0 for amount in amounts):
                raise ValueError("All DCA amounts must be positive")
            return amounts
        return v

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

    def get_spreads_and_amounts_in_quote(self, trade_type: TradeType, total_amount_quote: Decimal) -> Tuple[List[Decimal], List[Decimal]]:
        amounts_pct = self.dca_amounts_pct
        if amounts_pct is None:
            # Equally distribute if amounts_pct is not set
            spreads = self.dca_spreads
            normalized_amounts_pct = [Decimal('1.0') / len(spreads) for _ in spreads]
        else:
            if trade_type == TradeType.BUY:
                normalized_amounts_pct = [amt_pct / sum(amounts_pct) for amt_pct in amounts_pct]
            else:  # TradeType.SELL
                normalized_amounts_pct = [amt_pct / sum(amounts_pct) for amt_pct in amounts_pct]

        return self.dca_spreads, [amt_pct * total_amount_quote for amt_pct in normalized_amounts_pct]


class DManV3Controller(DirectionalTradingControllerBase):
    """
    Mean reversion strategy with Grid execution making use of Bollinger Bands indicator to make spreads dynamic
    and shift the mid-price.
    """

    def __init__(self, config: DManV3ControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = config.bb_length + 20
        super().__init__(config, *args, **kwargs)

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                      trading_pair=self.config.candles_trading_pair,
                                                      interval=self.config.interval,
                                                      max_records=self.max_records)
        # CDX-001 / CLA-405: fail closed to signal=0 on stale/absent candles.
        freshness = get_freshness_gate(self).check(
            df=df, interval=self.config.interval, now=self.market_data_provider.time(),
            max_age_intervals=self.config.stale_candle_max_age_intervals)
        if not freshness.fresh:
            self.processed_data["signal"] = 0
            self.processed_data["features"] = df if df is not None else pd.DataFrame()
            return
        # Add indicators
        df.ta.bbands(length=self.config.bb_length, lower_std=self.config.bb_std, upper_std=self.config.bb_std, append=True)

        # Generate signal
        long_condition = df[f"BBP_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"] < self.config.bb_long_threshold
        short_condition = df[f"BBP_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"] > self.config.bb_short_threshold

        # Generate signal
        df["signal"] = 0
        df.loc[long_condition, "signal"] = 1
        df.loc[short_condition, "signal"] = -1

        # Update processed data
        self.processed_data["signal"] = df["signal"].iloc[-1]
        self.processed_data["features"] = df

    def _latest_bb_width(self) -> Optional[float]:
        df = self.processed_data.get("features")
        if df is None or len(df) == 0:
            return None
        column = f"BBB_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"
        if column not in df.columns:
            return None
        return float(df[column].iloc[-1])

    def _bb_width_ok(self) -> bool:
        bb_width = self._latest_bb_width()
        return bb_width is not None and math.isfinite(bb_width) and bb_width > 0

    def can_create_executor(self, signal: int) -> bool:
        dynamic = self.config.dynamic_order_spread or self.config.dynamic_target
        if dynamic and not self._bb_width_ok():
            self.logger().warning("Skipping executor creation: Bollinger band width is zero/NaN "
                                  "(degenerate flat window) and dynamic spread/target is enabled.")
            return False
        return super().can_create_executor(signal)

    def _dynamic_multiplier(self) -> Decimal:
        """
        BBB / 200, i.e. half the BB width expressed as a fraction (BBB is a
        percentage), floored at config.min_spread_multiplier so a flat window can
        never collapse spreads, stop-loss or trailing stop to zero.
        """
        bb_width = self._latest_bb_width()
        if bb_width is None or not math.isfinite(bb_width) or bb_width <= 0:
            return self.config.min_spread_multiplier
        return max(Decimal(str(bb_width)) / Decimal("200"), self.config.min_spread_multiplier)

    def get_spread_multiplier(self) -> Decimal:
        if self.config.dynamic_order_spread:
            return self._dynamic_multiplier()
        else:
            return Decimal("1.0")

    def get_executor_config(self, trade_type: TradeType, price: Decimal, amount: Decimal) -> DCAExecutorConfig:
        spread, amounts_quote = self.config.get_spreads_and_amounts_in_quote(trade_type, amount * price)
        spread_multiplier = self.get_spread_multiplier()
        if trade_type == TradeType.BUY:
            prices = [price * (1 - spread * spread_multiplier) for spread in spread]
        else:
            prices = [price * (1 + spread * spread_multiplier) for spread in spread]
        if self.config.dynamic_target:
            # CLA-2b-006: use the dynamic multiplier directly. Scaling by
            # get_spread_multiplier() made dynamic_target a silent no-op
            # (multiplier fixed at 1) whenever dynamic_order_spread was off.
            target_multiplier = self._dynamic_multiplier()
            stop_loss = self.config.stop_loss * target_multiplier if self.config.stop_loss is not None else None
            take_profit = self.config.take_profit * target_multiplier if self.config.take_profit is not None else None
            if self.config.trailing_stop:
                trailing_stop = TrailingStop(
                    activation_price=self.config.trailing_stop.activation_price * target_multiplier,
                    trailing_delta=self.config.trailing_stop.trailing_delta * target_multiplier)
            else:
                trailing_stop = None
        else:
            stop_loss = self.config.stop_loss
            take_profit = self.config.take_profit
            trailing_stop = self.config.trailing_stop
        return DCAExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=trade_type,
            mode=DCAMode.MAKER,
            prices=prices,
            amounts_quote=amounts_quote,
            time_limit=self.config.time_limit,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing_stop,
            leverage=self.config.leverage,
            activation_bounds=self.config.activation_bounds,
        )

    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(
            connector=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )]
