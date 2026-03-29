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
        strategy.buy.side_effect = lambda *a, **kw: "OID-BUY-" + str(id(kw))
        strategy.sell.side_effect = lambda *a, **kw: "OID-SELL-" + str(id(kw))
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
        """With max_retries=3, exactly 3 failures should trigger FAILED (Fix 2: >= not >)."""
        executor = self._make_executor(max_retries=3)
        self.assertEqual(executor._current_retries, 0)

        for i in range(3):
            order_id = f"TP-ORDER-{i}"
            executor._take_profit_limit_order = TrackedOrder(order_id=order_id)
            self._fire_failure_event(executor, order_id)
            executor.evaluate_max_retries()

        # After 3 failures with max_retries=3, should be FAILED (>= not >)
        self.assertEqual(executor._current_retries, 3)
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

    # --- Fix 2 (RCA): TP limit order balance capping ---

    def _setup_tp_executor(self, available_base, amount_to_close=Decimal("1.0")):
        """Helper to create executor ready for TP limit sell test."""
        executor = self._make_executor()
        executor._status = RunnableStatus.RUNNING
        type(executor).open_filled_amount = PropertyMock(return_value=amount_to_close)
        type(executor).amount_to_close = PropertyMock(return_value=amount_to_close)
        type(executor).close_order_side = PropertyMock(return_value=TradeType.SELL)
        type(executor).is_perpetual = PropertyMock(return_value=False)
        type(executor).take_profit_price = PropertyMock(return_value=Decimal("110"))
        # Update the PropertyMock return value on the connector type
        connector = self.strategy.connectors["binance"]
        type(connector).available_balances = PropertyMock(
            return_value={"ETH": available_base, "USDT": Decimal("100000")})
        return executor

    def test_tp_limit_sell_capped_to_available_base(self):
        """Available base=0.5 with amount_to_close=1.0 => order placed with 0.5."""
        executor = self._setup_tp_executor(available_base=Decimal("0.5"))
        executor.place_take_profit_limit_order()

        self.assertIsNotNone(executor._take_profit_limit_order)
        self.strategy.sell.assert_called_once()
        # place_order calls strategy.sell(connector, pair, amount, ...) — amount is arg[2]
        call_args = self.strategy.sell.call_args[0]
        self.assertEqual(Decimal("0.5"), call_args[2])

    def test_tp_limit_sell_deferred_when_zero_balance(self):
        """Available base=0 => no order placed, no crash."""
        executor = self._setup_tp_executor(available_base=Decimal("0"))
        executor.place_take_profit_limit_order()

        self.assertIsNone(executor._take_profit_limit_order)
        self.strategy.sell.assert_not_called()

    def test_tp_limit_sell_below_min_order_skips(self):
        """Available base=0.0001 with min_order_size=0.001 => no order placed."""
        executor = self._setup_tp_executor(available_base=Decimal("0.0001"))
        executor.place_take_profit_limit_order()

        self.assertIsNone(executor._take_profit_limit_order)
        self.strategy.sell.assert_not_called()

    def test_tp_limit_full_balance_no_cap(self):
        """Available base=5.0 with amount_to_close=1.0 => order placed with full 1.0."""
        executor = self._setup_tp_executor(available_base=Decimal("5.0"))
        executor.place_take_profit_limit_order()

        self.assertIsNotNone(executor._take_profit_limit_order)
        call_args = self.strategy.sell.call_args[0]
        self.assertEqual(Decimal("1.0"), call_args[2])

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

    # --- Phase 1 Fix 1: Terminal failure classification ---

    def _fire_failure_with_message(self, executor, order_id, error_message):
        """Fire a MarketOrderFailureEvent with error_message set."""
        event = MarketOrderFailureEvent(
            timestamp=1234567890,
            order_id=order_id,
            order_type=OrderType.LIMIT,
            error_message=error_message,
        )
        executor.process_order_failed_event(None, None, event)

    def test_terminal_failure_not_retried(self):
        """Test 1: Min-notional terminal failure sets retries to max, no further retries."""
        executor = self._make_executor(max_retries=10)
        executor._open_order = TrackedOrder(order_id="OPEN-TERM-1")

        self._fire_failure_with_message(
            executor, "OPEN-TERM-1",
            "Order notional 0.91 is lower than minimum notional size 1.2 for the pair ARRR-USDT"
        )

        # Should be marked for shutdown immediately
        self.assertGreaterEqual(executor._current_retries, executor._max_retries)
        self.assertIsNotNone(executor._terminal_failure_reason)
        self.assertIn("notional", executor._terminal_failure_reason.lower())
        self.assertIsNone(executor._open_order)

    def test_transient_failure_is_retried(self):
        """Test 2: Connection timeout is a transient failure — normal retry."""
        executor = self._make_executor(max_retries=10)
        executor._open_order = TrackedOrder(order_id="OPEN-TRANS-1")

        self._fire_failure_with_message(
            executor, "OPEN-TRANS-1",
            "Connection timeout"
        )

        self.assertEqual(1, executor._current_retries)
        self.assertIsNone(executor._terminal_failure_reason)

    def test_exact_retry_count_no_off_by_one(self):
        """Test 3: With max_retries=3, exactly 3 transient failures stop the executor."""
        executor = self._make_executor(max_retries=3)

        for i in range(3):
            order_id = f"OPEN-RETRY-{i}"
            executor._open_order = TrackedOrder(order_id=order_id)
            self._fire_failure_with_message(executor, order_id, "Connection timeout")

        self.assertEqual(3, executor._current_retries)
        executor.evaluate_max_retries()
        self.assertEqual(CloseType.FAILED, executor.close_type)

        # Verify NO order placement occurs after 3rd failure
        # (control_open_order should not run because evaluate_max_retries runs first)

    def test_terminal_failure_on_close_order(self):
        """Terminal failure on close order sets terminal reason."""
        executor = self._make_executor()
        executor._close_order = TrackedOrder(order_id="CLOSE-TERM-1")

        self._fire_failure_with_message(
            executor, "CLOSE-TERM-1",
            "Order amount 0.0001 is lower than minimum order size 0.001"
        )

        self.assertGreaterEqual(executor._current_retries, executor._max_retries)
        self.assertIsNotNone(executor._terminal_failure_reason)

    def test_terminal_failure_on_tp_order(self):
        """Terminal failure on take-profit order sets terminal reason."""
        executor = self._make_executor()
        executor._take_profit_limit_order = TrackedOrder(order_id="TP-TERM-1")

        self._fire_failure_with_message(
            executor, "TP-TERM-1",
            "LIMIT_MAKER is not in the list of supported order types"
        )

        self.assertGreaterEqual(executor._current_retries, executor._max_retries)
        self.assertIsNotNone(executor._terminal_failure_reason)

    def test_custom_info_includes_terminal_reason(self):
        """get_custom_info should include terminal_failure_reason."""
        executor = self._make_executor()
        info = executor.get_custom_info()
        self.assertIn("terminal_failure_reason", info)
        self.assertIsNone(info["terminal_failure_reason"])

        # Set it and verify
        executor._terminal_failure_reason = "Test reason"
        info = executor.get_custom_info()
        self.assertEqual("Test reason", info["terminal_failure_reason"])

    # --- Phase 1 Fix 4: Close order min-notional ---

    def test_close_order_respects_min_notional(self):
        """Test 4: Close amount above min_order_size but notional below min_notional => dust."""
        executor = self._make_executor()
        executor._status = RunnableStatus.RUNNING
        type(executor).open_filled_amount = PropertyMock(return_value=Decimal("0.005"))
        type(executor).close_filled_amount = PropertyMock(return_value=Decimal("0"))
        type(executor).amount_to_close = PropertyMock(return_value=Decimal("0.005"))
        type(executor).close_order_side = PropertyMock(return_value=TradeType.SELL)
        type(executor).is_perpetual = PropertyMock(return_value=False)
        executor.close_type = CloseType.TAKE_PROFIT

        # Set trading rules: min_order_size=0.001, min_notional=10
        executor.trading_rules.min_order_size = Decimal("0.001")
        executor.trading_rules.min_notional_size = Decimal("10")

        # Price ~100, so notional = 0.005 * 100 = 0.5 < 10 min_notional
        with patch.object(executor, 'get_price', return_value=Decimal("100")):
            executor._place_close_order_now(Decimal("100"))

        # No order placed
        self.assertIsNone(executor._close_order)
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertIsNotNone(executor._terminal_failure_reason)

    # --- Phase 1 Fix 4: TP min-notional ---

    def test_tp_respects_min_notional(self):
        """Test 5: TP order notional below min_notional_size => deferred."""
        executor = self._setup_tp_executor(available_base=Decimal("5.0"), amount_to_close=Decimal("0.005"))
        # Set min_notional_size high enough that 0.005 * 110 = 0.55 < 10
        executor.trading_rules.min_notional_size = Decimal("10")

        executor.place_take_profit_limit_order()

        self.assertIsNone(executor._take_profit_limit_order)
        self.strategy.sell.assert_not_called()
