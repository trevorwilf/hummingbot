"""Tests for Fix 2: PositionExecutor take-profit retry increment."""
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import MagicMock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.events import MarketOrderFailureEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.executors.position_executor.position_executor import PositionExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class TestTakeProfitRetryFix(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.strategy = self._create_mock_strategy()

    def _create_mock_strategy(self):
        strategy = MagicMock(spec=StrategyV2Base)
        type(strategy).current_timestamp = PropertyMock(return_value=1234567890)
        strategy.buy.side_effect = lambda **kw: "OID-BUY-" + str(id(kw))
        strategy.sell.side_effect = lambda **kw: "OID-SELL-" + str(id(kw))
        strategy.cancel.return_value = None

        connector_mock = MagicMock(spec=ExchangePyBase)
        type(connector_mock).available_balances = PropertyMock(
            return_value={"ETH": Decimal("1000"), "USDT": Decimal("100000")})
        connector_mock.get_price_by_type.return_value = Decimal("100")
        connector_mock.quantize_order_amount.side_effect = lambda pair, amt: amt
        strategy.connectors = {"binance": connector_mock}
        return strategy

    def _make_config(self):
        return PositionExecutorConfig(
            id="test-tp-retry",
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
    def _make_executor(self, mock_price, mock_rules, max_retries=10):
        mock_price.return_value = Decimal("100")
        rules = MagicMock(spec=TradingRule)
        rules.min_order_size = Decimal("0.001")
        rules.min_notional_size = Decimal("1")
        mock_rules.return_value = rules
        config = self._make_config()
        executor = PositionExecutor(self.strategy, config, max_retries=max_retries)
        executor._status = RunnableStatus.RUNNING
        return executor

    def _fire_failure_event(self, executor, order_id):
        event = MarketOrderFailureEvent(
            timestamp=1234567890,
            order_id=order_id,
            order_type=OrderType.LIMIT,
        )
        executor.process_order_failed_event(None, None, event)

    def test_take_profit_failure_increments_retry_counter(self):
        executor = self._make_executor()
        self.assertEqual(executor._current_retries, 0)

        # Simulate a TP limit order being placed
        executor._take_profit_limit_order = TrackedOrder(order_id="TP-ORDER-1")

        self._fire_failure_event(executor, "TP-ORDER-1")

        self.assertEqual(executor._current_retries, 1)
        # TP order should be cleared
        self.assertIsNone(executor._take_profit_limit_order)

    def test_take_profit_failures_stop_after_max_retries(self):
        executor = self._make_executor(max_retries=3)
        self.assertEqual(executor._current_retries, 0)

        for i in range(4):
            order_id = f"TP-ORDER-{i}"
            executor._take_profit_limit_order = TrackedOrder(order_id=order_id)
            self._fire_failure_event(executor, order_id)
            executor.evaluate_max_retries()

        # After 4 failures with max_retries=3, should be FAILED
        self.assertEqual(executor._current_retries, 4)
        self.assertEqual(executor.close_type, CloseType.FAILED)

    def test_open_order_failure_still_increments(self):
        executor = self._make_executor()
        executor._open_order = TrackedOrder(order_id="OPEN-ORDER-1")

        self._fire_failure_event(executor, "OPEN-ORDER-1")

        self.assertEqual(executor._current_retries, 1)
        self.assertIsNone(executor._open_order)

    def test_close_order_failure_still_increments(self):
        executor = self._make_executor()
        executor._close_order = TrackedOrder(order_id="CLOSE-ORDER-1")

        self._fire_failure_event(executor, "CLOSE-ORDER-1")

        self.assertEqual(executor._current_retries, 1)
        self.assertIsNone(executor._close_order)

    def test_retry_counter_shared_across_order_types(self):
        executor = self._make_executor()

        # 1 open failure
        executor._open_order = TrackedOrder(order_id="OPEN-1")
        self._fire_failure_event(executor, "OPEN-1")
        self.assertEqual(executor._current_retries, 1)

        # 1 TP failure
        executor._take_profit_limit_order = TrackedOrder(order_id="TP-1")
        self._fire_failure_event(executor, "TP-1")
        self.assertEqual(executor._current_retries, 2)

        # 1 close failure
        executor._close_order = TrackedOrder(order_id="CLOSE-1")
        self._fire_failure_event(executor, "CLOSE-1")
        self.assertEqual(executor._current_retries, 3)
