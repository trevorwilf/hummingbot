"""Executor lifecycle safety tests (V2 strategy fixes phase 1 — findings A1, A4, B6-position, A2).

Under test:
- OrderExecutor shutdown watchdog: a lost cancel confirmation or an order wedged in a
  pending state can no longer pin the executor in SHUTTING_DOWN forever — after
  _SHUTDOWN_TIMEOUT_S it force-terminates (POSITION_HOLD when there are fills,
  FAILED otherwise) with a loud warning.
- OrderExecutor completed-event handling when the connector already evicted the
  InFlightOrder (TrackedOrder.order is None): no AttributeError, held inventory recorded.
- PositionExecutor enforces the retry ceiling in SHUTTING_DOWN, so a close order that
  can never fill terminates FAILED instead of retrying forever.
- ExecutorOrchestrator.stop(): a raising executor_info or a raising store cannot skip
  position/executor persistence.
"""
import asyncio
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.markets_recorder import MarketsRecorder
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.event.events import BuyOrderCompletedEvent
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator, PositionHold
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.order_executor.order_executor import OrderExecutor
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.position_executor import PositionExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def create_mock_strategy():
    market = MagicMock()
    market_info = MagicMock()
    market_info.market = market

    strategy = MagicMock(spec=StrategyV2Base)
    type(strategy).market_info = PropertyMock(return_value=market_info)
    type(strategy).trading_pair = PropertyMock(return_value="ETH-USDT")
    strategy.buy.side_effect = ["OID-BUY-1", "OID-BUY-2", "OID-BUY-3"]
    strategy.sell.side_effect = ["OID-SELL-1", "OID-SELL-2", "OID-SELL-3"]
    strategy.cancel.return_value = None
    connector = MagicMock(spec=ExchangePyBase)
    type(connector).trading_rules = PropertyMock(return_value={"ETH-USDT": TradingRule(trading_pair="ETH-USDT")})
    strategy.connectors = {
        "binance": connector,
    }
    strategy.market_data_provider = MagicMock(spec=MarketDataProvider)
    strategy.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal(230))
    strategy.controllers = {}
    strategy.markets = {"binance": {"ETH-USDT", "BTC-USDT"}}
    return strategy


def make_in_flight_order(order_id: str, state: OrderState) -> InFlightOrder:
    return InFlightOrder(
        client_order_id=order_id,
        trading_pair="ETH-USDT",
        order_type=OrderType.LIMIT,
        trade_type=TradeType.BUY,
        price=Decimal("100"),
        amount=Decimal("1"),
        creation_timestamp=1640001112.223,
        initial_state=state,
    )


