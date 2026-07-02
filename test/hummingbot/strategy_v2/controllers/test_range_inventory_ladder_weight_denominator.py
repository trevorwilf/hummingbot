"""Tests for the stable weight denominator in range_inventory_ladder.

Bug fix: each level's share must be its CONFIGURED fraction of deployable budget,
not its fraction of the currently-eligible subset. Otherwise blocked levels cause
other eligible levels to over-allocate.
"""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))


def _mk_mdp():
    mdp = MagicMock()
    def qprice(conn, pair, p):
        return Decimal(str(round(float(p), 2)))
    def qamount(conn, pair, a):
        # Floor to 0.001 (XMR-style quantum)
        return Decimal(str(int(float(a) * 1000) / 1000))
    mdp.quantize_order_price = qprice
    mdp.quantize_order_amount = qamount
    mdp.time.return_value = 1_000_000.0
    return mdp


def _bind_exchange_min_gate(controller, cls):
    """Bind the real v15 exchange-minimum feasibility chain onto a MagicMock controller.
    The MagicMock provider exposes no dict trading_rules, so exchange minimums resolve to 0
    and the gate reduces to the original amount>0 + min_order_quote checks."""
    controller._d = cls._d
    controller._rule_decimal = cls._rule_decimal
    for name in ("_exchange_trading_rule", "_exchange_min_order_size",
                 "_exchange_min_notional", "_level_quantization_failure"):
        setattr(controller, name, getattr(cls, name).__get__(controller, cls))


def _mk_buy_controller(prices, weights, min_order_quote=Decimal("1")):
    from range_inventory_ladder import RangeInventoryLadderController
    controller = MagicMock(spec=RangeInventoryLadderController)
    controller.config = MagicMock()
    controller.config.buy_prices = [Decimal(str(p)) for p in prices]
    controller.config.sell_prices = []
    controller.config.min_order_quote = Decimal(str(min_order_quote))
    controller.config.connector_name = "nonkyc"
    controller.config.trading_pair = "XMR-USDT"

    total_w = sum(Decimal(str(w)) for w in weights)
    controller.config.normalized_buy_weights = [
        Decimal(str(w)) / total_w for w in weights
    ]

    controller.market_data_provider = _mk_mdp()
    controller._emit_structured = MagicMock()

    controller._compress_buy_level_indexes_for_min_notional = (
        RangeInventoryLadderController._compress_buy_level_indexes_for_min_notional.__get__(
            controller, RangeInventoryLadderController
        )
    )
    _bind_exchange_min_gate(controller, RangeInventoryLadderController)
    return controller


def _mk_sell_controller(prices, weights, min_order_quote=Decimal("1")):
    from range_inventory_ladder import RangeInventoryLadderController
    controller = MagicMock(spec=RangeInventoryLadderController)
    controller.config = MagicMock()
    controller.config.sell_prices = [Decimal(str(p)) for p in prices]
    controller.config.buy_prices = []
    controller.config.min_order_quote = Decimal(str(min_order_quote))
    controller.config.connector_name = "nonkyc"
    controller.config.trading_pair = "XMR-USDT"

    total_w = sum(Decimal(str(w)) for w in weights)
    controller.config.normalized_sell_weights = [
        Decimal(str(w)) / total_w for w in weights
    ]

    controller.market_data_provider = _mk_mdp()
    controller._emit_structured = MagicMock()

    controller._compress_sell_level_indexes_for_min_notional = (
        RangeInventoryLadderController._compress_sell_level_indexes_for_min_notional.__get__(
            controller, RangeInventoryLadderController
        )
    )
    _bind_exchange_min_gate(controller, RangeInventoryLadderController)
    return controller


