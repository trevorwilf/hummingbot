from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Set, Union

import pandas_ta as ta  # noqa: F401
from pydantic import Field, ValidationError, field_validator, model_validator

from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class QGAConfig(ControllerConfigBase):
    controller_name: str = "quantum_grid_allocator"

    # Portfolio allocation zones
    long_only_threshold: Decimal = Field(default=Decimal("0.2"), json_schema_extra={"is_updatable": True})
    short_only_threshold: Decimal = Field(default=Decimal("0.2"), json_schema_extra={"is_updatable": True})
    # CLA-011: the dead `hedge_ratio` field was removed — nothing ever read it and a
    # configured value silently did nothing. CDX-R04: a config still carrying it is
    # rejected with a targeted migration error rather than silently discarded — an
    # operator tuning the knob deserves an error, not a continued no-op.

    # Grid allocation multipliers
    base_grid_value_pct: Decimal = Field(default=Decimal("0.08"), json_schema_extra={"is_updatable": True})
    max_grid_value_pct: Decimal = Field(default=Decimal("0.15"), json_schema_extra={"is_updatable": True})

    # Order frequency settings
    safe_extra_spread: Decimal = Field(default=Decimal("0.0001"), json_schema_extra={"is_updatable": True})
    favorable_order_frequency: int = Field(default=2, json_schema_extra={"is_updatable": True})
    unfavorable_order_frequency: int = Field(default=5, json_schema_extra={"is_updatable": True})
    max_orders_per_batch: int = Field(default=1, json_schema_extra={"is_updatable": True})

    # Portfolio allocation
    portfolio_allocation: Dict[str, Decimal] = Field(
        default={
            "SOL": Decimal("0.50"),  # 50%
        },
        json_schema_extra={"is_updatable": True})
    # Grid parameters
    grid_range: Decimal = Field(default=Decimal("0.002"), json_schema_extra={"is_updatable": True})
    tp_sl_ratio: Decimal = Field(default=Decimal("0.8"), json_schema_extra={"is_updatable": True})
    min_order_amount: Decimal = Field(default=Decimal("5"), json_schema_extra={"is_updatable": True})
    # Risk parameters
    max_deviation: Decimal = Field(default=Decimal("0.05"), json_schema_extra={"is_updatable": True})
    max_open_orders: int = Field(default=2, json_schema_extra={"is_updatable": True})
    # Exchange settings
    connector_name: str = "binance"
    leverage: int = 1
    position_mode: PositionMode = PositionMode.HEDGE
    quote_asset: str = "USDT"
    fee_asset: str = "BNB"
    # Grid price multipliers
    min_spread_between_orders: Decimal = Field(
        default=Decimal("0.0001"),  # 0.01% between orders
        json_schema_extra={"is_updatable": True})
    grid_tp_multiplier: Decimal = Field(
        default=Decimal("0.0001"),  # 0.2% take profit
        json_schema_extra={"is_updatable": True})
    # Grid safety parameters
    limit_price_spread: Decimal = Field(
        default=Decimal("0.001"),  # 0.1% spread for limit price
        json_schema_extra={"is_updatable": True})
    activation_bounds: Decimal = Field(
        default=Decimal("0.0002"),  # Activation bounds for orders
        json_schema_extra={"is_updatable": True})
    bb_length: int = 100
    bb_std_dev: float = 2.0
    interval: str = "1s"
    dynamic_grid_range: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    show_terminated_details: bool = False
    # CLA-308 (enhancement, DEFAULT OFF): optional consecutive-stop-out breaker.
    # 0 disables it entirely — recentering behavior is byte-for-byte unchanged.
    # When set to N > 0, after N consecutive stop-outs on a pair (grid terminated by
    # a limit-price breach: CloseType.STOP_LOSS, or POSITION_HOLD since QGA grids run
    # keep_position=True) no new grid is created for that pair until
    # stop_out_breaker_cooldown seconds have passed since the last stop-out.
    stop_out_breaker_count: int = Field(default=0, ge=0, json_schema_extra={"is_updatable": True})
    stop_out_breaker_cooldown: float = Field(default=300.0, ge=0, json_schema_extra={"is_updatable": True})
    # CLA-411/CDX-R02: a zero read for an asset never yet seen nonzero (cold start
    # OR the first ticks after a restart — the in-process nonzero history does not
    # survive a restart) is trusted only after it has persisted this many seconds.
    # A transient post-restart zero recovers within a tick or two and never gets
    # trusted; a genuinely flat all-quote start bootstraps after the window.
    # 0 trusts first-read zeros immediately (unsafe legacy opt-out).
    zero_balance_confirmation_seconds: float = Field(default=60.0, ge=0, json_schema_extra={"is_updatable": True})

    @property
    def quote_asset_allocation(self) -> Decimal:
        """Calculate the implicit quote asset (USDT) allocation"""
        return Decimal("1") - sum(self.portfolio_allocation.values())

    @model_validator(mode="before")
    @classmethod
    def reject_removed_hedge_ratio(cls, values):
        # CLA-011/CDX-R04: fail loudly with a migration hint (clearer than the
        # generic extra_forbidden error the base class would raise).
        if isinstance(values, dict) and "hedge_ratio" in values:
            raise ValueError(
                "QGA config field 'hedge_ratio' was removed — it was never read and had "
                "no effect on behavior. Delete it from the controller config.")
        return values

    @field_validator("portfolio_allocation")
    @classmethod
    def validate_allocation(cls, v):
        # CLA-011: every allocation must be a positive finite fraction — a zero or
        # negative allocation makes the theoretical value 0 and silently disables the
        # deviation math for that asset; NaN would poison the totals.
        for asset, allocation in v.items():
            if not allocation.is_finite() or allocation <= 0:
                raise ValueError(f"Allocation for {asset} must be a positive finite number, got {allocation}")
        total = sum(v.values())
        if total >= Decimal("1"):
            raise ValueError(f"Total allocation {total} exceeds or equals 100%. Must leave room for USDT allocation.")
        if "USDT" in v:
            raise ValueError("USDT should not be explicitly allocated as it is the quote asset")
        return v

    @model_validator(mode="after")
    def validate_grid_geometry_params(self):
        # GEN-9: the grid geometry is derived at runtime as
        #   start/end = mid * (1 ± grid_range * tp/sl multipliers)
        #   limit     = start/end * (1 ∓ limit_price_spread)
        # These bounds guarantee start < end and a correct-side limit whenever the
        # mid price is valid (NaN/zero mids are rejected at executor construction).
        if not self.grid_range.is_finite() or self.grid_range <= 0:
            raise ValueError(f"grid_range must be a positive finite number, got {self.grid_range}")
        if not self.tp_sl_ratio.is_finite() or not (Decimal("0") < self.tp_sl_ratio < Decimal("1")):
            raise ValueError(f"tp_sl_ratio must be strictly between 0 and 1, got {self.tp_sl_ratio}")
        if not self.limit_price_spread.is_finite() or self.limit_price_spread <= 0:
            raise ValueError(f"limit_price_spread must be a positive finite number, got {self.limit_price_spread}")
        return self

    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        if self.connector_name not in markets:
            markets[self.connector_name] = set()
        for asset in self.portfolio_allocation:
            markets[self.connector_name].add(f"{asset}-{self.quote_asset}")
        return markets


