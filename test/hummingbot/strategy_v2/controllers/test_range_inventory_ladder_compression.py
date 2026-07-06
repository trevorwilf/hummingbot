"""Tests for range_inventory_ladder quantization-aware compression."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CONTROLLER_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))


def _bind_exchange_min_gate(controller, cls):
    """Bind the real v15 exchange-minimum feasibility chain onto a MagicMock controller.
    The MagicMock provider has no dict trading_rules, so the exchange minimums resolve to 0
    and the gate reduces to the original amount>0 + min_order_quote checks."""
    controller._d = cls._d
    controller._rule_decimal = cls._rule_decimal
    for name in ("_exchange_trading_rule", "_exchange_min_order_size",
                 "_exchange_min_notional", "_level_quantization_failure"):
        setattr(controller, name, getattr(cls, name).__get__(controller, cls))


class TestBuyCompressionQuantizationAware(unittest.TestCase):
    """Verify buy compression accounts for quantization when deciding which levels to keep."""

    def _make_controller(self, buy_prices, buy_weights, min_order_quote="1"):
        from range_inventory_ladder import RangeInventoryLadderController
        controller = MagicMock(spec=RangeInventoryLadderController)
        controller.config = MagicMock()
        controller.config.buy_prices = [Decimal(str(p)) for p in buy_prices]
        controller.config.sell_prices = []
        controller.config.min_order_quote = Decimal(min_order_quote)
        controller.config.connector_name = "nonkyc"
        controller.config.trading_pair = "XMR-USDT"

        total_w = sum(Decimal(str(w)) for w in buy_weights)
        controller.config.normalized_buy_weights = [
            Decimal(str(w)) / total_w for w in buy_weights
        ]

        mdp = MagicMock()
        def quantize_price(connector, pair, price):
            return Decimal(str(round(float(price), 2)))
        def quantize_amount(connector, pair, amount):
            return Decimal(str(int(float(amount) * 1000) / 1000))
        mdp.quantize_order_price = quantize_price
        mdp.quantize_order_amount = quantize_amount
        controller.market_data_provider = mdp
        controller._emit_structured = MagicMock()

        controller._compress_buy_level_indexes_for_min_notional = (
            RangeInventoryLadderController._compress_buy_level_indexes_for_min_notional.__get__(
                controller, RangeInventoryLadderController
            )
        )
        _bind_exchange_min_gate(controller, RangeInventoryLadderController)
        return controller

    def test_equal_weight_budget_15_5(self):
        """Budget 15.5, 8 equal-weight levels. All kept levels must be placeable."""
        prices = [333, 327, 324, 321, 318, 315, 312, 305]
        weights = [1, 1, 1, 1, 1, 1, 1, 1]
        controller = self._make_controller(prices, weights)

        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=list(range(8)),
            total_quote_budget=Decimal("15.5"),
        )

        for idx in kept:
            price = Decimal(str(prices[idx]))
            w_total = sum(controller.config.normalized_buy_weights[i] for i in kept)
            rel_w = controller.config.normalized_buy_weights[idx] / w_total
            level_quote = Decimal("15.5") * rel_w
            q_price = Decimal(str(round(float(price), 2)))
            amount = level_quote / q_price
            q_amount = Decimal(str(int(float(amount) * 1000) / 1000))
            notional = q_amount * q_price
            self.assertGreaterEqual(notional, Decimal("1"),
                f"Level {prices[idx]} has notional {notional} < 1 after quantization")

    def test_equal_weight_budget_18(self):
        """Budget 18.05, 8 equal-weight levels."""
        prices = [333, 327, 324, 321, 318, 315, 312, 305]
        weights = [1, 1, 1, 1, 1, 1, 1, 1]
        controller = self._make_controller(prices, weights)

        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=list(range(8)),
            total_quote_budget=Decimal("18.05308728"),
        )

        for idx in kept:
            price = Decimal(str(prices[idx]))
            w_total = sum(controller.config.normalized_buy_weights[i] for i in kept)
            rel_w = controller.config.normalized_buy_weights[idx] / w_total
            level_quote = Decimal("18.05308728") * rel_w
            q_price = Decimal(str(round(float(price), 2)))
            amount = level_quote / q_price
            q_amount = Decimal(str(int(float(amount) * 1000) / 1000))
            notional = q_amount * q_price
            self.assertGreaterEqual(notional, Decimal("1"),
                f"Level {prices[idx]} has notional {notional} < 1 after quantization")

    def test_kept_levels_are_nearest_first(self):
        """Kept levels must form a prefix of the candidate list."""
        prices = [333, 327, 324, 321, 318, 315, 312, 305]
        weights = [1, 1, 1, 1, 1, 1, 1, 1]
        controller = self._make_controller(prices, weights)

        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=list(range(8)),
            total_quote_budget=Decimal("15.5"),
        )
        for i, idx in enumerate(kept):
            self.assertEqual(idx, i, f"Kept indexes are not a prefix: got {kept}")

    def test_zero_budget_returns_empty(self):
        controller = self._make_controller([333, 327], [1, 1])
        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=[0, 1], total_quote_budget=Decimal("0"))
        self.assertEqual(kept, [])

    def test_budget_below_min_returns_empty(self):
        controller = self._make_controller([333, 327], [1, 1])
        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=[0, 1], total_quote_budget=Decimal("0.5"))
        self.assertEqual(kept, [])

    def test_large_budget_keeps_all(self):
        prices = [333, 327, 324, 321, 318, 315, 312, 305]
        weights = [1, 1, 1, 1, 1, 1, 1, 1]
        controller = self._make_controller(prices, weights)
        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=list(range(8)), total_quote_budget=Decimal("100"))
        self.assertEqual(kept, list(range(8)))


class TestSellCompressionQuantizationAware(unittest.TestCase):
    """Verify sell compression accounts for quantization."""

    def _make_controller(self, sell_prices, sell_weights, min_order_quote="1"):
        from range_inventory_ladder import RangeInventoryLadderController
        controller = MagicMock(spec=RangeInventoryLadderController)
        controller.config = MagicMock()
        controller.config.sell_prices = [Decimal(str(p)) for p in sell_prices]
        controller.config.buy_prices = []
        controller.config.min_order_quote = Decimal(min_order_quote)
        controller.config.connector_name = "nonkyc"
        controller.config.trading_pair = "XMR-USDT"

        total_w = sum(Decimal(str(w)) for w in sell_weights)
        controller.config.normalized_sell_weights = [
            Decimal(str(w)) / total_w for w in sell_weights
        ]

        mdp = MagicMock()
        def quantize_price(connector, pair, price):
            return Decimal(str(round(float(price), 2)))
        def quantize_amount(connector, pair, amount):
            return Decimal(str(int(float(amount) * 1000) / 1000))
        mdp.quantize_order_price = quantize_price
        mdp.quantize_order_amount = quantize_amount
        controller.market_data_provider = mdp
        controller._emit_structured = MagicMock()

        controller._compress_sell_level_indexes_for_min_notional = (
            RangeInventoryLadderController._compress_sell_level_indexes_for_min_notional.__get__(
                controller, RangeInventoryLadderController
            )
        )
        _bind_exchange_min_gate(controller, RangeInventoryLadderController)
        return controller

    def test_sell_startup_issue1_reproduction(self):
        """owned_base=0.00289, sell prices 350,355,360. No level should be placeable."""
        prices = [350, 355, 360]
        weights = [1, 1, 0.5]
        controller = self._make_controller(prices, weights)

        kept = controller._compress_sell_level_indexes_for_min_notional(
            candidate_indexes=[0, 1, 2],
            total_base_budget=Decimal("0.002890298712371923638308019134"),
        )

        for idx in kept:
            price = Decimal(str(prices[idx]))
            w_total = sum(controller.config.normalized_sell_weights[i] for i in kept)
            rel_w = controller.config.normalized_sell_weights[idx] / w_total
            level_base = Decimal("0.002890298712371923638308019134") * rel_w
            q_amount = Decimal(str(int(float(level_base) * 1000) / 1000))
            notional = q_amount * price
            self.assertGreaterEqual(notional, Decimal("1"),
                f"Kept sell level {prices[idx]} has notional {notional} < 1")

    def test_sell_with_sufficient_base(self):
        """With enough base to cover level 0's configured share (1/2.5 = 40%).

        Under the stable-denominator policy, each level gets its configured fraction
        of the deployable budget regardless of which other levels are kept. Level 0
        share = 1/2.5 = 0.4. Need budget so that quantized(budget * 0.4) * 350 >= 1.
        That requires quantized_amount >= 0.003, i.e. budget * 0.4 >= 0.003 -> budget >= 0.0075.
        Use 0.010 for margin.
        """
        prices = [350, 355, 360]
        weights = [1, 1, 0.5]
        controller = self._make_controller(prices, weights)

        kept = controller._compress_sell_level_indexes_for_min_notional(
            candidate_indexes=[0, 1, 2],
            total_base_budget=Decimal("0.010"),
        )
        self.assertGreater(len(kept), 0, "Should keep at least one sell level with 0.010 XMR")


class TestDetermineExecutorActionsDefersCreates(unittest.TestCase):
    """v12 Issue 3: create proposals are deferred PER-SIDE when same-cycle stops exist.

    A stop on one side must set only that side's defer flag and must NOT block the other
    side: create_actions_proposal still runs (the old global early-return was removed, so a
    sell stop no longer needlessly suppresses buy creates and vice versa).
    """

    def _run(self, stop_side):
        from range_inventory_ladder import RangeInventoryLadderController
        controller = MagicMock(spec=RangeInventoryLadderController)

        stop_action = MagicMock()
        stop_action.executor_id = "test_executor_1"
        controller.stop_actions_proposal = MagicMock(return_value=[stop_action])
        controller.create_actions_proposal = MagicMock(return_value=[])

        mock_executor = MagicMock()
        mock_executor.config = MagicMock()

        controller._find_executor_by_id = MagicMock(return_value=mock_executor)
        controller._executor_side = MagicMock(return_value=stop_side)
        controller._emit_structured = MagicMock()
        controller.executors_info = [mock_executor]

        # Bind the REAL side-lookup helper (Phase 7) so it routes through the mocked
        # _find_executor_by_id / _executor_side above, exactly like the old inline lookup.
        controller._executor_side_by_id = (
            RangeInventoryLadderController._executor_side_by_id.__get__(
                controller, RangeInventoryLadderController
            )
        )
        controller.determine_executor_actions = (
            RangeInventoryLadderController.determine_executor_actions.__get__(
                controller, RangeInventoryLadderController
            )
        )
        actions = controller.determine_executor_actions()
        return controller, actions

    def test_buy_stop_defers_only_buy_side_and_creates_still_run(self):
        from hummingbot.core.data_type.common import TradeType
        controller, actions = self._run(TradeType.BUY)
        # the stop itself is still returned
        self.assertEqual(len(actions), 1)
        # v12: creates are NO LONGER globally skipped -- the proposal runs (fall-through)
        controller.create_actions_proposal.assert_called_once()
        # only the BUY side is deferred
        self.assertTrue(controller._defer_buy_creates_this_cycle)
        self.assertFalse(controller._defer_sell_creates_this_cycle)
        emitted = [c.args[0] for c in controller._emit_structured.call_args_list]
        self.assertIn("range_ladder_create_deferred_for_stops", emitted)

    def test_sell_stop_defers_only_sell_side_and_creates_still_run(self):
        from hummingbot.core.data_type.common import TradeType
        controller, actions = self._run(TradeType.SELL)
        self.assertEqual(len(actions), 1)
        controller.create_actions_proposal.assert_called_once()
        self.assertFalse(controller._defer_buy_creates_this_cycle)
        self.assertTrue(controller._defer_sell_creates_this_cycle)


if __name__ == "__main__":
    unittest.main()