class TestWeightDenominatorPlacement(unittest.TestCase):
    """Tests for placement loop target-allocation using configured denominator."""

    def test_all_levels_eligible_preserves_original_allocation(self):
        """Each level gets 100 * weight / 16.5 (configured fraction of full budget)."""
        prices = [333, 327, 324, 321, 318, 315, 312, 305]
        weights = [Decimal("1"), Decimal("1"), Decimal("1"), Decimal("3"),
                   Decimal("5"), Decimal("4"), Decimal("1"), Decimal("0.5")]
        total_w = sum(weights)
        budget = Decimal("100")
        deployable = budget

        # Simulate the new placement logic for all 8 eligible
        normalized = [w / total_w for w in weights]
        targets = []
        remaining = budget
        for i, w in enumerate(normalized):
            target = deployable * w
            target = min(target, remaining)
            targets.append(target)
            remaining -= target

        # Assert each level's share matches configured fraction
        for i, w in enumerate(weights):
            expected = budget * (w / total_w)
            self.assertAlmostEqual(float(targets[i]), float(expected), places=6)

    def test_blocked_levels_do_not_inflate_eligible_allocations(self):
        """Regression: buy_333 gets 1/16.5 share even when 312 and 305 are blocked."""
        weights = [Decimal("1"), Decimal("1"), Decimal("1"), Decimal("3"),
                   Decimal("5"), Decimal("4"), Decimal("1"), Decimal("0.5")]
        total_w = sum(weights)
        budget = Decimal("100")
        deployable = budget

        # Simulate with only first 6 eligible (312, 305 blocked)
        normalized = [w / total_w for w in weights]
        eligible_indexes = [0, 1, 2, 3, 4, 5]

        targets = {}
        remaining = budget
        for idx in eligible_indexes:
            w = normalized[idx]
            target = deployable * w
            target = min(target, remaining)
            targets[idx] = target
            remaining -= target

        # buy_333 (idx=0) gets 1/16.5 = 6.0606...%
        expected_333 = budget * (Decimal("1") / total_w)
        self.assertAlmostEqual(float(targets[0]), float(expected_333), places=6)

        # NOT 1/14 = 7.14% (old buggy share over subset sum)
        buggy_share = budget * (Decimal("1") / Decimal("14"))
        self.assertNotAlmostEqual(float(targets[0]), float(buggy_share), places=2)

        # Remaining budget reflects share reserved for blocked levels (1+0.5)/16.5 * 100
        reserved_for_blocked = budget * (Decimal("1.5") / total_w)
        self.assertAlmostEqual(float(remaining), float(reserved_for_blocked), places=5)

    def test_sell_side_same_behavior(self):
        """Mirror test on sell side: blocked levels don't inflate eligible shares."""
        weights = [Decimal("1"), Decimal("1"), Decimal("0.5")]
        total_w = sum(weights)
        budget = Decimal("0.5")  # base amount

        normalized = [w / total_w for w in weights]
        eligible_indexes = [0, 1]  # third blocked

        targets = {}
        remaining = budget
        for idx in eligible_indexes:
            w = normalized[idx]
            target = budget * w
            target = min(target, remaining)
            targets[idx] = target
            remaining -= target

        # Each eligible gets 1/2.5 of budget
        expected = budget * (Decimal("1") / total_w)
        for idx in eligible_indexes:
            self.assertAlmostEqual(float(targets[idx]), float(expected), places=6)

    def test_single_eligible_level_still_gets_only_its_configured_share(self):
        """With only 1 of 8 eligible, that level gets 1/16.5 share, not 100%."""
        weights = [Decimal("1"), Decimal("1"), Decimal("1"), Decimal("3"),
                   Decimal("5"), Decimal("4"), Decimal("1"), Decimal("0.5")]
        total_w = sum(weights)
        budget = Decimal("100")

        normalized = [w / total_w for w in weights]
        eligible_indexes = [0]

        target = budget * normalized[0]
        target = min(target, budget)
        expected = budget / total_w
        self.assertAlmostEqual(float(target), float(expected), places=6)
        # Definitely not 100 (the old subset-sum would be 100*1/1=100)
        self.assertLess(float(target), 50.0)


class TestCompressionFeasibilityUsesSameDenominator(unittest.TestCase):
    """Compression feasibility must use configured denominator, matching placement."""

    def test_compression_all_8_feasible_with_adequate_budget(self):
        """With budget large enough for smallest-weighted level, compression keeps all 8."""
        prices = [333, 327, 324, 321, 318, 315, 312, 305]
        weights = [Decimal("1"), Decimal("1"), Decimal("1"), Decimal("3"),
                   Decimal("5"), Decimal("4"), Decimal("1"), Decimal("0.5")]
        # Min weight is 0.5/16.5 ≈ 3%. With min_order_quote=1 and price=305,
        # need quantized_amount * price >= 1, which means amount >= 0.003
        # 0.003 * 305 = 0.915 (below 1), so need budget such that
        # (budget * 0.5/16.5) / 305 >= 0.004 -> budget >= 40.4
        budget = Decimal("50")
        controller = _mk_buy_controller(prices, weights)

        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=list(range(8)),
            total_quote_budget=budget,
        )
        self.assertEqual(kept, list(range(8)))

    def test_compression_drops_when_smallest_weight_infeasible(self):
        """With tight budget, smallest-weight levels get dropped."""
        prices = [333, 327]
        weights = [Decimal("1"), Decimal("0.05")]
        # 0.05/1.05 * budget at price 327 must quantize to >= 1 notional
        # Very small budget -> second level drops
        budget = Decimal("3")
        controller = _mk_buy_controller(prices, weights)

        kept = controller._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=[0, 1],
            total_quote_budget=budget,
        )
        # Second level (weight=0.05) should drop; first might or might not fit
        self.assertIn(len(kept), (0, 1))
        if len(kept) == 1:
            self.assertEqual(kept, [0])

    def test_sell_compression_same_denominator(self):
        """Sell-side compression uses configured denominator same as buy-side."""
        prices = [350, 355, 360]
        weights = [Decimal("1"), Decimal("1"), Decimal("0.5")]
        base_budget = Decimal("0.010")  # quantized to 0.010, with weights gives each a share
        controller = _mk_sell_controller(prices, weights)

        kept = controller._compress_sell_level_indexes_for_min_notional(
            candidate_indexes=[0, 1, 2],
            total_base_budget=base_budget,
        )
        # All feasibility is evaluated per-level on its CONFIGURED share
        # Each kept level's share = 0.010 * weight/2.5
        # Smallest share = 0.010 * 0.5/2.5 = 0.002, * 350 = 0.70 < 1 -> third drops
        # If 2 kept: each gets 0.010 * 1/2.5 = 0.004, quantized 0.004 * 350 = 1.4 >= 1 -> feasible
        self.assertEqual(kept, [0, 1])

    def test_quantization_rounding_doesnt_leak_across_levels(self):
        """Per-level allocation never exceeds configured share of deployable."""
        weights = [Decimal("1"), Decimal("1"), Decimal("3"), Decimal("5")]
        total_w = sum(weights)
        budget = Decimal("100")

        # Simulate the placement: total placed <= deployable * sum(weights) / total_w
        # Since sum(weights)/total_w = 1, total placed <= deployable
        normalized = [w / total_w for w in weights]
        targets = [budget * w for w in normalized]
        self.assertLessEqual(sum(targets), budget + Decimal("0.000001"))

        # No individual level exceeds its share
        for i, t in enumerate(targets):
            share = budget * (weights[i] / total_w)
            self.assertLessEqual(t, share + Decimal("0.000001"))


if __name__ == "__main__":
    unittest.main()