class QuantumGridAllocator(ControllerBase):
    _WARNING_INTERVAL = 30.0

    def __init__(self, config: QGAConfig, *args, **kwargs):
        self.config = config
        self.metrics = {}
        # GEN-9: rate-limit state for the invalid-geometry warning
        self._last_invalid_geometry_warning_timestamp: float = 0.0
        # CLA-411: assets whose balance has been observed nonzero at least once. A
        # later zero/absent read for such an asset is treated as a transient bad read
        # (fail-closed skip), while a genuinely-flat cold start stays tradeable.
        self._assets_seen_nonzero: Set[str] = set()
        # CDX-R02: first timestamp a valid zero was read for a never-seen-nonzero
        # asset — a zero must persist for zero_balance_confirmation_seconds before
        # it is trusted as a genuine flat position.
        self._zero_reads_since: Dict[str, float] = {}
        self._suspect_balance_assets: Set[str] = set()
        self._last_suspect_balance_warning_timestamp: float = 0.0
        # CLA-308: consecutive-stop-out breaker state (tracked even while the breaker
        # is disabled so hot-enabling it has history; it gates nothing at default 0).
        self._stopout_streaks: Dict[str, int] = {}
        self._last_stopout_timestamps: Dict[str, float] = {}
        self._counted_terminal_grid_ids: Set[str] = set()
        self._last_breaker_warning_timestamp: float = 0.0
        # Track unfavorable grid IDs
        self.unfavorable_grid_ids = set()
        # Track held positions from unfavorable grids
        self.unfavorable_positions = {
            f"{asset}-{config.quote_asset}": {
                'long': {'size': Decimal('0'), 'value': Decimal('0'), 'weighted_price': Decimal('0')},
                'short': {'size': Decimal('0'), 'value': Decimal('0'), 'weighted_price': Decimal('0')}
            }
            for asset in config.portfolio_allocation
        }
        super().__init__(config, *args, **kwargs)
        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        fee_pair = ConnectorPair(connector_name=self.config.connector_name, trading_pair=f"{self.config.fee_asset}-{self.config.quote_asset}")
        self.market_data_provider.initialize_rate_sources([fee_pair])

    async def update_processed_data(self):
        # Get the bb width to use it as the range for the grid
        for asset in self.config.portfolio_allocation:
            trading_pair = f"{asset}-{self.config.quote_asset}"
            candles = self.market_data_provider.get_candles_df(
                connector_name=self.config.connector_name,
                trading_pair=trading_pair,
                interval=self.config.interval,
                max_records=self.config.bb_length + 100
            )
            # GEN-11: fall back to the static grid_range whenever the dynamic width
            # is unavailable — ta.bbands returns None while 1 <= rows < bb_length,
            # and the BBB column is NaN during warm-up. A NaN width propagates into
            # NaN grid prices and fails executor creation every cycle. The bandwidth
            # column is located by prefix because the suffix format changed across
            # pandas_ta versions (0.4.x names it BBB_{len}_{std}_{std}).
            bb_width = self.config.grid_range
            if len(candles) > 0:
                bb = ta.bbands(candles["close"], length=self.config.bb_length, std=self.config.bb_std_dev)
                bbb_columns = [c for c in bb.columns if c.startswith("BBB_")] if bb is not None else []
                if bbb_columns:
                    raw_width = bb[bbb_columns[0]].iloc[-1]
                    try:
                        width = Decimal(str(raw_width)) / Decimal("100")
                    except (InvalidOperation, TypeError, ValueError):
                        width = None
                    if width is not None and width.is_finite() and width > 0:
                        bb_width = width
            self.processed_data[trading_pair] = {
                "bb_width": bb_width
            }

    def _read_balance(self, asset: str, suspect_assets: Set[str]) -> Decimal:
        """
        CLA-411: fail-closed balance read. `get_balance` returns 0 for a missing key,
        so a transient zero/absent read is indistinguishable from a flat position —
        but acting on it makes deviation=-1 and spawns a real BUY grid (and a mirror
        SELL grid when the read recovers). A zero read for an asset previously seen
        nonzero, or any non-Decimal/non-finite/negative read, marks the asset suspect.
        CDX-R02: the in-process nonzero history does not survive a restart, so a
        first-observation zero is ALSO suspect until it has persisted for
        zero_balance_confirmation_seconds — only a stable zero is trusted as a
        genuine cold-start flat position.
        """
        raw = self.market_data_provider.get_balance(self.config.connector_name, asset)
        valid = isinstance(raw, Decimal) and raw.is_finite() and raw >= 0
        balance = raw if valid else Decimal("0")
        if balance > 0:
            self._assets_seen_nonzero.add(asset)
            self._zero_reads_since.pop(asset, None)
        elif not valid or asset in self._assets_seen_nonzero:
            suspect_assets.add(asset)
            # An invalid read says nothing about flatness — restart the confirmation
            # window so it cannot count toward trusting a later zero.
            self._zero_reads_since.pop(asset, None)
        else:
            now = self.market_data_provider.time()
            first_zero = self._zero_reads_since.setdefault(asset, now)
            if now - first_zero < self.config.zero_balance_confirmation_seconds:
                suspect_assets.add(asset)
        return balance

    def update_portfolio_metrics(self):
        """
        Calculate theoretical vs actual portfolio allocations
        """
        metrics = {
            "theoretical": {},
            "actual": {},
            "difference": {},
        }

        # Get real balances and calculate total portfolio value
        suspect_assets: Set[str] = set()
        quote_balance = self._read_balance(self.config.quote_asset, suspect_assets)
        total_value_quote = quote_balance

        # Calculate actual allocations including positions
        for asset in self.config.portfolio_allocation:
            trading_pair = f"{asset}-{self.config.quote_asset}"
            price = self.get_mid_price(trading_pair)
            # Get balance and add any position from active grid
            balance = self._read_balance(asset, suspect_assets)
            value = balance * price
            total_value_quote += value
            metrics["actual"][asset] = value
        # Calculate theoretical allocations and differences
        for asset in self.config.portfolio_allocation:
            theoretical_value = total_value_quote * self.config.portfolio_allocation[asset]
            metrics["theoretical"][asset] = theoretical_value
            metrics["difference"][asset] = metrics["actual"][asset] - theoretical_value
        # Add quote asset metrics
        metrics["actual"][self.config.quote_asset] = quote_balance
        metrics["theoretical"][self.config.quote_asset] = total_value_quote * self.config.quote_asset_allocation
        metrics["difference"][self.config.quote_asset] = quote_balance - metrics["theoretical"][self.config.quote_asset]
        metrics["total_portfolio_value"] = total_value_quote
        self.metrics = metrics
        # CLA-411: any suspect read skews total_value_quote and therefore EVERY
        # asset's theoretical allocation, so the whole tick is treated as unreliable.
        self._suspect_balance_assets = suspect_assets
        if suspect_assets:
            now = self.market_data_provider.time()
            if now - self._last_suspect_balance_warning_timestamp >= self._WARNING_INTERVAL:
                self._last_suspect_balance_warning_timestamp = now
                self.logger().warning(
                    f"Zero/absent or unconfirmed balance read for asset(s) "
                    f"{sorted(suspect_assets)} on {self.config.connector_name}. Treating the "
                    f"read as transient/unreliable and skipping grid creation this tick.")

    def get_active_grids_by_asset(self) -> Dict[str, List[ExecutorInfo]]:
        """Group active grids by asset using filter_executors"""
        active_grids = {}
        for asset in self.config.portfolio_allocation:
            if asset == self.config.quote_asset:
                continue
            trading_pair = f"{asset}-{self.config.quote_asset}"
            active_executors = self.filter_executors(
                executors=self.executors_info,
                filter_func=lambda e: (
                    e.is_active and
                    e.config.trading_pair == trading_pair
                )
            )
            if active_executors:
                active_grids[asset] = active_executors
        return active_grids

    def to_format_status(self) -> List[str]:
        """Generate a detailed status report with portfolio, grid, and position information"""
        status_lines = []
        total_value = self.metrics.get("total_portfolio_value", Decimal("0"))
        # Portfolio Status
        status_lines.append(f"Total Portfolio Value: ${total_value:,.2f}")
        status_lines.append("")
        status_lines.append("Portfolio Status:")
        status_lines.append("-" * 80)
        status_lines.append(
            f"{'Asset':<8} | "
            f"{'Actual':>10} | "
            f"{'Target':>10} | "
            f"{'Diff':>10} | "
            f"{'Dev %':>8}"
        )
        status_lines.append("-" * 80)
        # Show metrics for each asset
        for asset in self.config.portfolio_allocation:
            actual = self.metrics["actual"].get(asset, Decimal("0"))
            theoretical = self.metrics["theoretical"].get(asset, Decimal("0"))
            difference = self.metrics["difference"].get(asset, Decimal("0"))
            deviation_pct = (difference / theoretical * 100) if theoretical != Decimal("0") else Decimal("0")
            status_lines.append(
                f"{asset:<8} | "
                f"${actual:>9.2f} | "
                f"${theoretical:>9.2f} | "
                f"${difference:>+9.2f} | "
                f"{deviation_pct:>+7.1f}%"
            )
        # Add quote asset metrics
        quote_asset = self.config.quote_asset
        actual = self.metrics["actual"].get(quote_asset, Decimal("0"))
        theoretical = self.metrics["theoretical"].get(quote_asset, Decimal("0"))
        difference = self.metrics["difference"].get(quote_asset, Decimal("0"))
        deviation_pct = (difference / theoretical * 100) if theoretical != Decimal("0") else Decimal("0")
        status_lines.append("-" * 80)
        status_lines.append(
            f"{quote_asset:<8} | "
            f"${actual:>9.2f} | "
            f"${theoretical:>9.2f} | "
            f"${difference:>+9.2f} | "
            f"{deviation_pct:>+7.1f}%"
        )
        # Active Grids Summary
        active_grids = self.get_active_grids_by_asset()
        if active_grids:
            status_lines.append("")
            status_lines.append("Active Grids:")
            status_lines.append("-" * 140)
            status_lines.append(
                f"{'Asset':<8} {'Side':<6} | "
                f"{'Total ($)':<10} {'Position':<10} {'Volume':<10} | "
                f"{'PnL':<10} {'RPnL':<10} {'Fees':<10} | "
                f"{'Start':<10} {'Current':<10} {'End':<10} {'Limit':<10}"
            )
            status_lines.append("-" * 140)
            for asset, executors in active_grids.items():
                for executor in executors:
                    config = executor.config
                    custom_info = executor.custom_info
                    trading_pair = config.trading_pair
                    current_price = self.get_mid_price(trading_pair)
                    # Get grid metrics
                    total_amount = Decimal(str(config.total_amount_quote))
                    position_size = Decimal(str(custom_info.get('position_size_quote', '0')))
                    volume = executor.filled_amount_quote
                    pnl = executor.net_pnl_quote
                    realized_pnl_quote = custom_info.get('realized_pnl_quote', Decimal('0'))
                    fees = executor.cum_fees_quote
                    status_lines.append(
                        f"{asset:<8} {config.side.name:<6} | "
                        f"${total_amount:<9.2f} ${position_size:<9.2f} ${volume:<9.2f} | "
                        f"${pnl:>+9.2f} ${realized_pnl_quote:>+9.2f} ${fees:>9.2f} | "
                        f"{config.start_price:<10.4f} {current_price:<10.4f} {config.end_price:<10.4f} {config.limit_price:<10.4f}"
                    )

        status_lines.append("-" * 100 + "\n")
        return status_lines

    def tp_multiplier(self):
        return self.config.tp_sl_ratio

    def sl_multiplier(self):
        return 1 - self.config.tp_sl_ratio

    def _register_grid_terminations(self):
        """
        CLA-308: fold newly-terminated grid executors into the per-pair stop-out
        streaks. Ids are counted once and accumulated in a cumulative set so buffer
        archival cannot decay the streak. A limit-price breach terminates as
        CloseType.STOP_LOSS, or as POSITION_HOLD because QGA grids run
        keep_position=True (grid_executor maps the breach to POSITION_HOLD then; an
        operator early-stop maps there too, which errs on the fail-closed side when
        the breaker is enabled). TAKE_PROFIT/COMPLETED closes reset the streak;
        FAILED/INSUFFICIENT_BALANCE are not market outcomes and change nothing.
        """
        now = self.market_data_provider.time()
        for executor in self.executors_info:
            if executor.is_active or executor.type != "grid_executor":
                continue
            if executor.id in self._counted_terminal_grid_ids:
                continue
            self._counted_terminal_grid_ids.add(executor.id)
            trading_pair = executor.config.trading_pair
            if executor.close_type in (CloseType.STOP_LOSS, CloseType.POSITION_HOLD):
                self._stopout_streaks[trading_pair] = self._stopout_streaks.get(trading_pair, 0) + 1
                self._last_stopout_timestamps[trading_pair] = executor.close_timestamp or now
            elif executor.close_type in (CloseType.TAKE_PROFIT, CloseType.COMPLETED):
                self._stopout_streaks[trading_pair] = 0

    def _stop_out_breaker_tripped(self, trading_pair: str) -> bool:
        """CLA-308: True while the (opt-in) breaker blocks new grids for the pair."""
        limit = self.config.stop_out_breaker_count
        if limit <= 0:
            return False  # default: breaker disabled, behavior unchanged
        if self._stopout_streaks.get(trading_pair, 0) < limit:
            return False
        now = self.market_data_provider.time()
        last_stopout = self._last_stopout_timestamps.get(trading_pair, 0.0)
        if now - last_stopout < self.config.stop_out_breaker_cooldown:
            if now - self._last_breaker_warning_timestamp >= self._WARNING_INTERVAL:
                self._last_breaker_warning_timestamp = now
                self.logger().warning(
                    f"Stop-out breaker tripped for {trading_pair}: "
                    f"{self._stopout_streaks.get(trading_pair, 0)} consecutive stop-outs "
                    f"(limit {limit}). Pausing grid creation until "
                    f"{self.config.stop_out_breaker_cooldown}s after the last stop-out.")
            return True
        # Cooldown served — reset the streak and allow grids again.
        self._stopout_streaks[trading_pair] = 0
        return False

    def determine_executor_actions(self) -> List[Union[CreateExecutorAction, StopExecutorAction]]:
        actions = []
        self.update_portfolio_metrics()
        self._register_grid_terminations()
        if self._suspect_balance_assets:
            # CLA-411: unreliable balance read this tick — fail closed, create nothing.
            return actions
        active_grids_by_asset = self.get_active_grids_by_asset()
        for asset in self.config.portfolio_allocation:
            if asset == self.config.quote_asset:
                continue
            trading_pair = f"{asset}-{self.config.quote_asset}"
            # Check if there are any active grids for this asset
            if asset in active_grids_by_asset:
                self.logger().debug(f"Skipping {trading_pair} - Active grid exists")
                continue
            if self._stop_out_breaker_tripped(trading_pair):
                continue
            theoretical = self.metrics["theoretical"][asset]
            difference = self.metrics["difference"][asset]
            deviation = difference / theoretical if theoretical != Decimal("0") else Decimal("0")
            mid_price = self.get_mid_price(trading_pair)

            # Calculate dynamic grid value percentage based on deviation
            abs_deviation = abs(deviation)
            grid_value_pct = self.config.max_grid_value_pct if abs_deviation > self.config.max_deviation else self.config.base_grid_value_pct

            self.logger().info(
                f"{trading_pair} Grid Sizing - "
                f"Deviation: {deviation:+.1%}, "
                f"Grid Value %: {grid_value_pct:.1%}"
            )
            if self.config.dynamic_grid_range:
                # GEN-11: bb_width is a validated positive finite Decimal (or the
                # static grid_range fallback); guard the key for pre-warm-up ticks.
                grid_range = Decimal(self.processed_data.get(trading_pair, {}).get("bb_width", self.config.grid_range))
            else:
                grid_range = self.config.grid_range

            # Determine which zone we're in by normalizing the deviation over the theoretical allocation
            if deviation < -self.config.long_only_threshold:
                # Long-only zone - only create buy grids
                if difference < Decimal("0"):  # Only if we need to buy
                    grid_value = min(abs(difference), theoretical * grid_value_pct)
                    start_price = mid_price * (1 - grid_range * self.sl_multiplier())
                    end_price = mid_price * (1 + grid_range * self.tp_multiplier())
                    grid_action = self.create_grid_executor(
                        trading_pair=trading_pair,
                        side=TradeType.BUY,
                        start_price=start_price,
                        end_price=end_price,
                        grid_value=grid_value,
                        is_unfavorable=False
                    )
                    if grid_action is not None:
                        actions.append(grid_action)
            elif deviation > self.config.short_only_threshold:
                # Short-only zone - only create sell grids
                if difference > Decimal("0"):  # Only if we need to sell
                    grid_value = min(abs(difference), theoretical * grid_value_pct)
                    start_price = mid_price * (1 - grid_range * self.tp_multiplier())
                    end_price = mid_price * (1 + grid_range * self.sl_multiplier())
                    grid_action = self.create_grid_executor(
                        trading_pair=trading_pair,
                        side=TradeType.SELL,
                        start_price=start_price,
                        end_price=end_price,
                        grid_value=grid_value,
                        is_unfavorable=False
                    )
                    if grid_action is not None:
                        actions.append(grid_action)
            else:
                # we create a buy and a sell grid with higher range pct and the base grid value pct
                # to hedge the position
                grid_value = theoretical * grid_value_pct
                if difference < Decimal("0"):  # create a bigger buy grid and sell grid
                    # Create buy grid
                    start_price = mid_price * (1 - 2 * grid_range * self.sl_multiplier())
                    end_price = mid_price * (1 + grid_range * self.tp_multiplier())
                    buy_grid_action = self.create_grid_executor(
                        trading_pair=trading_pair,
                        side=TradeType.BUY,
                        start_price=start_price,
                        end_price=end_price,
                        grid_value=grid_value,
                        is_unfavorable=False
                    )
                    if buy_grid_action is not None:
                        actions.append(buy_grid_action)
                    # Create sell grid
                    start_price = mid_price * (1 - grid_range * self.tp_multiplier())
                    end_price = mid_price * (1 + 2 * grid_range * self.sl_multiplier())
                    sell_grid_action = self.create_grid_executor(
                        trading_pair=trading_pair,
                        side=TradeType.SELL,
                        start_price=start_price,
                        end_price=end_price,
                        grid_value=grid_value,
                        is_unfavorable=False
                    )
                    if sell_grid_action is not None:
                        actions.append(sell_grid_action)
                if difference > Decimal("0"):
                    # Create sell grid
                    start_price = mid_price * (1 - 2 * grid_range * self.tp_multiplier())
                    end_price = mid_price * (1 + grid_range * self.sl_multiplier())
                    sell_grid_action = self.create_grid_executor(
                        trading_pair=trading_pair,
                        side=TradeType.SELL,
                        start_price=start_price,
                        end_price=end_price,
                        grid_value=grid_value,
                        is_unfavorable=False
                    )
                    if sell_grid_action is not None:
                        actions.append(sell_grid_action)
                    # Create buy grid
                    start_price = mid_price * (1 - grid_range * self.sl_multiplier())
                    end_price = mid_price * (1 + 2 * grid_range * self.tp_multiplier())
                    buy_grid_action = self.create_grid_executor(
                        trading_pair=trading_pair,
                        side=TradeType.BUY,
                        start_price=start_price,
                        end_price=end_price,
                        grid_value=grid_value,
                        is_unfavorable=False
                    )
                    if buy_grid_action is not None:
                        actions.append(buy_grid_action)
        return actions

    def create_grid_executor(
        self,
        trading_pair: str,
        side: TradeType,
        start_price: Decimal,
        end_price: Decimal,
        grid_value: Decimal,
        is_unfavorable: bool = False
    ) -> Optional[CreateExecutorAction]:
        """Creates a grid executor with dynamic sizing and range adjustments"""
        # Get trading rules and minimum notional
        trading_rules = self.market_data_provider.get_trading_rules(self.config.connector_name, trading_pair)
        min_notional = max(
            self.config.min_order_amount,
            trading_rules.min_notional_size if trading_rules else Decimal("5.0")
        )
        # Add safety margin and check if grid value is sufficient
        min_grid_value = min_notional * Decimal("5")  # Ensure room for at least 5 levels
        if grid_value < min_grid_value:
            self.logger().info(
                f"Grid value {grid_value} is too small for {trading_pair}. "
                f"Minimum required for viable grid: {min_grid_value}"
            )
            return None  # Skip grid creation if value is too small

        # Select order frequency based on grid favorability
        order_frequency = (
            self.config.unfavorable_order_frequency if is_unfavorable
            else self.config.favorable_order_frequency
        )
        # Calculate limit price to be more aggressive than grid boundaries
        if side == TradeType.BUY:
            # For buys, limit price should be lower than start price
            limit_price = start_price * (1 - self.config.limit_price_spread)
        else:
            # For sells, limit price should be higher than end price
            limit_price = end_price * (1 + self.config.limit_price_spread)
        # Create the executor action. The geometry here is computed from the live
        # mid price, so GEN-9's GridExecutorConfig validators can reject it (e.g. a
        # NaN/zero mid price yields non-finite prices) — fail closed: skip the grid
        # for this cycle with a rate-limited warning instead of raising per tick.
        try:
            executor_config = GridExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=trading_pair,
                side=side,
                start_price=start_price,
                end_price=end_price,
                limit_price=limit_price,
                leverage=self.config.leverage,
                total_amount_quote=grid_value,
                safe_extra_spread=self.config.safe_extra_spread,
                min_spread_between_orders=self.config.min_spread_between_orders,
                min_order_amount_quote=self.config.min_order_amount,
                max_open_orders=self.config.max_open_orders,
                order_frequency=order_frequency,  # Use dynamic order frequency
                max_orders_per_batch=self.config.max_orders_per_batch,
                activation_bounds=self.config.activation_bounds,
                keep_position=True,  # Always keep position for potential reversal
                coerce_tp_to_step=True,
                triple_barrier_config=TripleBarrierConfig(
                    take_profit=self.config.grid_tp_multiplier,
                    open_order_type=OrderType.LIMIT_MAKER,
                    take_profit_order_type=OrderType.LIMIT_MAKER,
                    stop_loss=None,
                    time_limit=None,
                    trailing_stop=None,
                ))
        except ValidationError as e:
            now = self.market_data_provider.time()
            if now - self._last_invalid_geometry_warning_timestamp >= self._WARNING_INTERVAL:
                self._last_invalid_geometry_warning_timestamp = now
                self.logger().warning(
                    f"Skipping grid creation for {trading_pair} ({side.name}): invalid geometry "
                    f"start={start_price} end={end_price} limit={limit_price} — {e.errors()[0].get('msg', e)}")
            return None
        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config)
        # Track unfavorable grid configs
        if is_unfavorable:
            self.unfavorable_grid_ids.add(action.executor_config.id)
            self.logger().info(
                f"Created unfavorable grid for {trading_pair} - "
                f"Side: {side.name}, Value: ${grid_value:,.2f}, "
                f"Order Frequency: {order_frequency}s"
            )
        else:
            self.logger().info(
                f"Created favorable grid for {trading_pair} - "
                f"Side: {side.name}, Value: ${grid_value:,.2f}, "
                f"Order Frequency: {order_frequency}s"
            )

        return action

    def get_mid_price(self, trading_pair: str) -> Decimal:
        return self.market_data_provider.get_price_by_type(self.config.connector_name, trading_pair, PriceType.MidPrice)

    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(
            connector=self.config.connector_name,
            trading_pair=trading_pair + "-" + self.config.quote_asset,
            interval=self.config.interval,
            max_records=self.config.bb_length + 100
        ) for trading_pair in self.config.portfolio_allocation.keys()]
