from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executors import TrackedOrder


class GridExecutorConfig(ExecutorConfigBase):
    type: Literal["grid_executor"] = "grid_executor"
    # Boundaries
    connector_name: str
    trading_pair: str
    start_price: Decimal
    end_price: Decimal
    limit_price: Decimal
    side: TradeType = TradeType.BUY
    # Profiling
    total_amount_quote: Decimal
    min_spread_between_orders: Decimal = Decimal("0.0005")
    min_order_amount_quote: Decimal = Decimal("5")
    # Execution
    max_open_orders: int = 5
    max_orders_per_batch: Optional[int] = None
    order_frequency: int = 0
    activation_bounds: Optional[Decimal] = None
    safe_extra_spread: Decimal = Decimal("0.0001")
    # Risk Management
    triple_barrier_config: TripleBarrierConfig
    leverage: int = 1
    level_id: Optional[str] = None
    deduct_base_fees: bool = False
    keep_position: bool = False
    coerce_tp_to_step: bool = False

    @model_validator(mode="after")
    def validate_grid_geometry(self):
        # GEN-9: grid configs bypass the orchestrator budget preflight (no `.amount`
        # attribute), so this validator is the only gate against degenerate geometry:
        # start=0 divides by zero at level construction, an inverted range produces a
        # silent single-level "grid" with negative step, and a wrong-side limit_price
        # triggers an instant limit-breach stop on every creation.
        for field_name in ("start_price", "end_price", "limit_price"):
            value = getattr(self, field_name)
            if not value.is_finite():
                raise ValueError(f"{field_name} must be a finite number, got {value}")
        if self.start_price <= 0:
            raise ValueError(f"start_price must be positive, got {self.start_price}")
        if self.start_price >= self.end_price:
            raise ValueError(
                f"start_price ({self.start_price}) must be below end_price ({self.end_price})")
        if self.side == TradeType.BUY and self.limit_price >= self.start_price:
            raise ValueError(
                f"BUY grid limit_price ({self.limit_price}) must be below start_price "
                f"({self.start_price}) or the limit breach triggers immediately")
        if self.side == TradeType.SELL and self.limit_price <= self.end_price:
            raise ValueError(
                f"SELL grid limit_price ({self.limit_price}) must be above end_price "
                f"({self.end_price}) or the limit breach triggers immediately")
        return self


class GridLevelStates(Enum):
    NOT_ACTIVE = "NOT_ACTIVE"
    OPEN_ORDER_PLACED = "OPEN_ORDER_PLACED"
    OPEN_ORDER_FILLED = "OPEN_ORDER_FILLED"
    CLOSE_ORDER_PLACED = "CLOSE_ORDER_PLACED"
    COMPLETE = "COMPLETE"


class GridLevel(BaseModel):
    id: str
    price: Decimal
    amount_quote: Decimal
    take_profit: Decimal
    side: TradeType
    open_order_type: OrderType
    take_profit_order_type: OrderType
    active_open_order: Optional[TrackedOrder] = None
    active_close_order: Optional[TrackedOrder] = None
    state: GridLevelStates = GridLevelStates.NOT_ACTIVE
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def update_state(self):
        if self.active_open_order is None:
            self.state = GridLevelStates.NOT_ACTIVE
        elif self.active_open_order.is_filled:
            self.state = GridLevelStates.OPEN_ORDER_FILLED
        else:
            self.state = GridLevelStates.OPEN_ORDER_PLACED
        if self.active_close_order is not None:
            if self.active_close_order.is_filled:
                self.state = GridLevelStates.COMPLETE
            else:
                self.state = GridLevelStates.CLOSE_ORDER_PLACED

    def reset_open_order(self):
        self.active_open_order = None
        self.state = GridLevelStates.NOT_ACTIVE

    def reset_close_order(self):
        self.active_close_order = None
        self.state = GridLevelStates.OPEN_ORDER_FILLED

    def reset_level(self):
        self.active_open_order = None
        self.active_close_order = None
        self.state = GridLevelStates.NOT_ACTIVE
