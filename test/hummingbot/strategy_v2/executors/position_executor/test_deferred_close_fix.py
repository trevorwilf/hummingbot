"""Tests for Fix 3: Spot close path -- deferred close after cancel."""
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import MagicMock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.event.events import OrderCancelledEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.executors.position_executor.position_executor import PositionExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class TestDeferredCloseFix(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.strategy = self._create_mock_strategy()

    def _create_mock_strategy(self):
        strategy = MagicMock(spec=StrategyV2Base)
        type(strategy).current_timestamp = PropertyMock(return_value=1234567890.0)
        strategy.buy.side_effect = ["OID-BUY-1", "OID-BUY-2", "OID-BUY-3"]
        strategy.sell.side_effect = ["OID-SELL-1", "OID-SELL-2", "OID-SELL-3"]
        strategy.cancel.return_value = None

        connector_mock = MagicMock(spec=ExchangePyBase)
        type(connector_mock).available_balances = PropertyMock(
            return_value={"ETH": Decimal("1000"), "USDT": Decimal("100000")})
        connector_mock.get_price_by_type.return_value = Decimal("100")
        connector_mock.quantize_order_amount.side_effect = lambda trading_pair, amount, **kw: amount
        strategy.connectors = {"binance": connector_mock}
        return strategy

    def _make_config(self, config_id="test-deferred"):
        return PositionExecutorConfig(
            id=config_id,
            timestamp=1234567890,
            trading_pair="ETH-USDT",
            connector_name="binance",
            side=TradeType.BUY,
            entry_price=Decimal("100"),
            amount=Decimal("1"),
            triple_barrier_config=TripleBarrierConfig(
                stop_loss=Decimal("0.05"),
                take_profit=Decimal("0.1"),
                time_limit=60,
                take_profit_order_type=OrderType.LIMIT,
                stop_loss_order_type=OrderType.MARKET,
            ),
        )

    @patch.object(PositionExecutor, "get_trading_rules")
    @patch.object(PositionExecutor, "get_price")
    def _make_executor(self, mock_price, mock_rules, config_id="test-deferred", perpetual=False):
        mock_price.return_value = Decimal("100")
        rules = MagicMock(spec=TradingRule)
        rules.min_order_size = Decimal("0.001")
        rules.min_notional_size = Decimal("1")
        mock_rules.return_value = rules
        config = self._make_config(config_id=config_id)
        executor = PositionExecutor(self.strategy, config)
        executor._status = RunnableStatus.RUNNING
        if perpetual:
            # Make is_perpetual return True
            executor.is_perpetual_connector = MagicMock(return_value=True)
        return executor

    def _make_open_in_flight_order(self, order_id, is_open=True):
        """Create a mock InFlightOrder that looks open."""
        order = MagicMock(spec=InFlightOrder)
        order.is_open = is_open
        order.is_done = not is_open
        order.is_filled = not is_open
        order.client_order_id = order_id
        return order

    def _simulate_filled_open_order(self, executor):
        """Set up executor as if the open order was filled with 1 ETH."""
        open_order = TrackedOrder(order_id="OPEN-1")
        open_ifo = MagicMock(spec=InFlightOrder)
        open_ifo.is_open = False
        open_ifo.is_done = True
        open_ifo.is_filled = True
        open_ifo.executed_amount_base = Decimal("1")
        open_ifo.average_executed_price = Decimal("100")
        open_ifo.order_fills = {}  # empty fills dict -- fee_asset falls through
        open_ifo.cumulative_fee_paid.return_value = Decimal("0")
        open_order.order = open_ifo
        executor._open_order = open_order

    def test_close_defers_when_tp_order_active(self):
        """When a TP limit order is active on spot, close should be deferred."""
        executor = self._make_executor(config_id="test-defer-1")
        self._simulate_filled_open_order(executor)

        # Set up an active TP limit order
        tp_order = TrackedOrder(order_id="TP-1")
        tp_order.order = self._make_open_in_flight_order("TP-1", is_open=True)
        executor._take_profit_limit_order = tp_order

        executor.place_close_order_and_cancel_open_orders(CloseType.STOP_LOSS)

        # Should be deferred -- no close order placed yet
        self.assertTrue(executor._pending_close_after_cancel)
        self.assertIsNone(executor._close_order)
        self.assertEqual(executor.close_type, CloseType.STOP_LOSS)
        self.assertEqual(executor._status, RunnableStatus.SHUTTING_DOWN)

    def test_close_places_after_cancel_confirmed(self):
        """After deferred close, cancel event should trigger actual close order placement."""
        executor = self._make_executor(config_id="test-defer-2")
        self._simulate_filled_open_order(executor)

        # Set up an active TP limit order
        tp_order = TrackedOrder(order_id="TP-2")
        tp_order.order = self._make_open_in_flight_order("TP-2", is_open=True)
        executor._take_profit_limit_order = tp_order

        executor.place_close_order_and_cancel_open_orders(CloseType.TIME_LIMIT)
        self.assertTrue(executor._pending_close_after_cancel)
        self.assertIsNone(executor._close_order)

        # Simulate the cancel event for the TP order
        cancel_event = OrderCancelledEvent(
            timestamp=1234567890,
            order_id="TP-2",
        )
        executor.process_order_canceled_event(None, None, cancel_event)

        # Now the close order should be placed
        self.assertFalse(executor._pending_close_after_cancel)
        self.assertIsNotNone(executor._close_order)

    def test_close_immediate_when_no_orders_to_cancel(self):
        """When there are no open orders to cancel, close should happen immediately."""
        executor = self._make_executor(config_id="test-defer-3")
        self._simulate_filled_open_order(executor)

        # No TP order, no open order with is_open=True
        executor.place_close_order_and_cancel_open_orders(CloseType.STOP_LOSS)

        # Should NOT be deferred
        self.assertFalse(executor._pending_close_after_cancel)
        self.assertIsNotNone(executor._close_order)

    def test_close_immediate_for_perpetual(self):
        """For perpetual connectors, close should always be immediate even with active orders."""
        executor = self._make_executor(config_id="test-defer-4", perpetual=True)
        self._simulate_filled_open_order(executor)

        # Set up an active TP limit order
        tp_order = TrackedOrder(order_id="TP-PERP")
        tp_order.order = self._make_open_in_flight_order("TP-PERP", is_open=True)
        executor._take_profit_limit_order = tp_order

        executor.place_close_order_and_cancel_open_orders(CloseType.STOP_LOSS)

        # Should NOT be deferred for perpetual
        self.assertFalse(executor._pending_close_after_cancel)
        self.assertIsNotNone(executor._close_order)

    @patch.object(PositionExecutor, "get_price")
    async def test_deferred_close_timeout_fires(self, mock_price):
        """If cancel is not confirmed within 15s, timeout should force close order."""
        mock_price.return_value = Decimal("100")
        executor = self._make_executor(config_id="test-defer-5")
        self._simulate_filled_open_order(executor)

        # Set up an active TP limit order
        tp_order = TrackedOrder(order_id="TP-TIMEOUT")
        tp_order.order = self._make_open_in_flight_order("TP-TIMEOUT", is_open=True)
        executor._take_profit_limit_order = tp_order

        executor.place_close_order_and_cancel_open_orders(CloseType.TIME_LIMIT)
        self.assertTrue(executor._pending_close_after_cancel)
        self.assertIsNone(executor._close_order)

        # Advance time by 16 seconds
        type(self.strategy).current_timestamp = PropertyMock(return_value=1234567890.0 + 16.0)

        # Set status to SHUTTING_DOWN and call control_shutdown_process
        executor._status = RunnableStatus.SHUTTING_DOWN
        await executor.control_shutdown_process()

        # Timeout should have triggered close order placement
        self.assertFalse(executor._pending_close_after_cancel)
        self.assertIsNotNone(executor._close_order)
