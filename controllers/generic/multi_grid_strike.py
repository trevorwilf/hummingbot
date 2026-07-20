from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class GridConfig(BaseModel):
    """Configuration for an individual grid"""
    grid_id: str
    start_price: Decimal = Field(json_schema_extra={"is_updatable": True})
    end_price: Decimal = Field(json_schema_extra={"is_updatable": True})
    limit_price: Decimal = Field(json_schema_extra={"is_updatable": True})
    side: TradeType = Field(json_schema_extra={"is_updatable": True})
    amount_quote_pct: Decimal = Field(json_schema_extra={"is_updatable": True})  # Percentage of total amount (0.0 to 1.0)
    enabled: bool = Field(default=True, json_schema_extra={"is_updatable": True})

    @model_validator(mode="after")
    def validate_grid_geometry(self):
        # GEN-9: same geometry gate as GridExecutorConfig — these configs bypass the
        # orchestrator budget preflight, so config-time validation is the only gate.
        for field_name in ("start_price", "end_price", "limit_price", "amount_quote_pct"):
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
                f"BUY grid limit_price ({self.limit_price}) must be below start_price ({self.start_price})")
        if self.side == TradeType.SELL and self.limit_price <= self.end_price:
            raise ValueError(
                f"SELL grid limit_price ({self.limit_price}) must be above end_price ({self.end_price})")
        if self.amount_quote_pct <= 0:
            raise ValueError(f"amount_quote_pct must be positive, got {self.amount_quote_pct}")
        return self


