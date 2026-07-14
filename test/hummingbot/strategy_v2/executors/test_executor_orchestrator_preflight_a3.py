"""Budget-preflight hardening (V2 strategy fixes phase 3 — finding A3).

(1) A transient price outage (empty book during a WS reconnect: spot connectors RAISE
    EnvironmentError; some paths yield NaN) is not a budget shortfall. Market-order
    actions whose validation price is unavailable are dropped fail-closed with their own
    reason ``price_unavailable`` — distinguishable by controllers from
    ``insufficient_balance``/``preflight_error`` — with a rate-limited (not per-action)
    warning.
(2) A preflight resize must not mutate the controller's own config object in place:
    the action carries a model_copy with the adjusted amount, while the original config
    keeps the controller's intent for deferred/re-proposed actions.
"""
import unittest
from decimal import Decimal
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import MagicMock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.markets_recorder import MarketsRecorder
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction


class TestPreflightPriceUnavailableAndConfigCopy(unittest.TestCase, LoggerMixinForTest):

    @patch.object(MarketsRecorder, "get_instance")
    def setUp(self, markets_recorder: MagicMock):
        markets_recorder.return_value = MagicMock(spec=MarketsRecorder)
        market_info = MagicMock()
        market_info.market = MagicMock()
        strategy = MagicMock(spec=StrategyV2Base)
        type(strategy).market_info = PropertyMock(return_value=market_info)
        connector = MagicMock(spec=ExchangePyBase)
        type(connector).trading_rules = PropertyMock(
            return_value={"ETH-USDT": TradingRule(trading_pair="ETH-USDT")})
        strategy.connectors = {"binance": connector}
        strategy.market_data_provider = MagicMock(spec=MarketDataProvider)
        strategy.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal(230))
        strategy.controllers = {}
        strategy.markets = {"binance": {"ETH-USDT"}}
        strategy.current_timestamp = 1000.0
        self.mock_strategy = strategy
        self.orchestrator = ExecutorOrchestrator(strategy=strategy)
        self.set_loggers(loggers=[self.orchestrator.logger()])

    def _wire_budget_checker(self, adjusted_amount: Decimal):
        budget_checker = MagicMock()

        def adjust(candidate, all_or_none=False):
            candidate.amount = adjusted_amount
            return candidate

        budget_checker.adjust_candidate_and_lock_available_collateral = adjust
        budget_checker.reset_locked_collateral = MagicMock()
        connector = MagicMock()
        connector.budget_checker = budget_checker
        self.mock_strategy.connectors = {"binance": connector}

    def _action(self, amount="50", entry_price=Decimal(100)):
        config = PositionExecutorConfig(
            timestamp=1234, connector_name="binance", trading_pair="ETH-USDT",
            side=TradeType.BUY, entry_price=entry_price, amount=Decimal(amount))
        return CreateExecutorAction(executor_config=config, controller_id="ladder")

    def _register_controller(self):
        controller = MagicMock()
        controller.on_budget_preflight_result = MagicMock()
        self.mock_strategy.controllers = {"ladder": controller}
        return controller

    # --------------------------------------------------- (2) resize on a copy

    def test_resize_does_not_mutate_the_original_config(self):
        self._wire_budget_checker(Decimal("20"))
        self._register_controller()
        action = self._action(amount="50")
        original_config = action.executor_config

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([action], surviving)
        # The executor will be created from the resized copy...
        self.assertEqual(Decimal("20"), action.executor_config.amount)
        # ...while the controller's own config object keeps its intent.
        self.assertEqual(Decimal("50"), original_config.amount)
        self.assertIsNot(original_config, action.executor_config)

    def test_resize_notification_still_reports_original_and_adjusted(self):
        self._wire_budget_checker(Decimal("20"))
        controller = self._register_controller()
        action = self._action(amount="50")

        self.orchestrator._preflight_budget_check([action])

        call = controller.on_budget_preflight_result.call_args
        self.assertEqual("resized", call.kwargs["result"])
        self.assertEqual("insufficient_balance", call.kwargs["reason"])
        self.assertEqual(Decimal("50"), call.kwargs["original_amount"])
        self.assertEqual(Decimal("20"), call.kwargs["adjusted_amount"])

    # --------------------------------------------- (1) price_unavailable drops

    def test_raising_price_fetch_drops_with_price_unavailable(self):
        # Spot connectors raise EnvironmentError on an empty book.
        self._wire_budget_checker(Decimal("50"))
        self.mock_strategy.market_data_provider.get_price_by_type = MagicMock(
            side_effect=EnvironmentError("Order book is empty for ETH-USDT"))
        controller = self._register_controller()
        action = self._action(amount="50", entry_price=None)  # market action: price fetched

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([], surviving)  # fail-closed: dropped, controller retries
        call = controller.on_budget_preflight_result.call_args
        self.assertEqual("dropped", call.kwargs["result"])
        self.assertEqual("price_unavailable", call.kwargs["reason"])
        self.assertEqual(Decimal("50"), call.kwargs["original_amount"])
        self.assertIsNone(call.kwargs["adjusted_amount"])

    def test_nan_price_drops_with_price_unavailable(self):
        self._wire_budget_checker(Decimal("50"))
        self.mock_strategy.market_data_provider.get_price_by_type = MagicMock(
            return_value=Decimal("NaN"))
        controller = self._register_controller()
        action = self._action(amount="50", entry_price=None)

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([], surviving)
        self.assertEqual("price_unavailable",
                         controller.on_budget_preflight_result.call_args.kwargs["reason"])

    def test_price_unavailable_warning_is_rate_limited_not_per_action(self):
        self._wire_budget_checker(Decimal("50"))
        self.mock_strategy.market_data_provider.get_price_by_type = MagicMock(
            side_effect=EnvironmentError("Order book is empty"))
        self._register_controller()

        def warning_count():
            return sum(1 for record in self.log_records
                       if record.levelname == "WARNING"
                       and "validation price unavailable" in record.getMessage())

        # Two dropped actions in one pass -> ONE warning.
        self.orchestrator._preflight_budget_check(
            [self._action(entry_price=None), self._action(entry_price=None)])
        self.assertEqual(1, warning_count())

        # Same window -> still one warning (repeats are DEBUG).
        self.mock_strategy.current_timestamp = 1010.0
        self.orchestrator._preflight_budget_check([self._action(entry_price=None)])
        self.assertEqual(1, warning_count())

        # Past the 30s window -> the warning re-arms.
        self.mock_strategy.current_timestamp = 1040.0
        self.orchestrator._preflight_budget_check([self._action(entry_price=None)])
        self.assertEqual(2, warning_count())

    def test_priced_actions_do_not_touch_the_market_price_path(self):
        # A limit action carrying its own price never consults the provider, so a price
        # outage cannot affect it (the ladder's limit orders keep flowing).
        self._wire_budget_checker(Decimal("50"))
        self.mock_strategy.market_data_provider.get_price_by_type = MagicMock(
            side_effect=EnvironmentError("Order book is empty"))
        self._register_controller()
        action = self._action(amount="50", entry_price=Decimal(100))

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([action], surviving)


if __name__ == "__main__":
    unittest.main()
