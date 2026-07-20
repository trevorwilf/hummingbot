from decimal import Decimal, InvalidOperation
from typing import List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction, StopExecutorAction


class DManMakerV2Config(MarketMakingControllerConfigBase):
    """
    Configuration required to run the D-Man Maker V2 strategy.
    """
    controller_name: str = "dman_maker_v2"

    # DCA configuration
    # NOTE: dca_spreads/dca_amounts are consumed once in the controller's __init__ and
    # are NOT hot-reloadable — YAML edits to them require a controller restart.
    # CLA-001: typed defaults. The old comma-string defaults bypassed the parse
    # validators (pydantic v2 does not validate omitted defaults), so an omitted
    # field reached the controller as a raw string.
    dca_spreads: List[Decimal] = Field(
        default_factory=lambda: [Decimal("0.01"), Decimal("0.02"), Decimal("0.04"), Decimal("0.08")],
        json_schema_extra={"prompt": "Enter a comma-separated list of spreads for each DCA level "
                                     "(not hot-reloadable, requires restart): ", "prompt_on_new": True})
    dca_amounts: List[Decimal] = Field(
        default_factory=lambda: [Decimal("0.1"), Decimal("0.2"), Decimal("0.4"), Decimal("0.8")],
        json_schema_extra={"prompt": "Enter a comma-separated list of amounts for each DCA level "
                                     "(not hot-reloadable, requires restart): ", "prompt_on_new": True})
    top_executor_refresh_time: Optional[float] = Field(default=None, json_schema_extra={"is_updatable": True})
    executor_activation_bounds: Optional[List[Decimal]] = Field(default=None, json_schema_extra={"is_updatable": True})

    @field_validator("executor_activation_bounds", mode="before")
    @classmethod
    def parse_activation_bounds(cls, v):
        if isinstance(v, list):
            return [Decimal(val) for val in v]
        elif isinstance(v, str):
            if v == "":
                return None
            return [Decimal(val) for val in v.split(",")]
        return v

    @field_validator('dca_spreads', mode="before")
    @classmethod
    def parse_dca_spreads(cls, v):
        # CDX-010 / CLA-010: '' / None used to parse to [] and quietly produce an
        # empty ladder; validate nonempty + per-element positive after every parse
        # form.
        if v is None or (isinstance(v, str) and v.strip() == ""):
            raise ValueError("dca_spreads must not be empty")
        if isinstance(v, str):
            try:
                v = [Decimal(x.strip()) for x in v.split(',')]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_spreads contains a non-numeric entry: {v!r}")
        if isinstance(v, list):
            if len(v) == 0:
                raise ValueError("dca_spreads must not be empty")
            try:
                spreads = [Decimal(str(x)) for x in v]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_spreads contains a non-numeric entry: {v!r}")
            if any(spread <= 0 for spread in spreads):
                raise ValueError("All DCA spreads must be positive")
            return spreads
        return v

    @field_validator('dca_amounts', mode="before")
    @classmethod
    def parse_and_validate_dca_amounts(cls, v, validation_info):
        # CDX-010 / CLA-010: .get() so a failed dca_spreads surfaces its own error
        # instead of a masking KeyError here.
        spreads = validation_info.data.get('dca_spreads')
        if v is None or (isinstance(v, str) and v.strip() == ""):
            if not spreads:
                # dca_spreads failed its own validation; let its error surface.
                return []
            return [Decimal("1") for _ in spreads]
        if isinstance(v, str):
            try:
                v = [Decimal(x.strip()) for x in v.split(',')]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_amounts contains a non-numeric entry: {v!r}")
        if isinstance(v, list):
            try:
                amounts = [Decimal(str(x)) for x in v]
            except (InvalidOperation, ValueError):
                raise ValueError(f"dca_amounts contains a non-numeric entry: {v!r}")
            if len(amounts) == 0:
                raise ValueError("dca_amounts must not be empty")
            # CDX-010: length check applied AFTER all parse forms — the old `elif`
            # skipped it for the common comma-string path, silently zip-truncating
            # DCA levels in get_executor_config.
            if spreads is not None and len(amounts) != len(spreads):
                raise ValueError(
                    f"The number of dca_amounts ({len(amounts)}) must match the number of "
                    f"dca_spreads ({len(spreads)}).")
            if any(amount <= 0 for amount in amounts):
                raise ValueError("All DCA amounts must be positive")
            return amounts
        return v


class DManMakerV2(MarketMakingControllerBase):
    def __init__(self, config: DManMakerV2Config, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        # The validator forbids a zero sum from YAML; guard the division anyway for
        # programmatic construction — fall back to equal weights instead of raising.
        total_dca_amount = sum(self.config.dca_amounts)
        if total_dca_amount > 0:
            self.dca_amounts_pct = [Decimal(amount) / total_dca_amount for amount in self.config.dca_amounts]
        else:
            level_count = len(self.config.dca_amounts)
            if level_count > 0:
                self.logger().warning("dca_amounts sum to zero — falling back to equal DCA weights.")
            self.dca_amounts_pct = [Decimal("1") / level_count for _ in range(level_count)]
        self.spreads = self.config.dca_spreads

    def first_level_refresh_condition(self, executor):
        if self.config.top_executor_refresh_time is not None:
            if self.get_level_from_level_id(executor.custom_info["level_id"]) == 0:
                return self.market_data_provider.time() - executor.timestamp > self.config.top_executor_refresh_time
        return False

    def order_level_refresh_condition(self, executor):
        return self.market_data_provider.time() - executor.timestamp > self.config.executor_refresh_time

    def executors_to_refresh(self) -> List[ExecutorAction]:
        executors_to_refresh = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: not x.is_trading and x.is_active and (self.order_level_refresh_condition(x) or self.first_level_refresh_condition(x)))
        return [StopExecutorAction(
            controller_id=self.config.id,
            executor_id=executor.id) for executor in executors_to_refresh]

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        if trade_type == TradeType.BUY:
            prices = [price * (1 - spread) for spread in self.spreads]
        else:
            prices = [price * (1 + spread) for spread in self.spreads]
        amounts = [amount * pct for pct in self.dca_amounts_pct]
        amounts_quote = [amount * price for amount, price in zip(amounts, prices)]
        return DCAExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            mode=DCAMode.MAKER,
            side=trade_type,
            prices=prices,
            amounts_quote=amounts_quote,
            level_id=level_id,
            time_limit=self.config.time_limit,
            stop_loss=self.config.stop_loss,
            take_profit=self.config.take_profit,
            trailing_stop=self.config.trailing_stop,
            activation_bounds=self.config.executor_activation_bounds,
            leverage=self.config.leverage,
        )
