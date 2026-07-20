from decimal import Decimal
from typing import List

import numpy as np
import pandas as pd
from pydantic import field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo
from sklearn.linear_model import LinearRegression

from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


class StatArbConfig(ControllerConfigBase):
    """
    Configuration for a statistical arbitrage controller that trades two cointegrated assets.
    """
    controller_type: str = "generic"
    controller_name: str = "stat_arb"
    connector_pair_dominant: ConnectorPair = ConnectorPair(connector_name="binance_perpetual", trading_pair="SOL-USDT")
    connector_pair_hedge: ConnectorPair = ConnectorPair(connector_name="binance_perpetual", trading_pair="POPCAT-USDT")
    interval: str = "1m"
    lookback_period: int = 300
    entry_threshold: Decimal = Decimal("2.0")
    take_profit: Decimal = Decimal("0.0008")
    tp_global: Decimal = Decimal("0.01")
    sl_global: Decimal = Decimal("0.05")
    min_amount_quote: Decimal = Decimal("10")
    quoter_spread: Decimal = Decimal("0.0001")
    quoter_cooldown: int = 30
    quoter_refresh: int = 10
    max_orders_placed_per_side: int = 2
    max_orders_filled_per_side: int = 2
    max_position_deviation: Decimal = Decimal("0.1")
    pos_hedge_ratio: Decimal = Decimal("1.0")
    leverage: int = 20
    position_mode: PositionMode = PositionMode.HEDGE
    stop_out_cooldown: int = 300

    @field_validator("pos_hedge_ratio")
    @classmethod
    def validate_pos_hedge_ratio(cls, v: Decimal) -> Decimal:
        # CLA-012: pos_hedge_ratio == -1 crashes __init__ with a division by zero;
        # non-positive or non-finite ratios produce nonsensical leg allocations.
        if not v.is_finite() or v <= Decimal("0"):
            raise ValueError("pos_hedge_ratio must be a finite number greater than zero")
        return v

    @field_validator("entry_threshold")
    @classmethod
    def validate_entry_threshold(cls, v: Decimal) -> Decimal:
        # CLA-012: a zero/negative threshold turns every z-score reading into an entry.
        if not v.is_finite() or v <= Decimal("0"):
            raise ValueError("entry_threshold must be a finite number greater than zero")
        return v

    @field_validator("tp_global", "sl_global")
    @classmethod
    def validate_global_barriers(cls, v: Decimal, info: ValidationInfo) -> Decimal:
        # CLA-012: sl_global <= 0 fires the global stop at zero PnL (and with the
        # stop-out latch would keep the controller permanently flat); same for tp_global.
        if not v.is_finite() or v <= Decimal("0"):
            raise ValueError(f"{info.field_name} must be a finite number greater than zero")
        return v

    @field_validator("lookback_period")
    @classmethod
    def validate_lookback_period(cls, v: int) -> int:
        if v < 2:
            raise ValueError("lookback_period must be at least 2")
        return v

    @field_validator("stop_out_cooldown")
    @classmethod
    def validate_stop_out_cooldown(cls, v: int) -> int:
        # A zero cooldown would expire the stop-out latch on the breach tick itself,
        # restoring immediate re-entry on the still-hot z-score (the exact CLA-304
        # exposure) and disabling the cross-tick flatten sweep.
        if v <= 0:
            raise ValueError("stop_out_cooldown must be greater than zero")
        return v

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        if v not in CandlesBase.interval_to_seconds:
            raise ValueError(
                f"Unsupported interval '{v}'. Must be one of {list(CandlesBase.interval_to_seconds.keys())}")
        return v

    @model_validator(mode="after")
    def validate_distinct_pairs(self):
        if (self.connector_pair_dominant.connector_name == self.connector_pair_hedge.connector_name and
                self.connector_pair_dominant.trading_pair == self.connector_pair_hedge.trading_pair):
            raise ValueError("connector_pair_dominant and connector_pair_hedge must be different markets")
        return self

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            take_profit=self.take_profit,
            open_order_type=OrderType.LIMIT_MAKER,
            take_profit_order_type=OrderType.LIMIT_MAKER,
        )

    def update_markets(self, markets: dict) -> dict:
        """Update markets dictionary with both trading pairs"""
        # Add dominant pair
        if self.connector_pair_dominant.connector_name not in markets:
            markets[self.connector_pair_dominant.connector_name] = set()
        markets[self.connector_pair_dominant.connector_name].add(self.connector_pair_dominant.trading_pair)

        # Add hedge pair
        if self.connector_pair_hedge.connector_name not in markets:
            markets[self.connector_pair_hedge.connector_name] = set()
        markets[self.connector_pair_hedge.connector_name].add(self.connector_pair_hedge.trading_pair)

        return markets


