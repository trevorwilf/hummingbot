from decimal import Decimal
from typing import List

from pydantic import Field, model_validator

from hummingbot.core.data_type.common import PositionMode
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig


class PMMSimpleConfig(MarketMakingControllerConfigBase):
    controller_name: str = "pmm_simple"

    # CLA-001: the base class declares these with STRING defaults ("0.01,0.02" /
    # "HEDGE") relying on mode="before" validators that never run for omitted fields
    # (validate_default is not set on the base model). Re-declare them controller-side
    # with real typed defaults so a default-constructed config is already normalized —
    # the inherited validators still parse user-provided string input.
    buy_spreads: List[float] = Field(
        default_factory=lambda: [0.01, 0.02],
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy spreads (e.g., '0.01, 0.02'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_spreads: List[float] = Field(
        default_factory=lambda: [0.01, 0.02],
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell spreads (e.g., '0.01, 0.02'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    position_mode: PositionMode = Field(
        default=PositionMode.HEDGE,
        json_schema_extra={"prompt": "Enter the position mode (HEDGE/ONEWAY): "}
    )

    @model_validator(mode="after")
    def normalize_omitted_amounts_pct(self):
        # CLA-001: buy/sell_amounts_pct default to None ("distribute equally") but the
        # base parse validator does not run for omitted fields, so None would reach
        # sum() in get_spreads_and_amounts_in_quote and raise TypeError. Model
        # validators DO run on default construction — normalize here.
        if not self.buy_amounts_pct:
            self.buy_amounts_pct = [Decimal("1") for _ in self.buy_spreads]
        if not self.sell_amounts_pct:
            self.sell_amounts_pct = [Decimal("1") for _ in self.sell_spreads]
        return self


class PMMSimpleController(MarketMakingControllerBase):
    def __init__(self, config: PMMSimpleConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

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