class TestOrderExecutorShutdownWatchdog(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    def setUp(self) -> None:
        super().setUp()
        self.strategy = create_mock_strategy()
        self.strategy.current_timestamp = 1000.0

    def build_executor(self) -> OrderExecutor:
        config = OrderExecutorConfig(
            id="test",
            timestamp=123,
            side=TradeType.BUY,
            connector_name="binance",
            trading_pair="ETH-USDT",
            amount=Decimal("1"),
            price=Decimal("100"),
            execution_strategy=ExecutionStrategy.LIMIT,
        )
        executor = OrderExecutor(self.strategy, config, update_interval=0.5)
        self.set_loggers(loggers=[executor.logger()])
        executor._status = RunnableStatus.SHUTTING_DOWN
        return executor

    @patch.object(OrderExecutor, "_sleep", new_callable=AsyncMock)
    async def test_lost_cancel_confirmation_force_fails_after_timeout(self, _):
        executor = self.build_executor()
        executor._order = TrackedOrder("OID-OPEN")
        executor._order.order = make_in_flight_order("OID-OPEN", OrderState.OPEN)

        await executor.control_shutdown_process()
        # The cancel was (re-)issued and the executor keeps waiting inside the window.
        self.strategy.cancel.assert_called_once()
        self.assertEqual(RunnableStatus.SHUTTING_DOWN, executor.status)

        # The cancel confirmation never arrives; past the timeout the watchdog fires.
        self.strategy.current_timestamp = 1000.0 + OrderExecutor._SHUTDOWN_TIMEOUT_S + 1
        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertTrue(self.is_partially_logged("WARNING", "may still be live on the exchange"))

    @patch.object(OrderExecutor, "_sleep", new_callable=AsyncMock)
    async def test_lost_cancel_with_partial_fill_holds_inventory(self, _):
        executor = self.build_executor()
        order = make_in_flight_order("OID-PART", OrderState.PARTIALLY_FILLED)
        order.executed_amount_base = Decimal("0.4")
        order.executed_amount_quote = Decimal("40")
        executor._order = TrackedOrder("OID-PART")
        executor._order.order = order

        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.SHUTTING_DOWN, executor.status)

        self.strategy.current_timestamp = 1000.0 + OrderExecutor._SHUTDOWN_TIMEOUT_S + 1
        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual(CloseType.POSITION_HOLD, executor.close_type)
        self.assertEqual(1, len(executor._held_position_orders))
        self.assertEqual("0.4", executor._held_position_orders[0]["executed_amount_base"])
        self.assertTrue(self.is_partially_logged("WARNING", "may still be live on the exchange"))

    @patch.object(OrderExecutor, "_sleep", new_callable=AsyncMock)
    async def test_pending_state_fall_through_terminates(self, _):
        # A TrackedOrder that never got its InFlightOrder attached is neither is_open nor
        # is_filled: before the watchdog it matched NO shutdown branch and spun forever.
        executor = self.build_executor()
        executor._order = TrackedOrder("OID-PENDING")

        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.SHUTTING_DOWN, executor.status)

        self.strategy.current_timestamp = 1000.0 + OrderExecutor._SHUTDOWN_TIMEOUT_S + 1
        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual(CloseType.FAILED, executor.close_type)

    @patch.object(OrderExecutor, "_sleep", new_callable=AsyncMock)
    async def test_no_force_stop_within_timeout(self, _):
        executor = self.build_executor()
        executor._order = TrackedOrder("OID-PENDING")

        await executor.control_shutdown_process()
        self.strategy.current_timestamp = 1000.0 + OrderExecutor._SHUTDOWN_TIMEOUT_S - 1
        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.SHUTTING_DOWN, executor.status)

    @patch.object(OrderExecutor, "get_in_flight_order", MagicMock(return_value=None))
    def test_completed_event_with_evicted_order_does_not_raise(self):
        executor = self.build_executor()
        executor._status = RunnableStatus.RUNNING
        executor._order = TrackedOrder("OID-DONE")  # connector evicted the order: .order stays None
        event = BuyOrderCompletedEvent(
            timestamp=1234.0,
            order_id="OID-DONE",
            base_asset="ETH",
            quote_asset="USDT",
            base_asset_amount=Decimal("1"),
            quote_asset_amount=Decimal("100"),
            order_type=OrderType.LIMIT,
        )
        executor.process_order_completed_event("mock", MagicMock(), event)
        self.assertEqual(CloseType.POSITION_HOLD, executor.close_type)
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual(1, len(executor._held_position_orders))
        self.assertEqual("OID-DONE", executor._held_position_orders[0]["client_order_id"])

    @patch.object(OrderExecutor, "_sleep", new_callable=AsyncMock)
    async def test_shutdown_with_evicted_partial_fills_does_not_raise(self, _):
        # _partial_filled_orders entries whose InFlightOrder was evicted must not raise
        # in the held-inventory serialization either.
        executor = self.build_executor()
        executor._order = None
        executor._partial_filled_orders = [TrackedOrder("OID-EVICTED")]

        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual(CloseType.POSITION_HOLD, executor.close_type)
        self.assertEqual("OID-EVICTED", executor._held_position_orders[0]["client_order_id"])