class StatArb(ControllerBase):
    """
    Statistical arbitrage controller that trades two cointegrated assets.
    """

    def __init__(self, config: StatArbConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.theoretical_dominant_quote = self.config.total_amount_quote * (1 / (1 + self.config.pos_hedge_ratio))
        self.theoretical_hedge_quote = self.config.total_amount_quote * (self.config.pos_hedge_ratio / (1 + self.config.pos_hedge_ratio))
        # Candle interval in seconds — guaranteed resolvable by the interval validator.
        self._interval_seconds = CandlesBase.interval_to_seconds[self.config.interval]
        # Stop-out latch (CLA-304): while time < _stop_out_until, entry quoting is gated
        # so a still-hot z-score cannot re-enter right after a global TP/SL exit.
        self._stop_out_until: float = 0.0
        self._last_stop_out_log_ts: float = 0.0

        # Initialize processed data dictionary
        self.processed_data = {
            "dominant_price": None,
            "hedge_price": None,
            "spread": Decimal("0"),
            "z_score": Decimal("0"),
            "hedge_ratio": None,
            "position_dominant": Decimal("0"),
            "position_hedge": Decimal("0"),
            "active_orders_dominant": [],
            "active_orders_hedge": [],
            "pair_pnl": Decimal("0"),
            "pair_pnl_pct": Decimal("0"),
            "signal": 0,  # 0: no signal, 1: long dominant/short hedge, -1: short dominant/long hedge
            "alpha": 0.0,
            "beta": 0.0,
            "executors_dominant_placed": [],
            "executors_dominant_filled": [],
            "executors_hedge_placed": [],
            "executors_hedge_filled": [],
        }

        # Setup max records for safety
        max_records = self.config.lookback_period + 20
        self.max_records = max_records
        if "_perpetual" in self.config.connector_pair_dominant.connector_name:
            connector = self.market_data_provider.get_connector(self.config.connector_pair_dominant.connector_name)
            connector.set_position_mode(self.config.position_mode)
            connector.set_leverage(self.config.connector_pair_dominant.trading_pair, self.config.leverage)
        if "_perpetual" in self.config.connector_pair_hedge.connector_name:
            connector = self.market_data_provider.get_connector(self.config.connector_pair_hedge.connector_name)
            connector.set_position_mode(self.config.position_mode)
            connector.set_leverage(self.config.connector_pair_hedge.trading_pair, self.config.leverage)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        The execution logic for the statistical arbitrage strategy.
        Market Data Conditions: Signal is generated based on the z-score of the spread between the two assets.
                                If signal == 1 --> long dominant/short hedge
                                If signal == -1 --> short dominant/long hedge
        Execution Conditions: If the signal is generated add position executors to quote from the dominant and hedge markets.
                              We compare the current position with the theoretical position for the dominant and hedge assets.
                              If the current position + the active placed amount is greater than the theoretical position, can't place more orders.
                              If the imbalance scaled pct is greater than the threshold, we avoid placing orders in the market passed on filtered_connector_pair.
                              If the pnl of total position is greater than the take profit or lower than the stop loss, we close the position.
        """
        actions: List[ExecutorAction] = []
        # Check global take profit and stop loss. This must run every tick, even when the
        # z-score signal is unavailable (candles outage) — GEN-1.
        pair_pnl_pct = self.processed_data.get("pair_pnl_pct", Decimal("0"))
        if pair_pnl_pct > self.config.tp_global or pair_pnl_pct < -self.config.sl_global:
            # Global TP/SL breach (CLA-304): latch the stop-out cooldown. The latch
            # re-arms on every breached tick, so re-entry stays gated for
            # stop_out_cooldown seconds after the LAST breached observation (never
            # shorter than configured).
            now = self.market_data_provider.time()
            self._stop_out_until = now + self.config.stop_out_cooldown
            if now - self._last_stop_out_log_ts >= 60:
                self._last_stop_out_log_ts = now
                self.logger().warning(
                    f"Global TP/SL breached (pair pnl {pair_pnl_pct:.4%}) — cancelling entry executors, "
                    f"closing positions and gating re-entry for {self.config.stop_out_cooldown}s.")
        if self.stop_out_cooldown_active():
            # Global-exit flatten is transactional across ticks (CLA-304 hardening): a
            # fill that raced the breach-tick cancellation, or a POSITION_HOLD transfer
            # that lands in positions_held only after the breach-tick close was sized,
            # shows up here on a LATER tick — when the original breach reading may
            # already be gone and the residual sits on the CURRENT signal side with
            # ~zero PnL of its own. While the latch is active, keep stopping entry
            # executors and closing everything held, regardless of signal side, so no
            # residual leg survives the stop-out. The in-flight close guard inside
            # get_executors_to_reduce_position keeps this sweep idempotent per tick.
            actions.extend(self.get_entry_executors_to_stop())
            for position in self.positions_held:
                actions.extend(self.get_executors_to_reduce_position(position))
            return actions
        # Check the signal
        if self.processed_data["signal"] != 0:
            actions.extend(self.get_executors_to_quote())
            actions.extend(self.get_executors_to_reduce_position_on_opposite_signal())

        # Get the executors to keep position after a cooldown is reached
        actions.extend(self.get_executors_to_keep_position())
        actions.extend(self.get_executors_to_refresh())

        return actions

    def stop_out_cooldown_active(self) -> bool:
        """True while re-entry is gated after a global TP/SL exit (CLA-304)."""
        return self.market_data_provider.time() < self._stop_out_until

    def get_entry_executors_to_stop(self) -> List[ExecutorAction]:
        """
        Stop every active entry (position) executor on a global TP/SL breach (CLA-304,
        pmm_mister GEN-4 pattern). In-flight close order executors are excluded — this
        controller only creates order executors to close positions, and stopping one
        would cancel the exit itself. keep_position=False makes each executor cancel
        its entry and flatten any fill it owns: folding fills into positions_held
        instead would race the breach-tick close snapshot — the POSITION_HOLD transfer
        lands only after the executor is done (after the market close was already
        sized), leaving the residual as unowned inventory on the still-hot side.
        """
        entry_executors = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: e.is_active and e.type == "position_executor")
        return [StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=False)
                for executor in entry_executors]

    def get_executors_to_reduce_position_on_opposite_signal(self) -> List[ExecutorAction]:
        if self.processed_data["signal"] == 1:
            dominant_side, hedge_side = TradeType.SELL, TradeType.BUY
        elif self.processed_data["signal"] == -1:
            dominant_side, hedge_side = TradeType.BUY, TradeType.SELL
        else:
            return []
        # Get executors to stop (only active ones — StopExecutorAction on a terminated
        # executor is re-sent forever, GEN-2)
        dominant_active_executors_to_stop = self.filter_executors(self.executors_info, filter_func=lambda e: e.is_active and e.connector_name == self.config.connector_pair_dominant.connector_name and e.trading_pair == self.config.connector_pair_dominant.trading_pair and e.side == dominant_side)
        hedge_active_executors_to_stop = self.filter_executors(self.executors_info, filter_func=lambda e: e.is_active and e.connector_name == self.config.connector_pair_hedge.connector_name and e.trading_pair == self.config.connector_pair_hedge.trading_pair and e.side == hedge_side)
        stop_actions = [StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=False) for executor in dominant_active_executors_to_stop + hedge_active_executors_to_stop]

        # Get order executors to reduce positions
        reduce_actions: List[ExecutorAction] = []
        for position in self.positions_held:
            if position.connector_name == self.config.connector_pair_dominant.connector_name and position.trading_pair == self.config.connector_pair_dominant.trading_pair and position.side == dominant_side:
                reduce_actions.extend(self.get_executors_to_reduce_position(position))
            elif position.connector_name == self.config.connector_pair_hedge.connector_name and position.trading_pair == self.config.connector_pair_hedge.trading_pair and position.side == hedge_side:
                reduce_actions.extend(self.get_executors_to_reduce_position(position))
        return stop_actions + reduce_actions

    def get_executors_to_keep_position(self) -> List[ExecutorAction]:
        stop_actions: List[ExecutorAction] = []
        for executor in self.processed_data["executors_dominant_filled"] + self.processed_data["executors_hedge_filled"]:
            if self.market_data_provider.time() - executor.timestamp >= self.config.quoter_cooldown:
                # Create a new executor to keep the position
                stop_actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=True))
        return stop_actions

    def get_executors_to_refresh(self) -> List[ExecutorAction]:
        refresh_actions: List[ExecutorAction] = []
        for executor in self.processed_data["executors_dominant_placed"] + self.processed_data["executors_hedge_placed"]:
            if self.market_data_provider.time() - executor.timestamp >= self.config.quoter_refresh:
                # Create a new executor to refresh the position
                refresh_actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=False))
        return refresh_actions

    def get_executors_to_quote(self) -> List[ExecutorAction]:
        """
        Get Order Executor to quote from the dominant and hedge markets.
        """
        actions: List[ExecutorAction] = []
        if self.stop_out_cooldown_active():
            # Re-entry gated after a global TP/SL exit (CLA-304): the z-score is usually
            # still beyond the entry threshold right after a stop-out.
            return actions
        trade_type_dominant = TradeType.BUY if self.processed_data["signal"] == 1 else TradeType.SELL
        trade_type_hedge = TradeType.SELL if self.processed_data["signal"] == 1 else TradeType.BUY

        # Analyze dominant active orders, max deviation and imbalance to create a new executor
        if self.processed_data["dominant_gap"] > Decimal("0") and \
           self.processed_data["filter_connector_pair"] != self.config.connector_pair_dominant and \
           len(self.processed_data["executors_dominant_placed"]) < self.config.max_orders_placed_per_side and \
           len(self.processed_data["executors_dominant_filled"]) < self.config.max_orders_filled_per_side:
            # Create Position Executor for dominant asset
            if trade_type_dominant == TradeType.BUY:
                price = self.processed_data["min_price_dominant"] * (1 - self.config.quoter_spread)
            else:
                price = self.processed_data["max_price_dominant"] * (1 + self.config.quoter_spread)
            dominant_executor_config = PositionExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_pair_dominant.connector_name,
                trading_pair=self.config.connector_pair_dominant.trading_pair,
                side=trade_type_dominant,
                entry_price=price,
                amount=self.config.min_amount_quote / self.processed_data["dominant_price"],
                triple_barrier_config=self.config.triple_barrier_config,
                leverage=self.config.leverage,
            )
            actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=dominant_executor_config))

        # Analyze hedge active orders, max deviation and imbalance to create a new executor
        if self.processed_data["hedge_gap"] > Decimal("0") and \
           self.processed_data["filter_connector_pair"] != self.config.connector_pair_hedge and \
           len(self.processed_data["executors_hedge_placed"]) < self.config.max_orders_placed_per_side and \
           len(self.processed_data["executors_hedge_filled"]) < self.config.max_orders_filled_per_side:
            # Create Position Executor for hedge asset
            if trade_type_hedge == TradeType.BUY:
                price = self.processed_data["min_price_hedge"] * (1 - self.config.quoter_spread)
            else:
                price = self.processed_data["max_price_hedge"] * (1 + self.config.quoter_spread)
            hedge_executor_config = PositionExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_pair_hedge.connector_name,
                trading_pair=self.config.connector_pair_hedge.trading_pair,
                side=trade_type_hedge,
                entry_price=price,
                amount=self.config.min_amount_quote / self.processed_data["hedge_price"],
                triple_barrier_config=self.config.triple_barrier_config,
                leverage=self.config.leverage,
            )
            actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=hedge_executor_config))
        return actions

    def get_active_close_amount(self, connector_name: str, trading_pair: str, close_side: TradeType) -> Decimal:
        """
        Sum the amounts of active close-side order executors for a pair. Used as an
        in-flight guard so reduce actions are not re-emitted at full size every tick
        while a previous close is still executing (GEN-2).
        """
        active_close_executors = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: e.is_active and e.type == "order_executor" and
            e.connector_name == connector_name and e.trading_pair == trading_pair and
            e.config.side == close_side
        )
        return Decimal(str(sum(e.config.amount for e in active_close_executors)))

    def get_executors_to_reduce_position(self, position: PositionSummary) -> List[ExecutorAction]:
        """
        Get Order Executor to reduce position. The target amount is reduced by the
        amount already in flight on active close-side order executors (GEN-2).
        """
        if position.amount > Decimal("0"):
            close_side = TradeType.BUY if position.side == TradeType.SELL else TradeType.SELL
            in_flight_close_amount = self.get_active_close_amount(
                position.connector_name, position.trading_pair, close_side)
            amount_to_close = position.amount - in_flight_close_amount
            if amount_to_close <= Decimal("0"):
                return []
            # Close position
            config = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=position.connector_name,
                trading_pair=position.trading_pair,
                side=close_side,
                amount=amount_to_close,
                position_action=PositionAction.CLOSE,
                execution_strategy=ExecutionStrategy.MARKET,
                leverage=self.config.leverage,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]
        return []

    async def update_processed_data(self):
        """
        Update processed data with the latest market information and statistical calculations
        needed for the statistical arbitrage strategy.
        """
        # Stat arb analysis. The signal may be unavailable (empty candles, short lookback,
        # zero spread std) — the rest of this method MUST still run so that the global
        # TP/SL evaluation in determine_executor_actions keeps working (GEN-1).
        spread, z_score = self.get_spread_and_z_score()

        # Current prices
        dominant_price, hedge_price = self.get_pairs_prices()
        prices_valid = self._is_valid_price(dominant_price) and self._is_valid_price(hedge_price)

        # Generate trading signal based on z-score
        entry_threshold = float(self.config.entry_threshold)
        if z_score is None or not prices_valid:
            # Signal unavailable — fail closed on quoting, keep risk management running
            signal = 0
        elif z_score > entry_threshold:
            # Spread is too high, expect it to revert: long dominant, short hedge
            signal = 1
        elif z_score < -entry_threshold:
            # Spread is too low, expect it to revert: short dominant, long hedge
            signal = -1
        else:
            # No signal
            signal = 0

        # Aggregate EVERY position summary for each configured pair (CDX-005): in HEDGE
        # mode both sides coexist while a signal flip is being reduced, and the global
        # TP/SL must see the losing non-current side. The aggregation is independent of
        # the signal, so signal == 0 cannot pick a nondeterministic side. PnL is
        # unrealized only, denominated on gross open exposure — cumulative realized
        # PnL/fees persist across restarts and would let banked history mask (or force)
        # an exit on the currently open position.
        dominant_positions = [
            position for position in self.positions_held
            if position.connector_name == self.config.connector_pair_dominant.connector_name and
            position.trading_pair == self.config.connector_pair_dominant.trading_pair]
        hedge_positions = [
            position for position in self.positions_held
            if position.connector_name == self.config.connector_pair_hedge.connector_name and
            position.trading_pair == self.config.connector_pair_hedge.trading_pair]
        position_dominant_quote = sum((abs(position.amount_quote) for position in dominant_positions), Decimal("0"))
        position_hedge_quote = sum((abs(position.amount_quote) for position in hedge_positions), Decimal("0"))
        pair_pnl_quote = sum(
            (position.unrealized_pnl_quote for position in dominant_positions + hedge_positions), Decimal("0"))
        total_exposure_quote = position_dominant_quote + position_hedge_quote
        pair_pnl_pct = pair_pnl_quote / total_exposure_quote if total_exposure_quote > Decimal("0") else Decimal("0")
        # Get active executors
        executors_dominant_placed, executors_dominant_filled = self.get_executors_dominant()
        executors_hedge_placed, executors_hedge_filled = self.get_executors_hedge()
        min_price_dominant = Decimal(str(min([executor.config.entry_price for executor in executors_dominant_placed]))) if executors_dominant_placed else None
        max_price_dominant = Decimal(str(max([executor.config.entry_price for executor in executors_dominant_placed]))) if executors_dominant_placed else None
        min_price_hedge = Decimal(str(min([executor.config.entry_price for executor in executors_hedge_placed]))) if executors_hedge_placed else None
        max_price_hedge = Decimal(str(max([executor.config.entry_price for executor in executors_hedge_placed]))) if executors_hedge_placed else None

        active_amount_dominant = Decimal(str(sum([executor.filled_amount_quote for executor in executors_dominant_filled])))
        active_amount_hedge = Decimal(str(sum([executor.filled_amount_quote for executor in executors_hedge_filled])))

        # Compute imbalance based on the hedge ratio
        dominant_gap = self.theoretical_dominant_quote - position_dominant_quote - active_amount_dominant
        hedge_gap = self.theoretical_hedge_quote - position_hedge_quote - active_amount_hedge
        imbalance = position_dominant_quote - position_hedge_quote
        imbalance_scaled = position_dominant_quote - position_hedge_quote * self.config.pos_hedge_ratio
        imbalance_scaled_pct = imbalance_scaled / position_dominant_quote if position_dominant_quote != Decimal("0") else Decimal("0")
        filter_connector_pair = None
        if imbalance_scaled_pct > self.config.max_position_deviation:
            # Avoid placing orders in the dominant market
            filter_connector_pair = self.config.connector_pair_dominant
        elif imbalance_scaled_pct < -self.config.max_position_deviation:
            # Avoid placing orders in the hedge market
            filter_connector_pair = self.config.connector_pair_hedge

        # Update processed data (safe defaults when the signal is unavailable — GEN-1)
        self.processed_data.update({
            "dominant_price": self._safe_decimal(dominant_price),
            "hedge_price": self._safe_decimal(hedge_price),
            "spread": self._safe_decimal(spread),
            "z_score": self._safe_decimal(z_score),
            "dominant_gap": Decimal(str(dominant_gap)),
            "hedge_gap": Decimal(str(hedge_gap)),
            "position_dominant_quote": position_dominant_quote,
            "position_hedge_quote": position_hedge_quote,
            "active_amount_dominant": active_amount_dominant,
            "active_amount_hedge": active_amount_hedge,
            "signal": signal,
            # Store full dataframes for reference
            "imbalance": Decimal(str(imbalance)),
            "imbalance_scaled_pct": Decimal(str(imbalance_scaled_pct)),
            "filter_connector_pair": filter_connector_pair,
            "min_price_dominant": min_price_dominant if min_price_dominant is not None else self._safe_decimal(dominant_price),
            "max_price_dominant": max_price_dominant if max_price_dominant is not None else self._safe_decimal(dominant_price),
            "min_price_hedge": min_price_hedge if min_price_hedge is not None else self._safe_decimal(hedge_price),
            "max_price_hedge": max_price_hedge if max_price_hedge is not None else self._safe_decimal(hedge_price),
            "executors_dominant_filled": executors_dominant_filled,
            "executors_hedge_filled": executors_hedge_filled,
            "executors_dominant_placed": executors_dominant_placed,
            "executors_hedge_placed": executors_hedge_placed,
            "pair_pnl_pct": pair_pnl_pct,
        })

    @staticmethod
    def _is_valid_price(price) -> bool:
        """A usable price is a finite, positive number."""
        if price is None:
            return False
        try:
            price_decimal = Decimal(str(price))
        except Exception:
            return False
        return price_decimal.is_finite() and price_decimal > Decimal("0")

    @staticmethod
    def _safe_decimal(value) -> Decimal:
        """Convert to Decimal, falling back to 0 for None/unparseable values (GEN-1)."""
        if value is None:
            return Decimal("0")
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal("0")

    def get_spread_and_z_score(self):
        # A failure here must degrade to "signal unavailable" — an exception would
        # propagate into update_processed_data and stall the global TP/SL loop (GEN-1).
        try:
            return self._compute_spread_and_z_score()
        except Exception:
            self.logger().warning("Spread/z-score computation failed; signal unavailable.", exc_info=True)
            return None, None

    def _aligned_close_frames(self, dominant_df: pd.DataFrame, hedge_df: pd.DataFrame, now: float) -> pd.DataFrame:
        """
        Pair the two candle frames by candle-open timestamp (CDX-004). Only bars that
        are closed (opened at least one interval before `now`) with finite, positive
        closes participate; an inner join then drops any timestamp missing from either
        feed, so a missing/late candle removes that one row instead of shifting every
        subsequent pair. There is deliberately no positional fallback.
        """
        empty = pd.DataFrame(columns=["timestamp", "close_dominant", "close_hedge"])
        cleaned = []
        for df in (dominant_df, hedge_df):
            if "timestamp" not in df.columns or "close" not in df.columns:
                return empty
            bars = df[["timestamp", "close"]].copy()
            bars["timestamp"] = pd.to_numeric(bars["timestamp"], errors="coerce")
            bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
            bars = bars.dropna()
            bars = bars[(bars["close"] > 0) & (bars["timestamp"] + self._interval_seconds <= now)]
            bars = bars.sort_values("timestamp").drop_duplicates(subset="timestamp", keep="last")
            cleaned.append(bars)
        merged = pd.merge(cleaned[0], cleaned[1], on="timestamp", how="inner",
                          suffixes=("_dominant", "_hedge"))
        return merged.sort_values("timestamp").reset_index(drop=True)

    def _compute_spread_and_z_score(self):
        # Fetch candle data for both assets
        dominant_df = self.market_data_provider.get_candles_df(
            connector_name=self.config.connector_pair_dominant.connector_name,
            trading_pair=self.config.connector_pair_dominant.trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )

        hedge_df = self.market_data_provider.get_candles_df(
            connector_name=self.config.connector_pair_hedge.connector_name,
            trading_pair=self.config.connector_pair_hedge.trading_pair,
            interval=self.config.interval,
            max_records=self.max_records
        )

        if dominant_df.empty or hedge_df.empty:
            self.logger().warning("Not enough candle data available for statistical analysis")
            return None, None

        # Align by timestamp — never by array position (CDX-004)
        merged = self._aligned_close_frames(dominant_df, hedge_df, self.market_data_provider.time())
        if len(merged) < self.config.lookback_period:
            self.logger().warning(
                f"Not enough aligned candle data for analysis. "
                f"Required: {self.config.lookback_period}, Available: {len(merged)}")
            return None, None

        # Use the most recent aligned data points
        merged = merged.tail(self.config.lookback_period)
        dominant_prices_np = merged["close_dominant"].to_numpy(dtype=float)
        hedge_prices_np = merged["close_hedge"].to_numpy(dtype=float)

        # Calculate percentage returns
        dominant_pct_change = np.diff(dominant_prices_np) / dominant_prices_np[:-1]
        hedge_pct_change = np.diff(hedge_prices_np) / hedge_prices_np[:-1]

        # Convert to cumulative returns
        dominant_cum_returns = np.cumprod(dominant_pct_change + 1)
        hedge_cum_returns = np.cumprod(hedge_pct_change + 1)

        # Normalize to start at 1
        dominant_cum_returns = dominant_cum_returns / dominant_cum_returns[0] if len(dominant_cum_returns) > 0 else np.array([1.0])
        hedge_cum_returns = hedge_cum_returns / hedge_cum_returns[0] if len(hedge_cum_returns) > 0 else np.array([1.0])

        # Perform linear regression
        dominant_cum_returns_reshaped = dominant_cum_returns.reshape(-1, 1)
        reg = LinearRegression().fit(dominant_cum_returns_reshaped, hedge_cum_returns)
        alpha = reg.intercept_
        beta = reg.coef_[0]
        self.processed_data.update({
            "alpha": alpha,
            "beta": beta,
        })

        # Calculate spread as percentage difference from predicted value
        y_pred = alpha + beta * dominant_cum_returns
        spread_pct = (hedge_cum_returns - y_pred) / y_pred * 100

        # Calculate z-score
        mean_spread = np.mean(spread_pct)
        std_spread = np.std(spread_pct)
        if std_spread == 0:
            self.logger().warning("Standard deviation of spread is zero, cannot calculate z-score")
            return None, None

        current_spread = spread_pct[-1]
        current_z_score = (current_spread - mean_spread) / std_spread
        if not (np.isfinite(current_spread) and np.isfinite(current_z_score)):
            self.logger().warning("Non-finite spread/z-score; signal unavailable.")
            return None, None

        return current_spread, current_z_score

    def get_pairs_prices(self):
        current_dominant_price = self.market_data_provider.get_price_by_type(
            connector_name=self.config.connector_pair_dominant.connector_name,
            trading_pair=self.config.connector_pair_dominant.trading_pair, price_type=PriceType.MidPrice)

        current_hedge_price = self.market_data_provider.get_price_by_type(
            connector_name=self.config.connector_pair_hedge.connector_name,
            trading_pair=self.config.connector_pair_hedge.trading_pair, price_type=PriceType.MidPrice)
        return current_dominant_price, current_hedge_price

    def get_executors_dominant(self):
        active_executors_dominant_placed = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: e.connector_name == self.config.connector_pair_dominant.connector_name and e.trading_pair == self.config.connector_pair_dominant.trading_pair and e.is_active and not e.is_trading and e.type == "position_executor"
        )
        active_executors_dominant_filled = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: e.connector_name == self.config.connector_pair_dominant.connector_name and e.trading_pair == self.config.connector_pair_dominant.trading_pair and e.is_active and e.is_trading and e.type == "position_executor"
        )
        return active_executors_dominant_placed, active_executors_dominant_filled

    def get_executors_hedge(self):
        active_executors_hedge_placed = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: e.connector_name == self.config.connector_pair_hedge.connector_name and e.trading_pair == self.config.connector_pair_hedge.trading_pair and e.is_active and not e.is_trading and e.type == "position_executor"
        )
        active_executors_hedge_filled = self.filter_executors(
            self.executors_info,
            filter_func=lambda e: e.connector_name == self.config.connector_pair_hedge.connector_name and e.trading_pair == self.config.connector_pair_hedge.trading_pair and e.is_active and e.is_trading and e.type == "position_executor"
        )
        return active_executors_hedge_placed, active_executors_hedge_filled

    def to_format_status(self) -> List[str]:
        """
        Format the status of the controller for display.
        """
        status_lines = []
        status_lines.append(f"""
Dominant Pair: {self.config.connector_pair_dominant} | Hedge Pair: {self.config.connector_pair_hedge} |
Timeframe: {self.config.interval} | Lookback Period: {self.config.lookback_period} | Entry Threshold: {self.config.entry_threshold}

Positions targets:
Theoretical Dominant         : {self.theoretical_dominant_quote} | Theoretical Hedge: {self.theoretical_hedge_quote} | Position Hedge Ratio: {self.config.pos_hedge_ratio}
Position Dominant            : {self.processed_data['position_dominant_quote']:.2f} | Position Hedge: {self.processed_data['position_hedge_quote']:.2f} | Imbalance: {self.processed_data['imbalance']:.2f} | Imbalance Scaled: {self.processed_data['imbalance_scaled_pct']:.2f} %

Current Executors:
Active Orders Dominant       : {len(self.processed_data['executors_dominant_placed'])} | Active Orders Hedge       : {len(self.processed_data['executors_hedge_placed'])} |
Active Orders Dominant Filled: {len(self.processed_data['executors_dominant_filled'])} | Active Orders Hedge Filled: {len(self.processed_data['executors_hedge_filled'])}

Signal: {self.processed_data['signal']:.2f} | Z-Score: {self.processed_data['z_score']:.2f} | Spread: {self.processed_data['spread']:.2f}
Alpha : {self.processed_data['alpha']:.2f} | Beta: {self.processed_data['beta']:.2f}
Pair PnL PCT: {self.processed_data['pair_pnl_pct'] * 100:.2f} %
""")
        cooldown_remaining = self._stop_out_until - self.market_data_provider.time()
        if cooldown_remaining > 0:
            status_lines.append(
                f"Stop-out cooldown ACTIVE: re-entry gated for another {cooldown_remaining:.0f}s\n")
        return status_lines

    def get_candles_config(self) -> List[CandlesConfig]:
        max_records = self.config.lookback_period + 20
        return [
            CandlesConfig(
                connector=self.config.connector_pair_dominant.connector_name,
                trading_pair=self.config.connector_pair_dominant.trading_pair,
                interval=self.config.interval,
                max_records=max_records
            ),
            CandlesConfig(
                connector=self.config.connector_pair_hedge.connector_name,
                trading_pair=self.config.connector_pair_hedge.trading_pair,
                interval=self.config.interval,
                max_records=max_records
            )
        ]
