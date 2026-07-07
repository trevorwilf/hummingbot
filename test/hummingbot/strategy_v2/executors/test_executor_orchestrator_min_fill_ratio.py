"""Budget-preflight dust-resize rule + controller feedback (preflight-retry fix).

A create action may carry min_fill_ratio: a preflight RESIZE below that fraction of the
intended amount becomes a DROP (a dust-sized order burns its level; the controller retries
at full size once balances settle). On every drop or resize the orchestrator notifies the
originating controller via on_budget_preflight_result() -- best-effort and opt-in, so the
action pipeline never depends on it. The preflight itself is NOT weakened: zero-amount
adjustments still drop, above-ratio resizes still resize, and actions without the hint keep
the legacy behavior byte-for-byte.
"""
import unittest
from decimal import Decimal
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


class TestPreflightMinFillRatio(unittest.TestCase):

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
        self.mock_strategy = strategy
        self.orchestrator = ExecutorOrchestrator(strategy=strategy)

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

    def _action(self, amount="50", min_fill_ratio=None):
        config = PositionExecutorConfig(
            timestamp=1234, connector_name="binance", trading_pair="ETH-USDT",
            side=TradeType.BUY, entry_price=Decimal(100), amount=Decimal(amount))
        return CreateExecutorAction(executor_config=config, controller_id="ladder",
                                    min_fill_ratio=min_fill_ratio)

    def _register_controller(self):
        controller = MagicMock()
        controller.on_budget_preflight_result = MagicMock()
        self.mock_strategy.controllers = {"ladder": controller}
        return controller

    # ------------------------------------------------------------- dust-resize rule

    def test_resize_below_ratio_is_dropped_not_placed(self):
        # intended 50, adjusted 10 -> 20% < 25% ratio -> DROP (the 0.0154-of-0.0512 case).
        self._wire_budget_checker(Decimal("10"))
        controller = self._register_controller()
        action = self._action(amount="50", min_fill_ratio=Decimal("0.25"))

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([], surviving)
        call = controller.on_budget_preflight_result.call_args
        self.assertEqual("dropped", call.kwargs["result"])
        self.assertEqual("resize_below_min_fill_ratio", call.kwargs["reason"])
        self.assertEqual(Decimal("50"), call.kwargs["original_amount"])
        self.assertEqual(Decimal("10"), call.kwargs["adjusted_amount"])

    def test_resize_above_ratio_still_resizes(self):
        # adjusted 20 -> 40% >= 25% -> normal resize survives (preflight not weakened).
        self._wire_budget_checker(Decimal("20"))
        controller = self._register_controller()
        action = self._action(amount="50", min_fill_ratio=Decimal("0.25"))

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([action], surviving)
        self.assertEqual(Decimal("20"), action.executor_config.amount)
        self.assertEqual("resized", controller.on_budget_preflight_result.call_args.kwargs["result"])

    def test_no_ratio_keeps_legacy_resize_behavior(self):
        self._wire_budget_checker(Decimal("1"))          # 2% of intended, but no hint
        self._register_controller()
        action = self._action(amount="50", min_fill_ratio=None)

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([action], surviving)            # legacy: resized, not dropped
        self.assertEqual(Decimal("1"), action.executor_config.amount)

    def test_zero_adjustment_still_drops(self):
        # Genuinely insufficient balance: the preflight still blocks over-placement.
        self._wire_budget_checker(Decimal("0"))
        controller = self._register_controller()
        action = self._action(amount="50", min_fill_ratio=Decimal("0.25"))

        surviving = self.orchestrator._preflight_budget_check([action])

        self.assertEqual([], surviving)
        call = controller.on_budget_preflight_result.call_args
        self.assertEqual("dropped", call.kwargs["result"])
        self.assertEqual("insufficient_balance", call.kwargs["reason"])

    # ------------------------------------------------------------- feedback robustness

    def test_missing_controller_never_breaks_the_pipeline(self):
        self._wire_budget_checker(Decimal("0"))
        self.mock_strategy.controllers = {}              # nobody registered
        surviving = self.orchestrator._preflight_budget_check(
            [self._action(amount="50", min_fill_ratio=Decimal("0.25"))])
        self.assertEqual([], surviving)                  # drop still happened, no crash

    def test_controller_without_hook_is_skipped(self):
        self._wire_budget_checker(Decimal("0"))
        controller = MagicMock(spec=[])                  # no on_budget_preflight_result
        self.mock_strategy.controllers = {"ladder": controller}
        surviving = self.orchestrator._preflight_budget_check(
            [self._action(amount="50", min_fill_ratio=Decimal("0.25"))])
        self.assertEqual([], surviving)

    def test_raising_hook_is_swallowed(self):
        self._wire_budget_checker(Decimal("0"))
        controller = MagicMock()
        controller.on_budget_preflight_result = MagicMock(side_effect=RuntimeError("boom"))
        self.mock_strategy.controllers = {"ladder": controller}
        surviving = self.orchestrator._preflight_budget_check(
            [self._action(amount="50", min_fill_ratio=Decimal("0.25"))])
        self.assertEqual([], surviving)


if __name__ == "__main__":
    unittest.main()
