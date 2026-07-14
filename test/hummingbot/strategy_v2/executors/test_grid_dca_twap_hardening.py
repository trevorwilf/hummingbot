"""Grid / DCA / TWAP executor hardening tests (V2 strategy fixes phase 5 — B1, B2, B3, B6a).

Under test:
- B1: GridExecutor constructed during an empty book terminates FAILED without raising
  out of __init__, fully initialized and queryable; start() on the failed executor never
  registers events.
- B2: grid/DCA barrier evaluation is SUSPENDED with a rate-limited warning on NaN
  prices/PnL (never a silent False), and still fires on finite values.
- B3: a TWAP order failure resets ONLY the failed slot; validation compares quote
  amounts against min_notional_size and min_order_size correctly; non-finite/negative
  slice prices and amounts are never submitted.
- B6a: a DCAExecutor that fails min-size validation at construction is still fully
  initialized (get_custom_info/executor_info work).
"""
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import MagicMock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.event.events import MarketOrderFailureEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode
from hummingbot.strategy_v2.executors.dca_executor.dca_executor import DCAExecutor
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.grid_executor.grid_executor import GridExecutor
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.executors.twap_executor.data_types import TWAPExecutorConfig, TWAPMode
from hummingbot.strategy_v2.executors.twap_executor.twap_executor import TWAPExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


def create_mock_strategy():
    market = MagicMock()
    market_info = MagicMock()
    market_info.market = market

    strategy = MagicMock(spec=StrategyV2Base)
    type(strategy).market_info = PropertyMock(return_value=market_info)
    type(strategy).trading_pair = PropertyMock(return_value="ETH-USDT")
    strategy.current_timestamp = 1000.0
    strategy.buy.side_effect = [f"OID-BUY-{i}" for i in range(1, 10)]
    strategy.sell.side_effect = [f"OID-SELL-{i}" for i in range(1, 10)]
    strategy.cancel.return_value = None
    connector = MagicMock(spec=ExchangePyBase)
    type(connector).trading_rules = PropertyMock(return_value={
        "ETH-USDT": TradingRule(
            trading_pair="ETH-USDT",
            min_order_size=Decimal("0.01"),
            min_price_increment=Decimal("0.01"),
            min_base_amount_increment=Decimal("0.0001"),
            min_notional_size=Decimal("5"),
        )})
    strategy.connectors = {"binance": connector}
    return strategy


def grid_config(**overrides):
    kwargs = dict(
        timestamp=1234, connector_name="binance", trading_pair="ETH-USDT",
        side=TradeType.BUY, total_amount_quote=Decimal("1000"),
        start_price=Decimal("90"), end_price=Decimal("110"), limit_price=Decimal("80"),
        triple_barrier_config=TripleBarrierConfig(
            take_profit=Decimal("0.01"), stop_loss=Decimal("0.2")),
    )
    kwargs.update(overrides)
    return GridExecutorConfig(**kwargs)


class TestGridConstructionSafety(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """B1 — grid construction with an unavailable mid price."""

    def setUp(self) -> None:
        super().setUp()
        self.strategy = create_mock_strategy()

    @patch.object(GridExecutor, "get_price", MagicMock(side_effect=EnvironmentError("Order book is empty")))
    def test_raising_price_terminates_failed_without_raising(self):
        executor = GridExecutor(self.strategy, grid_config())
        self.set_loggers(loggers=[executor.logger()])
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual([], executor.grid_levels)
        # Fully initialized: status queries must not raise (A2 trigger otherwise).
        info = executor.executor_info
        self.assertFalse(info.is_active)
        executor.get_custom_info()

    @patch.object(GridExecutor, "get_price", MagicMock(return_value=Decimal("NaN")))
    def test_nan_price_terminates_failed_without_raising(self):
        executor = GridExecutor(self.strategy, grid_config())
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        self.assertEqual([], executor.grid_levels)

    @patch.object(GridExecutor, "get_price", MagicMock(side_effect=EnvironmentError("empty")))
    def test_start_on_failed_executor_never_registers_events(self):
        executor = GridExecutor(self.strategy, grid_config())
        with patch.object(GridExecutor, "register_events") as register_mock:
            executor.start()
            register_mock.assert_not_called()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)

    @patch.object(GridExecutor, "get_price", MagicMock(return_value=Decimal("100")))
    def test_healthy_price_builds_grid(self):
        executor = GridExecutor(self.strategy, grid_config())
        self.assertGreater(len(executor.grid_levels), 0)
        self.assertIsNone(executor.close_type)
        self.assertEqual(RunnableStatus.NOT_STARTED, executor.status)