class MultiGridStrikeConfig(ControllerConfigBase):
    """
    Configuration for MultiGridStrike strategy supporting multiple grids
    """
    controller_type: str = "generic"
    controller_name: str = "multi_grid_strike"

    # Account configuration
    leverage: int = 20
    position_mode: PositionMode = PositionMode.HEDGE

    # Common configuration
    connector_name: str = "binance_perpetual"
    trading_pair: str = "WLD-USDT"

    # Total capital allocation
    total_amount_quote: Decimal = Field(default=Decimal("1000"), json_schema_extra={"is_updatable": True})

    # Grid configurations
    grids: List[GridConfig] = Field(default_factory=list, json_schema_extra={"is_updatable": True})

    # Common grid parameters
    min_spread_between_orders: Optional[Decimal] = Field(default=Decimal("0.001"), json_schema_extra={"is_updatable": True})
    min_order_amount_quote: Optional[Decimal] = Field(default=Decimal("5"), json_schema_extra={"is_updatable": True})

    # Execution
    max_open_orders: int = Field(default=2, json_schema_extra={"is_updatable": True})
    max_orders_per_batch: Optional[int] = Field(default=1, json_schema_extra={"is_updatable": True})
    order_frequency: int = Field(default=3, json_schema_extra={"is_updatable": True})
    activation_bounds: Optional[Decimal] = Field(default=None, json_schema_extra={"is_updatable": True})
    keep_position: bool = Field(default=False, json_schema_extra={"is_updatable": True})

    # Risk Management
    triple_barrier_config: TripleBarrierConfig = TripleBarrierConfig(
        take_profit=Decimal("0.001"),
        open_order_type=OrderType.LIMIT_MAKER,
        take_profit_order_type=OrderType.LIMIT_MAKER,
    )

    @model_validator(mode="after")
    def validate_grid_allocation(self):
        # GEN-9: the enabled grids' allocations must not overcommit the total budget.
        enabled_pct_sum = sum((g.amount_quote_pct for g in self.grids if g.enabled), Decimal("0"))
        if enabled_pct_sum > Decimal("1"):
            raise ValueError(
                f"Sum of enabled grids' amount_quote_pct ({enabled_pct_sum}) exceeds 1")
        return self

    @model_validator(mode="after")
    def validate_grid_ids_unique(self):
        # CDX-003/CLA-015: duplicate grid_ids collapse the one-to-one
        # grid_id -> executor mapping and leave one executor unowned. Reject over
        # ALL entries, including disabled ones (a disabled duplicate re-enabled
        # later would collide at runtime).
        seen = set()
        for g in self.grids:
            if g.grid_id in seen:
                raise ValueError(f"Duplicate grid_id '{g.grid_id}': grid_ids must be unique across all entries")
            seen.add(g.grid_id)
        return self

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class MultiGridStrike(ControllerBase):
    _WARNING_INTERVAL = 30.0

    def __init__(self, config: MultiGridStrikeConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_config_hash = self._get_config_hash()
        self._grid_executor_mapping: Dict[str, str] = {}  # grid_id -> executor_id
        # GEN-8: per-grid parameter hashes so edits to a still-enabled grid are applied
        self._grid_param_hashes: Dict[str, str] = {
            g.grid_id: self._grid_param_hash(g) for g in self.config.grids
        }
        self._last_price_warning_timestamp: float = 0.0
        self.trading_rules = None
        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        self.market_data_provider.initialize_rate_sources([ConnectorPair(connector_name=self.config.connector_name,
                                                                         trading_pair=self.config.trading_pair)])

    def _get_config_hash(self) -> str:
        """Generate a hash of the current grid configurations"""
        return str(hash(tuple(
            (g.grid_id, g.start_price, g.end_price, g.limit_price, g.side, g.amount_quote_pct, g.enabled)
            for g in self.config.grids
        )))

    def _shared_param_signature(self) -> tuple:
        """CDX-M01: creation-only `is_updatable` fields shared by every grid. They
        feed GridExecutorConfig at creation, so an edit must stop/reissue the live
        executors or it silently never applies."""
        return (
            self.config.total_amount_quote,
            self.config.min_spread_between_orders,
            self.config.min_order_amount_quote,
            self.config.max_open_orders,
            self.config.max_orders_per_batch,
            self.config.order_frequency,
            self.config.activation_bounds,
            self.config.keep_position,
        )

    def _grid_param_hash(self, grid: GridConfig) -> str:
        """GEN-8/CDX-M01: hash of every parameter that requires re-issuing the
        grid's executor — per-grid geometry plus the shared creation-only fields."""
        return str((grid.start_price, grid.end_price, grid.limit_price, grid.side,
                    grid.amount_quote_pct, self._shared_param_signature()))

    @staticmethod
    def _is_valid_price(price) -> bool:
        return isinstance(price, Decimal) and price.is_finite() and price > 0

    def active_executors(self) -> List[ExecutorInfo]:
        return [
            executor for executor in self.executors_info
            if executor.is_active
        ]

    def get_executor_by_grid_id(self, grid_id: str) -> Optional[ExecutorInfo]:
        """Get the ACTIVE executor associated with a specific grid.

        GEN-6: terminated executors linger in executors_info until they fall out of
        the newest-100 archival window across ALL controllers — returning the corpse
        here blocked the grid's respawn indefinitely on quiet deployments.
        """
        executor_id = self._grid_executor_mapping.get(grid_id)
        if executor_id:
            for executor in self.executors_info:
                if executor.id == executor_id and executor.is_active:
                    return executor
        return None

    def calculate_grid_amount(self, grid: GridConfig) -> Decimal:
        """Calculate the actual amount for a grid based on its percentage allocation"""
        return self.config.total_amount_quote * grid.amount_quote_pct

    def is_inside_bounds(self, price: Decimal, grid: GridConfig) -> bool:
        """Check if price is within grid bounds"""
        # CLA-M01: an empty-book mid arrives as Decimal("NaN") and NaN Decimal
        # comparisons raise InvalidOperation — treat any invalid price as
        # out-of-bounds (fail-closed) instead of raising mid-loop.
        if not self._is_valid_price(price):
            return False
        return grid.start_price <= price <= grid.end_price

    def _reconcile_executor_ownership(self, stopped_executor_ids: set) -> List[ExecutorAction]:
        """CDX-003/CLA-015: every active executor must be owned by exactly one
        configured, enabled grid, and each grid must own at most one executor.
        Excess executors (transient duplicate creates, mapping collapse, stale
        state after a missed removed-grid pass) are stopped so nothing trades
        unowned. Idempotent — derived from live executors_info each tick."""
        actions: List[ExecutorAction] = []
        enabled_ids = {g.grid_id for g in self.config.grids if g.enabled}
        executors_by_grid: Dict[Optional[str], List[ExecutorInfo]] = {}
        for executor in self.active_executors():
            level_id = getattr(executor.config, "level_id", None)
            executors_by_grid.setdefault(level_id, []).append(executor)
        for grid_id, executors in executors_by_grid.items():
            if grid_id not in enabled_ids:
                # Unowned: the grid was removed/disabled (or the executor carries
                # no level_id) — stop it rather than let it trade unmanaged.
                for executor in executors:
                    if executor.id not in stopped_executor_ids:
                        stopped_executor_ids.add(executor.id)
                        actions.append(StopExecutorAction(
                            controller_id=self.config.id, executor_id=executor.id))
                if grid_id is not None:
                    self._grid_executor_mapping.pop(grid_id, None)
                continue
            if len(executors) > 1:
                # Duplicate executors for one grid: keep the mapped one (else the
                # oldest, deterministically) and stop the rest.
                executors_sorted = sorted(executors, key=lambda e: (e.timestamp, e.id))
                mapped_id = self._grid_executor_mapping.get(grid_id)
                keeper = next((e for e in executors_sorted if e.id == mapped_id),
                              executors_sorted[0])
                for executor in executors_sorted:
                    if executor.id != keeper.id and executor.id not in stopped_executor_ids:
                        stopped_executor_ids.add(executor.id)
                        actions.append(StopExecutorAction(
                            controller_id=self.config.id, executor_id=executor.id))
                self._grid_executor_mapping[grid_id] = keeper.id
        return actions

    def determine_executor_actions(self) -> List[ExecutorAction]:
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        # CLA-M01: bail out BEFORE any change-detection state is consumed so a bad
        # tick cannot mark a pending config change as already handled.
        if not self._is_valid_price(mid_price):
            now = self.market_data_provider.time()
            if now - self._last_price_warning_timestamp >= self._WARNING_INTERVAL:
                self._last_price_warning_timestamp = now
                self.logger().warning(
                    f"Mid price unavailable/invalid ({mid_price}) for "
                    f"{self.config.connector_name}:{self.config.trading_pair} — skipping grid actions this tick.")
            return []

        actions: List[ExecutorAction] = []
        stopped_executor_ids: set = set()
        # CLA-M01: change-detection state is committed only after the whole action
        # list is built — a mid-loop raise leaves the pending change detectable on
        # the next tick instead of silently consumed.
        new_config_hash = self._get_config_hash()
        mapping_removals: List[str] = []
        if new_config_hash != self._last_config_hash:
            # Handle removed or disabled grids
            current_grid_ids = {g.grid_id for g in self.config.grids if g.enabled}
            for grid_id, executor_id in list(self._grid_executor_mapping.items()):
                if grid_id not in current_grid_ids:
                    # GEN-6: only stop executors that are still active — sending a
                    # StopExecutorAction to a terminated executor raises upstream.
                    if self.get_executor_by_grid_id(grid_id) is not None:
                        stopped_executor_ids.add(executor_id)
                        actions.append(StopExecutorAction(
                            controller_id=self.config.id,
                            executor_id=executor_id
                        ))
                    mapping_removals.append(grid_id)

        # CDX-003: stop duplicate/unowned executors before per-grid processing.
        actions.extend(self._reconcile_executor_ownership(stopped_executor_ids))

        param_hash_updates: Dict[str, str] = {}
        # Process each enabled grid
        for grid in self.config.grids:
            if not grid.enabled:
                continue

            executor = self.get_executor_by_grid_id(grid.grid_id)

            # GEN-8: apply parameter edits to a still-enabled grid — stop its active
            # executor so the create path re-issues with the new parameters. The
            # grid stays mapped until the executor terminates, so no new executor is
            # created while the old one is still winding down.
            current_param_hash = self._grid_param_hash(grid)
            if self._grid_param_hashes.get(grid.grid_id) != current_param_hash:
                param_hash_updates[grid.grid_id] = current_param_hash
                if executor is not None:
                    if executor.id not in stopped_executor_ids:
                        stopped_executor_ids.add(executor.id)
                        actions.append(StopExecutorAction(
                            controller_id=self.config.id,
                            executor_id=executor.id
                        ))
                    continue

            # Create new executor if none exists and price is in bounds
            if executor is None and self.is_inside_bounds(mid_price, grid):
                executor_action = CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=GridExecutorConfig(
                        timestamp=self.market_data_provider.time(),
                        connector_name=self.config.connector_name,
                        trading_pair=self.config.trading_pair,
                        start_price=grid.start_price,
                        end_price=grid.end_price,
                        leverage=self.config.leverage,
                        limit_price=grid.limit_price,
                        side=grid.side,
                        total_amount_quote=self.calculate_grid_amount(grid),
                        min_spread_between_orders=self.config.min_spread_between_orders,
                        min_order_amount_quote=self.config.min_order_amount_quote,
                        max_open_orders=self.config.max_open_orders,
                        max_orders_per_batch=self.config.max_orders_per_batch,
                        order_frequency=self.config.order_frequency,
                        activation_bounds=self.config.activation_bounds,
                        triple_barrier_config=self.config.triple_barrier_config,
                        level_id=grid.grid_id,  # Use grid_id as level_id for identification
                        keep_position=self.config.keep_position,
                    ))
                actions.append(executor_action)
                # Note: We'll update the mapping after executor is created

        # CLA-M01: commit change-detection state only now that the full pass built
        # its actions without raising.
        self._last_config_hash = new_config_hash
        for grid_id in mapping_removals:
            self._grid_executor_mapping.pop(grid_id, None)
        self._grid_param_hashes.update(param_hash_updates)
        configured_ids = {g.grid_id for g in self.config.grids}
        self._grid_param_hashes = {
            grid_id: param_hash for grid_id, param_hash in self._grid_param_hashes.items()
            if grid_id in configured_ids
        }
        return actions

    async def update_processed_data(self):
        # GEN-6: prune mapping entries whose executor is done (or already archived
        # out of executors_info) so the grid can respawn.
        active_ids = {executor.id for executor in self.active_executors()}
        for grid_id, executor_id in list(self._grid_executor_mapping.items()):
            if executor_id not in active_ids:
                del self._grid_executor_mapping[grid_id]
        # Update executor mapping for newly created executors
        for executor in self.active_executors():
            if hasattr(executor.config, 'level_id') and executor.config.level_id:
                self._grid_executor_mapping[executor.config.level_id] = executor.id

    def to_format_status(self) -> List[str]:
        status = []
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)

        # Define standard box width for consistency
        box_width = 114

        # Top Multi-Grid Configuration box
        status.append("┌" + "─" * box_width + "┐")

        # Header
        header = f"│ Multi-Grid Configuration - {self.config.connector_name} {self.config.trading_pair}"
        header += " " * (box_width - len(header) + 1) + "│"
        status.append(header)

        # Mid price, grid count, and total amount
        active_grids = len([g for g in self.config.grids if g.enabled])
        total_grids = len(self.config.grids)
        total_amount = self.config.total_amount_quote
        info_line = f"│ Mid Price: {mid_price:.4f} │ Active Grids: {active_grids}/{total_grids} │ Total Amount: {total_amount:.2f} │"
        info_line += " " * (box_width - len(info_line) + 1) + "│"
        status.append(info_line)

        status.append("└" + "─" * box_width + "┘")

        # Display each grid configuration
        for grid in self.config.grids:
            if not grid.enabled:
                continue

            executor = self.get_executor_by_grid_id(grid.grid_id)
            in_bounds = self.is_inside_bounds(mid_price, grid)

            # Grid header
            grid_status = "ACTIVE" if executor else ("READY" if in_bounds else "OUT_OF_BOUNDS")
            status_header = f"Grid {grid.grid_id}: {grid_status}"
            status_line = f"┌ {status_header}" + "─" * (box_width - len(status_header) - 2) + "┐"
            status.append(status_line)

            # Grid configuration
            grid_amount = self.calculate_grid_amount(grid)
            pct_display = f"{grid.amount_quote_pct * 100:.1f}%"
            config_line = f"│ Start: {grid.start_price:.4f} │ End: {grid.end_price:.4f} │ Side: {grid.side} │ Limit: {grid.limit_price:.4f} │ Amount: {grid_amount:.2f} ({pct_display}) │"
            config_line += " " * (box_width - len(config_line) + 1) + "│"
            status.append(config_line)

            if executor:
                # Display executor statistics
                col_width = box_width // 3

                # Column headers
                header_line = "│ Level Distribution" + " " * (col_width - 20) + "│"
                header_line += " Order Statistics" + " " * (col_width - 18) + "│"
                header_line += " Performance Metrics" + " " * (col_width - 21) + "│"
                status.append(header_line)

                # Data columns
                level_dist_data = [
                    f"NOT_ACTIVE: {len(executor.custom_info.get('levels_by_state', {}).get('NOT_ACTIVE', []))}",
                    f"OPEN_ORDER_PLACED: {len(executor.custom_info.get('levels_by_state', {}).get('OPEN_ORDER_PLACED', []))}",
                    f"OPEN_ORDER_FILLED: {len(executor.custom_info.get('levels_by_state', {}).get('OPEN_ORDER_FILLED', []))}",
                    f"CLOSE_ORDER_PLACED: {len(executor.custom_info.get('levels_by_state', {}).get('CLOSE_ORDER_PLACED', []))}",
                    f"COMPLETE: {len(executor.custom_info.get('levels_by_state', {}).get('COMPLETE', []))}"
                ]

                order_stats_data = [
                    f"Total: {sum(len(executor.custom_info.get(k, [])) for k in ['filled_orders', 'failed_orders', 'canceled_orders'])}",
                    f"Filled: {len(executor.custom_info.get('filled_orders', []))}",
                    f"Failed: {len(executor.custom_info.get('failed_orders', []))}",
                    f"Canceled: {len(executor.custom_info.get('canceled_orders', []))}"
                ]

                perf_metrics_data = [
                    f"Buy Vol: {executor.custom_info.get('realized_buy_size_quote', 0):.4f}",
                    f"Sell Vol: {executor.custom_info.get('realized_sell_size_quote', 0):.4f}",
                    f"R. PnL: {executor.custom_info.get('realized_pnl_quote', 0):.4f}",
                    f"R. Fees: {executor.custom_info.get('realized_fees_quote', 0):.4f}",
                    f"P. PnL: {executor.custom_info.get('position_pnl_quote', 0):.4f}",
                    f"Position: {executor.custom_info.get('position_size_quote', 0):.4f}"
                ]

                # Build rows
                max_rows = max(len(level_dist_data), len(order_stats_data), len(perf_metrics_data))
                for i in range(max_rows):
                    col1 = level_dist_data[i] if i < len(level_dist_data) else ""
                    col2 = order_stats_data[i] if i < len(order_stats_data) else ""
                    col3 = perf_metrics_data[i] if i < len(perf_metrics_data) else ""

                    row = "│ " + col1
                    row += " " * (col_width - len(col1) - 2)
                    row += "│ " + col2
                    row += " " * (col_width - len(col2) - 2)
                    row += "│ " + col3
                    row += " " * (col_width - len(col3) - 2)
                    row += "│"
                    status.append(row)

                # Liquidity line
                status.append("├" + "─" * box_width + "┤")
                liquidity_line = f"│ Open Liquidity: {executor.custom_info.get('open_liquidity_placed', 0):.4f} │ Close Liquidity: {executor.custom_info.get('close_liquidity_placed', 0):.4f} │"
                liquidity_line += " " * (box_width - len(liquidity_line) + 1) + "│"
                status.append(liquidity_line)

            status.append("└" + "─" * box_width + "┘")

        return status