class TestPositionExecutorShutdownRetries(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    def setUp(self) -> None:
        super().setUp()
        self.strategy = create_mock_strategy()
        self.strategy.current_timestamp = 1000.0

    def build_executor(self) -> PositionExecutor:
        config = PositionExecutorConfig(
            timestamp=1234,
            connector_name="binance",
            trading_pair="ETH-USDT",
            side=TradeType.BUY,
            entry_price=Decimal("100"),
            amount=Decimal("1"),
        )
        executor = PositionExecutor(self.strategy, config)
        self.set_loggers(loggers=[executor.logger()])
        executor._status = RunnableStatus.SHUTTING_DOWN
        executor.close_type = CloseType.STOP_LOSS
        return executor

    @patch("hummingbot.strategy_v2.executors.position_executor.position_executor.get_structured_logger")
    @patch.object(PositionExecutor, "_sleep", new_callable=AsyncMock)
    @patch.object(PositionExecutor, "control_close_order", new_callable=AsyncMock)
    @patch.object(PositionExecutor, "open_and_close_volume_match", MagicMock(return_value=False))
    @patch.object(PositionExecutor, "all_orders_completed", MagicMock(return_value=True))
    async def test_wedged_close_order_terminates_at_retry_ceiling(self, close_order_mock, _, __):
        executor = self.build_executor()
        executor._current_retries = executor._max_retries

        await executor.control_shutdown_process()
        close_order_mock.assert_not_called()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertTrue(self.is_partially_logged("ERROR", "may remain open on the exchange"))

    @patch("hummingbot.strategy_v2.executors.position_executor.position_executor.get_structured_logger")
    @patch.object(PositionExecutor, "_sleep", new_callable=AsyncMock)
    @patch.object(PositionExecutor, "control_close_order", new_callable=AsyncMock)
    @patch.object(PositionExecutor, "open_and_close_volume_match", MagicMock(return_value=False))
    @patch.object(PositionExecutor, "all_orders_completed", MagicMock(return_value=True))
    async def test_close_keeps_retrying_below_ceiling(self, close_order_mock, _, __):
        executor = self.build_executor()
        executor._current_retries = executor._max_retries - 2

        await executor.control_shutdown_process()
        close_order_mock.assert_called_once()
        self.assertEqual(RunnableStatus.SHUTTING_DOWN, executor.status)
        self.assertEqual(executor._max_retries - 1, executor._current_retries)


class _RaisingInfoExecutor:
    """An executor whose executor_info computation raises (e.g. a NaN path in PnL)."""

    def __init__(self):
        self.is_closed = False
        self.early_stop_calls = 0
        self.config = MagicMock()
        self.config.id = "raising"

    def early_stop(self, keep_position: bool = False):
        self.early_stop_calls += 1

    @property
    def executor_info(self):
        raise RuntimeError("boom: executor_info computation failed")


class TestOrchestratorStopPersistence(unittest.TestCase):
    @patch.object(MarketsRecorder, "get_instance")
    def setUp(self, markets_recorder: MagicMock):
        markets_recorder.return_value = MagicMock(spec=MarketsRecorder)
        self.strategy = create_mock_strategy()
        self.strategy.current_timestamp = 1000.0
        self.orchestrator = ExecutorOrchestrator(strategy=self.strategy)

    @patch.object(ExecutorOrchestrator, "store_all_positions")
    @patch.object(ExecutorOrchestrator, "store_all_executors")
    def test_stop_with_raising_executor_info_still_stores(self, store_executors, store_positions):
        async def run():
            bad_executor = _RaisingInfoExecutor()
            self.orchestrator.active_executors["ctrl"] = [bad_executor]
            await self.orchestrator.stop()
            self.assertEqual(1, bad_executor.early_stop_calls)
            store_positions.assert_called_once()
            store_executors.assert_called_once()

        asyncio.run(run())

    @patch.object(ExecutorOrchestrator, "store_all_positions", MagicMock(side_effect=RuntimeError("db down")))
    @patch.object(ExecutorOrchestrator, "store_all_executors")
    def test_raising_position_store_does_not_skip_executor_store(self, store_executors):
        async def run():
            await self.orchestrator.stop()
            store_executors.assert_called_once()

        asyncio.run(run())

    @patch.object(MarketsRecorder, "get_instance")
    def test_store_all_positions_per_item_guard(self, markets_recorder_mock):
        recorder = MagicMock(spec=MarketsRecorder)
        markets_recorder_mock.return_value = recorder

        def make_position(trading_pair: str) -> PositionHold:
            position = PositionHold("binance", trading_pair, side=TradeType.BUY)
            executor_info = ExecutorInfo(
                id=f"id-{trading_pair}", timestamp=1234, type="position_executor",
                status=RunnableStatus.TERMINATED, config=PositionExecutorConfig(
                    timestamp=1234, trading_pair=trading_pair, connector_name="binance",
                    side=TradeType.BUY, amount=Decimal(10), entry_price=Decimal(100),
                ), net_pnl_pct=Decimal(0), net_pnl_quote=Decimal(0), cum_fees_quote=Decimal(0),
                filled_amount_quote=Decimal(100), is_active=False, is_trading=False,
                custom_info={"held_position_orders": [
                    {"order_id": "123", "amount": Decimal(10), "trade_type": "BUY",
                     "executed_amount_base": Decimal("10"), "executed_amount_quote": Decimal("2300"),
                     "cumulative_fee_paid_quote": Decimal(0)}]},
                controller_id="main",
            )
            position.add_orders_from_executor(executor_info)
            return position

        # First position's mid-price read raises (empty book at shutdown), second stores fine.
        self.strategy.market_data_provider.get_price_by_type.side_effect = [
            EnvironmentError("Order book is empty"), Decimal(230)]
        self.orchestrator.positions_held = {"main": [make_position("ETH-USDT"), make_position("BTC-USDT")]}
        self.orchestrator.store_all_positions()
        recorder.update_or_store_position.assert_called_once()

    @patch.object(MarketsRecorder, "get_instance")
    def test_store_all_executors_per_item_guard(self, markets_recorder_mock):
        recorder = MagicMock(spec=MarketsRecorder)
        recorder.store_or_update_executor.side_effect = [RuntimeError("db error"), None]
        markets_recorder_mock.return_value = recorder

        def make_executor(executor_id: str) -> MagicMock:
            executor = MagicMock(spec=PositionExecutor)
            executor.config = MagicMock()
            executor.config.id = executor_id
            executor.config.controller_id = "ctrl"
            return executor

        self.orchestrator.active_executors["ctrl"] = [make_executor("a"), make_executor("b")]
        self.orchestrator.store_all_executors()
        self.assertEqual(2, recorder.store_or_update_executor.call_count)
        self.assertEqual({}, self.orchestrator.active_executors)


if __name__ == "__main__":
    unittest.main()
