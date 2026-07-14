import asyncio
import logging
from decimal import Decimal
from typing import Dict, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate, PerpetualOrderCandidate
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class OrderExecutor(ExecutorBase):
    _logger = None

    # Watchdog for a shutdown that never completes (lost cancel confirmation, or an
    # order stuck in a pending state that matches no shutdown branch).
    _SHUTDOWN_TIMEOUT_S = 30.0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, strategy: StrategyV2Base, config: OrderExecutorConfig,
                 update_interval: float = 1.0, max_retries: int = 10):
        """
        Initialize the OrderExecutor instance.

        :param strategy: The strategy to be used by the OrderExecutor.
        :param config: The configuration for the OrderExecutor.
        :param update_interval: The interval at which the OrderExecutor should be updated, defaults to 1.0.
        :param max_retries: The maximum number of retries for the OrderExecutor, defaults to 10.
        """
        super().__init__(strategy=strategy, config=config, connectors=[config.connector_name],
                         update_interval=update_interval)
        self.config: OrderExecutorConfig = config

        # Order tracking
        self._order: Optional[TrackedOrder] = None
        self._failed_orders: list[TrackedOrder] = []
        self._canceled_orders: list[TrackedOrder] = []
        self._partial_filled_orders: list[TrackedOrder] = []
        self._current_retries = 0
        self._max_retries = max_retries
        self._shutdown_start_timestamp: Optional[float] = None

    @property
    def current_market_price(self) -> Decimal:
        """
        Get the current market price based on the order side.

        :return: The current market price.
        """
        price_type = PriceType.BestBid if self.config.side == TradeType.BUY else PriceType.BestAsk
        return self.get_price(self.config.connector_name, self.config.trading_pair, price_type=price_type)

    async def control_task(self):
        """
        Control the order execution process based on the execution strategy.
        """
        if self.status == RunnableStatus.RUNNING:
            self.control_order()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            await self.control_shutdown_process()
        self.evaluate_max_retries()

    def control_order(self):
        """
        Control the order based on the execution strategy.
        """
        if not self._order:
            self.place_open_order()
        elif self.config.execution_strategy == ExecutionStrategy.LIMIT_CHASER:
            self.control_limit_chaser()

    def control_limit_chaser(self):
        """
        Control the limit chaser strategy by updating the order price based on market conditions.
        The distance is treated as a percentage of the current market price.
        """
        if not self._order or not self._order.order or not self._order.order.is_open:
            return

        current_price = self.current_market_price
        threshold = self.config.chaser_config.refresh_threshold

        if self.config.side == TradeType.BUY:
            if current_price - self._order.order.price > (current_price * threshold):
                self.renew_order()
        else:  # SELL
            if self._order.order.price - current_price > (current_price * threshold):
                self.renew_order()

    def early_stop(self, keep_position: bool = True):
        """
        This method allows strategy to stop the executor early.

        :return: None
        """
        self._status = RunnableStatus.SHUTTING_DOWN

    async def control_shutdown_process(self):
        """
        Control the shutdown process of the executor.
        """
        if self._shutdown_start_timestamp is None:
            self._shutdown_start_timestamp = self._strategy.current_timestamp
        if self._order:
            if self._order.is_open:
                self.cancel_order()
            elif self._order.is_filled:
                self.close_type = CloseType.POSITION_HOLD
                self._held_position_orders.append(self._order_json_safe(self._order))
                self._held_position_orders.extend([self._order_json_safe(order) for order in self._partial_filled_orders])
                self.stop()
        else:
            if self._partial_filled_orders:
                self._held_position_orders.extend([self._order_json_safe(order) for order in self._partial_filled_orders])
                self.close_type = CloseType.POSITION_HOLD
            else:
                self.close_type = CloseType.EARLY_STOP
            self.stop()
        if self.status == RunnableStatus.SHUTTING_DOWN and \
                self._strategy.current_timestamp - self._shutdown_start_timestamp > self._SHUTDOWN_TIMEOUT_S:
            self._force_terminate_shutdown()
        await self._sleep(5.0)

    def _force_terminate_shutdown(self):
        """
        Terminate a shutdown that did not complete within _SHUTDOWN_TIMEOUT_S: the cancel
        confirmation was lost, or the order is wedged in a pending state. Filled inventory
        is recorded as POSITION_HOLD; otherwise the executor terminates FAILED. The order
        may still be live on the exchange either way.
        """
        order_id = self._order.order_id if self._order else None
        order_has_fills = self._order is not None and \
            (self._order.is_filled or self._order.executed_amount_base > Decimal("0"))
        if order_has_fills or self._partial_filled_orders:
            self.close_type = CloseType.POSITION_HOLD
            if order_has_fills:
                self._held_position_orders.append(self._order_json_safe(self._order))
            self._held_position_orders.extend([self._order_json_safe(order) for order in self._partial_filled_orders])
        else:
            self.close_type = CloseType.FAILED
        self.logger().warning(
            f"Executor {self.config.id} ({self.config.trading_pair} on {self.config.connector_name}): shutdown did "
            f"not complete within {self._SHUTDOWN_TIMEOUT_S:.0f}s — order {order_id} may still be live on the "
            f"exchange. Force-terminating as {self.close_type.name}."
        )
        self.stop()

    def _order_json_safe(self, tracked: TrackedOrder) -> Dict:
        """
        A TrackedOrder whose InFlightOrder was evicted by the connector has order=None;
        fall back to a minimal record instead of raising inside event handlers / shutdown.
        """
        try:
            if tracked.order is not None:
                return tracked.order.to_json()
        except Exception:
            pass
        return {
            "client_order_id": tracked.order_id,
            "exchange_order_id": None,
            "trading_pair": self.config.trading_pair,
            "trade_type": self.config.side.name,
            "price": str(self.config.price),
            "amount": str(self.config.amount),
            "executed_amount_base": str(tracked.executed_amount_base),
            "executed_amount_quote": str(tracked.executed_amount_quote),
        }

    def evaluate_max_retries(self):
        """
        Evaluate if the maximum number of retries has been reached.
        """
        if self._current_retries > self._max_retries:
            self.stop()

    def place_open_order(self):
        """
        Place the order based on the execution strategy.
        """
        order_id = self.place_order(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            order_type=self.get_order_type(),
            amount=self.config.amount,
            price=self.get_order_price(),
            side=self.config.side,
            position_action=self.config.position_action,
        )
        self._order = TrackedOrder(order_id=order_id)
        self.logger().debug(f"Executor ID: {self.config.id} - Placing order {order_id}")

    def get_order_type(self) -> OrderType:
        """
        Get the order type based on the execution strategy.

        :return: The order type.
        """
        if self.config.execution_strategy == ExecutionStrategy.MARKET:
            return OrderType.MARKET
        elif self.config.execution_strategy in [ExecutionStrategy.LIMIT_MAKER, ExecutionStrategy.LIMIT_CHASER]:
            return OrderType.LIMIT_MAKER
        else:
            return OrderType.LIMIT

    def get_order_price(self) -> Decimal:
        """
        Get the order price based on the execution strategy.

        :return: The order price.
        """
        if self.config.execution_strategy == ExecutionStrategy.MARKET:
            return Decimal("NaN")
        elif self.config.execution_strategy == ExecutionStrategy.LIMIT_CHASER:
            if self.config.side == TradeType.BUY:
                return self.current_market_price * (Decimal("1") - self.config.chaser_config.distance)
            else:
                return self.current_market_price * (Decimal("1") + self.config.chaser_config.distance)
        elif self.config.execution_strategy == ExecutionStrategy.LIMIT_MAKER:
            if self.config.side == TradeType.BUY:
                return min(self.config.price, self.current_market_price)
            else:
                return max(self.config.price, self.current_market_price)
        else:
            return self.config.price

    def get_price_for_balance_validation(self) -> Decimal:
        """
        Get the price to use for balance validation.
        For MARKET orders, uses current market price since NaN cannot be used in calculations.

        :return: The price for balance validation.
        """
        if self.config.execution_strategy == ExecutionStrategy.MARKET:
            return self.current_market_price
        return self.get_order_price()

    def renew_order(self):
        """
        Renew the order with a new price.

        :param new_price: The new price for the order.
        """
        self.cancel_order()
        self.place_open_order()
        self.logger().debug("Renewing order")

    def cancel_order(self):
        """
        Cancel the current order.
        """
        if self._order and self._order.order and self._order.order.is_open:
            self._strategy.cancel(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                order_id=self._order.order_id
            )
            self.logger().debug("Cancelling order")

    def update_tracked_order_with_order_id(self, order_id: str):
        """
        Update the tracked order with the information from the InFlightOrder.

        :param order_id: The order ID to update.
        """
        in_flight_order = self.get_in_flight_order(self.config.connector_name, order_id)
        if self._order and self._order.order_id == order_id:
            self._order.order = in_flight_order

    def process_order_created_event(self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        """
        Process the order created event.
        """
        self.update_tracked_order_with_order_id(event.order_id)

    def process_order_filled_event(self, _, market, event: OrderFilledEvent):
        """
        Process the order filled event.
        """
        self.update_tracked_order_with_order_id(event.order_id)

    def process_order_completed_event(self, _, market, event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        """
        Process the order completed event.
        """
        self.update_tracked_order_with_order_id(event.order_id)
        if self._order and self._order.order_id == event.order_id:
            self._held_position_orders.append(self._order_json_safe(self._order))
            self.close_type = CloseType.POSITION_HOLD
            self.stop()

    def process_order_canceled_event(self, _, market: ConnectorBase, event: OrderCancelledEvent):
        """
        Process the order canceled event.
        """
        if self._order and event.order_id == self._order.order_id:
            if self._order.executed_amount_base > Decimal("0"):
                self._partial_filled_orders.append(self._order)
            else:
                self._canceled_orders.append(self._order)
            self._order = None

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        """
        Process the order failed event.
        """
        if self._order and event.order_id == self._order.order_id:
            self._failed_orders.append(self._order)
            self._order = None
            self.logger().error(f"Order failed {event.order_id}. Retrying {self._current_retries}/{self._max_retries}")
            self._current_retries += 1

    def _aggregate_fills(self):
        """Sum executed base/quote and quote fees across this executor's fill-bearing orders.

        The executor places one order at a time. ``self._order`` is retained on a full fill
        (``process_order_completed_event`` sets ``POSITION_HOLD`` without clearing it), and
        ``self._partial_filled_orders`` holds orders cancelled after a partial fill. Cancelled
        and failed orders carry no fills, so they are intentionally excluded.
        """
        tracked = []
        if self._order is not None:
            tracked.append(self._order)
        tracked.extend(self._partial_filled_orders)

        filled_base = Decimal("0")
        filled_quote = Decimal("0")
        cum_fees_quote = Decimal("0")
        for t in tracked:
            try:
                filled_base += Decimal(str(t.executed_amount_base or 0))
                filled_quote += Decimal(str(t.executed_amount_quote or 0))
                cum_fees_quote += Decimal(str(t.cum_fees_quote or 0))
            except Exception:
                # A tracked order without an underlying InFlightOrder yet; skip it.
                continue
        return filled_base, filled_quote, cum_fees_quote

    def get_custom_info(self) -> Dict:
        """
        Get custom information about the executor.

        :return: A dictionary containing custom information.
        """
        filled_base, filled_quote, cum_fees_quote = self._aggregate_fills()
        return {
            "level_id": self.config.level_id,
            "current_retries": self._current_retries,
            "max_retries": self._max_retries,
            "order_id": self._order.order_id if self._order else None,
            "order_last_update": self._order.last_update_timestamp if self._order else None,
            "held_position_orders": self._held_position_orders,
            # --- exact, controller-consumable fill accounting ---
            # These are stored by the orchestrator but NOT aggregated into performance
            # metrics (volume_traded / global_pnl), so exposing real fills here is safe
            # while the public filled_amount_quote property stays 0 for POSITION_HOLD.
            "side": self.config.side,
            "filled_amount_base": filled_base,
            "filled_amount_quote": filled_quote,
            "cum_fees_quote": cum_fees_quote,
        }

    def to_format_status(self, scale=1.0):
        """
        Format the status of the executor.

        :param scale: The scale for formatting.
        :return: A list of formatted status lines.
        """
        lines = [f"""
| Trading Pair: {self.config.trading_pair} | Exchange: {self.config.connector_name} | Action: {self.config.position_action}
| Amount: {self.config.amount} | Price: {self._order.order.price if self._order and self._order.order else 'N/A'}
| Execution Strategy: {self.config.execution_strategy} | Retries: {self._current_retries}/{self._max_retries}
"""]
        return lines

    async def validate_sufficient_balance(self):
        price_for_validation = self.get_price_for_balance_validation()
        if self.is_perpetual_connector(self.config.connector_name):
            order_candidate = PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.get_order_type().is_limit_type(),
                order_type=self.get_order_type(),
                order_side=self.config.side,
                amount=self.config.amount,
                price=price_for_validation,
                leverage=Decimal(self.config.leverage),
            )
        else:
            order_candidate = OrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.get_order_type().is_limit_type(),
                order_type=self.get_order_type(),
                order_side=self.config.side,
                amount=self.config.amount,
                price=price_for_validation,
            )
        adjusted_order_candidates = self.adjust_order_candidates(self.config.connector_name, [order_candidate])
        if adjusted_order_candidates[0].amount == Decimal("0"):
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            self.logger().error("Not enough budget to open position.")
            self.stop()

    async def _sleep(self, delay: float):
        """
        Sleep for a specified delay.

        :param delay: The delay in seconds.
        """
        await asyncio.sleep(delay)

    def get_net_pnl_pct(self) -> Decimal:
        """
        Get the net profit and loss percentage.

        :return: The net profit and loss percentage.
        """
        return Decimal("0")

    def get_net_pnl_quote(self) -> Decimal:
        """
        Get the net profit and loss in quote currency.

        :return: The net profit and loss in quote currency.
        """
        return Decimal("0")

    def get_cum_fees_quote(self) -> Decimal:
        """
        Get the cumulative fees in quote currency.

        :return: The cumulative fees in quote currency.
        """
        return Decimal("0")