class TestGridBarrierSuspension(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """B2 (grid) — NaN prices suspend barrier evaluation with a warning."""

    def setUp(self) -> None:
        super().setUp()
        self.strategy = create_mock_strategy()

    def build_executor(self) -> GridExecutor:
        with patch.object(GridExecutor, "get_price", MagicMock(return_value=Decimal("100"))):
            executor = GridExecutor(self.strategy, grid_config())
        self.set_loggers(loggers=[executor.logger()])
        return executor

    def test_nan_prices_suspend_metrics_with_warning(self):
        executor = self.build_executor()
        executor.mid_price = Decimal("100")  # previous valid value
        with patch.object(GridExecutor, "get_price", MagicMock(return_value=Decimal("NaN"))):
            self.assertFalse(executor.update_metrics())
        self.assertEqual(Decimal("100"), executor.mid_price)  # previous value kept
        self.assertTrue(self.is_partially_logged("WARNING", "barriers suspended"))

    def test_raising_prices_suspend_metrics_with_warning(self):
        executor = self.build_executor()
        with patch.object(GridExecutor, "get_price",
                          MagicMock(side_effect=EnvironmentError("Order book is empty"))):
            self.assertFalse(executor.update_metrics())
        self.assertTrue(self.is_partially_logged("WARNING", "barriers suspended"))

    async def test_running_tick_skips_orders_and_barriers_when_prices_unavailable(self):
        executor = self.build_executor()
        executor._status = RunnableStatus.RUNNING
        with patch.object(GridExecutor, "get_price", MagicMock(return_value=Decimal("NaN"))), \
                patch.object(GridExecutor, "control_triple_barrier") as barrier_mock, \
                patch.object(GridExecutor, "get_open_orders_to_create") as create_mock:
            await executor.control_task()
        barrier_mock.assert_not_called()
        create_mock.assert_not_called()
        self.assertEqual(RunnableStatus.RUNNING, executor.status)

    def test_nan_pnl_suspends_stop_loss_with_warning(self):
        executor = self.build_executor()
        executor.position_pnl_pct = Decimal("NaN")
        self.assertFalse(executor.stop_loss_condition())
        self.assertTrue(self.is_partially_logged("WARNING", "barriers suspended"))

    def test_finite_pnl_still_fires_stop_loss(self):
        # Regression: a real stop-loss must still trigger.
        executor = self.build_executor()
        executor.position_pnl_pct = Decimal("-0.5")
        self.assertTrue(executor.stop_loss_condition())

    def test_valid_metrics_update_and_return_true(self):
        executor = self.build_executor()
        with patch.object(GridExecutor, "get_price", MagicMock(return_value=Decimal("105"))):
            self.assertTrue(executor.update_metrics())
        self.assertEqual(Decimal("105"), executor.mid_price)


class TestDCABarrierSuspensionAndInit(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """B2 (DCA) + B6a."""

    def setUp(self) -> None:
        super().setUp()
        self.strategy = create_mock_strategy()

    def dca_config(self, **overrides):
        kwargs = dict(
            id="dca-test", timestamp=1234, connector_name="binance", trading_pair="ETH-USDT",
            side=TradeType.BUY, amounts_quote=[Decimal("10"), Decimal("20")],
            prices=[Decimal("100"), Decimal("80")], stop_loss=Decimal("0.1"),
            take_profit=Decimal("0.1"), mode=DCAMode.TAKER,
        )
        kwargs.update(overrides)
        return DCAExecutorConfig(**kwargs)

    def build_executor(self, **overrides) -> DCAExecutor:
        executor = DCAExecutor(self.strategy, self.dca_config(**overrides))
        self.set_loggers(loggers=[executor.logger()])
        return executor

    @patch.object(DCAExecutor, "get_price", MagicMock(return_value=Decimal("100")))
    def test_min_size_failure_leaves_fully_initialized_executor(self):
        # amounts below min_notional_size (5): fails validation at construction.
        executor = self.build_executor(amounts_quote=[Decimal("1")], prices=[Decimal("100")])
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)
        # B6a: these used to raise AttributeError (half-init) — and via executor_info
        # they aborted the orchestrator's stop() persistence (A2 trigger).
        executor.get_custom_info()
        info = executor.executor_info
        self.assertFalse(info.is_active)
        self.assertEqual(0, executor._current_retries)
        self.assertEqual([], executor._open_orders)

    def test_nan_pnl_suspends_taker_stop_loss_with_warning(self):
        executor = self.build_executor()
        with patch.object(DCAExecutor, "net_pnl_quote", new_callable=PropertyMock,
                          return_value=Decimal("NaN")), \
                patch.object(DCAExecutor, "place_close_order_and_cancel_open_orders") as close_mock:
            executor.control_stop_loss()
        close_mock.assert_not_called()
        self.assertTrue(self.is_partially_logged("WARNING", "barriers suspended"))

    def test_finite_pnl_still_fires_taker_stop_loss(self):
        executor = self.build_executor()
        with patch.object(DCAExecutor, "net_pnl_quote", new_callable=PropertyMock,
                          return_value=Decimal("-1000")), \
                patch.object(DCAExecutor, "place_close_order_and_cancel_open_orders") as close_mock:
            executor.control_stop_loss()
        close_mock.assert_called_once()
        self.assertEqual(CloseType.STOP_LOSS, executor.close_type)

    def test_nan_pnl_suspends_take_profit_with_warning(self):
        executor = self.build_executor()
        with patch.object(DCAExecutor, "net_pnl_pct", new_callable=PropertyMock,
                          return_value=Decimal("NaN")), \
                patch.object(DCAExecutor, "place_close_order_and_cancel_open_orders") as close_mock:
            executor.control_take_profit()
        close_mock.assert_not_called()
        self.assertTrue(self.is_partially_logged("WARNING", "barriers suspended"))

    def test_finite_pnl_still_fires_take_profit(self):
        executor = self.build_executor()
        with patch.object(DCAExecutor, "net_pnl_pct", new_callable=PropertyMock,
                          return_value=Decimal("0.5")), \
                patch.object(DCAExecutor, "place_close_order_and_cancel_open_orders") as close_mock:
            executor.control_take_profit()
        close_mock.assert_called_once()


class TestTWAPFixes(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """B3 — plan-wipe fix, unit-correct validation, slice gates."""

    def setUp(self) -> None:
        super().setUp()
        self.strategy = create_mock_strategy()

    def twap_config(self, **overrides):
        kwargs = dict(
            timestamp=1, side=TradeType.BUY, trading_pair="ETH-USDT",
            connector_name="binance", total_amount_quote=Decimal("100"),
            total_duration=10, order_interval=5, mode=TWAPMode.TAKER,
        )
        kwargs.update(overrides)
        return TWAPExecutorConfig(**kwargs)

    def build_executor(self, **overrides) -> TWAPExecutor:
        executor = TWAPExecutor(self.strategy, self.twap_config(**overrides))
        self.set_loggers(loggers=[executor.logger()])
        return executor

    @patch.object(TWAPExecutor, "get_price", MagicMock(return_value=Decimal("100")))
    def test_single_failure_resets_only_the_failed_slot(self):
        executor = self.build_executor()
        timestamps = list(executor._order_plan.keys())
        filled = TrackedOrder("OID-FILLED")
        failed = TrackedOrder("OID-FAILED")
        pending = TrackedOrder("OID-PENDING")
        executor._order_plan = {timestamps[0]: filled, timestamps[1]: failed, timestamps[2]: pending}

        event = MarketOrderFailureEvent(timestamp=2, order_id="OID-FAILED", order_type=MagicMock())
        executor.process_order_failed_event("tag", MagicMock(), event)

        # Only the failed slot is reset; schedule and accounting elsewhere preserved.
        self.assertIs(filled, executor._order_plan[timestamps[0]])
        self.assertIsNone(executor._order_plan[timestamps[1]])
        self.assertIs(pending, executor._order_plan[timestamps[2]])
        self.assertEqual(1, executor._current_retries)
        self.assertIn(failed, executor._failed_orders)

    @patch.object(TWAPExecutor, "get_price", MagicMock(return_value=Decimal("100")))
    def test_validation_rejects_below_min_notional(self):
        # 3 slices of 100/3 quote... use a small total: slice = 100/3 > 5. Use total 9:
        # slice = 3 quote < min_notional_size 5 -> FAILED.
        executor = self.build_executor(total_amount_quote=Decimal("9"))
        self.assertEqual(CloseType.FAILED, executor.close_type)
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)

    @patch.object(TWAPExecutor, "get_price", MagicMock(return_value=Decimal("10000")))
    def test_validation_rejects_below_min_order_size_via_price(self):
        # slice = 30 quote passes notional (5), but 30/10000 = 0.003 base < 0.01 min_order_size.
        executor = self.build_executor(total_amount_quote=Decimal("90"))
        self.assertEqual(CloseType.FAILED, executor.close_type)

    @patch.object(TWAPExecutor, "get_price", MagicMock(return_value=Decimal("100")))
    def test_validation_passes_valid_config(self):
        # slice = 100/3 quote >= 5 notional; base 0.33 >= 0.01.
        executor = self.build_executor()
        self.assertIsNone(executor.close_type)
        self.assertEqual(RunnableStatus.NOT_STARTED, executor.status)

    @patch.object(TWAPExecutor, "get_price", MagicMock(return_value=Decimal("NaN")))
    def test_nan_price_skips_slice_instead_of_submitting(self):
        executor = self.build_executor()
        timestamps = list(executor._order_plan.keys())
        executor.create_order(timestamps[0])
        self.strategy.buy.assert_not_called()
        self.assertIsNone(executor._order_plan[timestamps[0]])
        self.assertTrue(self.is_partially_logged("WARNING", "mid price unavailable"))

    @patch.object(TWAPExecutor, "get_price", MagicMock(return_value=Decimal("100")))
    def test_negative_computed_amount_skips_slice(self):
        executor = self.build_executor()
        timestamps = list(executor._order_plan.keys())
        # An open resting order larger than the remaining budget drives the computed
        # slice negative — it must be skipped, not submitted.
        from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
        from hummingbot.core.data_type.common import OrderType
        resting = TrackedOrder("OID-RESTING")
        resting.order = InFlightOrder(
            client_order_id="OID-RESTING", trading_pair="ETH-USDT", order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY, amount=Decimal("2"), price=Decimal("100"),
            creation_timestamp=1.0, initial_state=OrderState.OPEN)
        executor._order_plan[timestamps[0]] = resting
        executor.create_order(timestamps[1])
        self.strategy.buy.assert_not_called()
        self.assertIsNone(executor._order_plan[timestamps[1]])
        self.assertTrue(self.is_partially_logged("WARNING", "is not placeable"))


if __name__ == "__main__":
    unittest.main()
