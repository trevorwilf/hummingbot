"""Plan/budget invariant fix tests (DASH-USDT 2026-07-10/11).

Observed twice in production:
    PLAN/BUDGET INVARIANT VIOLATION (buy, side_refresh_plan):
    planned=167.8029494720 exceeds the effective budget=167.7888208771... by 0.0141285948...

Root cause: the planner accounted each buy level at the QUANTIZED price while the invariant
check measures at the CONFIG price; when price quantization rounds down, amount x config
price sums a hair (~0.008%) above the budget. The fix accounts at the conservative
max(config, quantized) price, floors every quantized amount, and adds a HARD planner
post-condition that shaves (or drops) the smallest level until planned_total <= budget.

Covered:
 - deterministic DASH-shaped case: the exact budget 167.7888208771 with the production
   rung-weight vector 0.5,1,1,4,3.5,1,0.5 and prices whose quantization rounds down; the
   pre-fix accounting is replicated in-test and shown to exceed the budget, and the fixed
   planner is asserted to land <= budget.
 - property-style sweep over randomized budgets, weights, prices, and amount increments
   (including the awkward 0.0001 and 0.01): planned_total <= effective budget always, and
   every planned level clears min_order_quote.
"""
import random
import unittest
from decimal import ROUND_DOWN, Decimal

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (
    D,
    _Harness,
    _make_mdp,
)

PRODUCTION_WEIGHTS = ["0.5", "1", "1", "4", "3.5", "1", "0.5"]
DASH_BUDGET = Decimal("167.7888208771")


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return value.quantize(increment, rounding=ROUND_DOWN)


class _PlanHarness(_Harness):

    def _plan_ctrl(self, *, buy_prices, weights, owned_quote, amount_inc, price_inc,
                   mid="24.30", bid="24.29", ask="24.31", **overrides):
        balances = {"DASH": [D(0), D(0)], "USDT": [D(owned_quote), D(owned_quote)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        mdp.quantize_order_price.side_effect = (
            lambda c, p, price: _floor(D(price), D(price_inc)))
        mdp.quantize_order_amount.side_effect = (
            lambda c, p, amt: _floor(D(amt), D(amount_inc)))
        cfg = dict(
            trading_pair="DASH-USDT",
            total_amount_quote=Decimal("10000"),
            max_fund_value_quote=Decimal("50000"),
            buy_prices=[D(p) for p in buy_prices],
            buy_amounts_pct=[D(w) for w in weights],
            sell_prices=[D("25.5"), D("26.0"), D("26.5")],
            sell_amounts_pct=[D("1"), D("1"), D("1")],
            min_order_quote=D("1"),
            allow_partial_levels=True,
        )
        cfg.update(overrides)
        ctrl = self._build(mdp, **cfg)
        self._init_state(ctrl, owned_quote=owned_quote, owned_base="0", seed_value=owned_quote)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    def _sell_plan_ctrl(self, *, sell_prices, weights, owned_base, amount_inc, price_inc,
                        mid="24.30", bid="24.29", ask="24.31", **overrides):
        balances = {"DASH": [D(owned_base), D(owned_base)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        mdp.quantize_order_price.side_effect = (
            lambda c, p, price: _floor(D(price), D(price_inc)))
        mdp.quantize_order_amount.side_effect = (
            lambda c, p, amt: _floor(D(amt), D(amount_inc)))
        cfg = dict(
            trading_pair="DASH-USDT",
            total_amount_quote=Decimal("10000"),
            max_fund_value_quote=Decimal("50000"),
            buy_prices=[D("23.0"), D("22.5"), D("22.0")],
            buy_amounts_pct=[D("1"), D("1"), D("1")],
            sell_prices=[D(p) for p in sell_prices],
            sell_amounts_pct=[D(w) for w in weights],
            min_order_quote=D("1"),
            allow_partial_levels=True,
        )
        cfg.update(overrides)
        ctrl = self._build(mdp, **cfg)
        self._init_state(ctrl, owned_quote=0, owned_base=owned_base, seed_value=1000)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances


class TestDashDeterministicCase(_PlanHarness):

    # Config prices with a third decimal so a 0.01 price grid rounds them DOWN -- the
    # production mechanism behind the +0.0141 overshoot on the 167.7888208771 budget.
    PRICES = ["24.183", "23.847", "23.511", "23.175", "22.839", "22.503", "22.167"]

    def test_pre_fix_accounting_reproduces_the_violation_shape(self):
        """Replicates the OLD sizing loop (account at the quantized price, measure at the
        config price) and shows it exceeds the DASH budget -- the regression this suite
        exists to catch."""
        prices = [D(p) for p in self.PRICES]
        weights = [D(w) for w in PRODUCTION_WEIGHTS]
        total_w = sum(weights)
        remaining = DASH_BUDGET
        planned_at_config_prices = Decimal("0")
        for price, weight in zip(prices, weights):
            target = min(DASH_BUDGET * (weight / total_w), remaining)
            qprice = _floor(price, D("0.01"))
            qamount = _floor(target / qprice, D("0.0001"))
            planned_at_config_prices += qamount * price       # invariant-check basis
            remaining -= qamount * qprice                     # OLD accounting basis
        self.assertGreater(
            planned_at_config_prices, DASH_BUDGET,
            "the pre-fix accounting must overshoot, or this scenario lost its teeth")
        # Same order of magnitude as production (+0.0141 on 167.79, ~0.008%).
        overshoot = planned_at_config_prices - DASH_BUDGET
        self.assertLess(overshoot / DASH_BUDGET, Decimal("0.001"))

    def test_fixed_planner_lands_at_or_below_the_budget(self):
        ctrl, mdp, balances = self._plan_ctrl(
            buy_prices=self.PRICES, weights=PRODUCTION_WEIGHTS,
            owned_quote=str(DASH_BUDGET), amount_inc="0.0001", price_inc="0.01",
        )
        budget = ctrl._side_rebuild_budget_quote()
        self.assertEqual(DASH_BUDGET, budget)

        plan = ctrl._plan_buy_book()
        self.assertGreaterEqual(len(plan), 6, f"the DASH plan should keep the ladder: {plan}")

        level_prices = {ctrl._buy_level_id(i): p
                        for i, p in enumerate(ctrl.config.buy_prices)}
        planned_total = sum((amt * level_prices[lid] for lid, amt in plan.items()), Decimal("0"))
        self.assertLessEqual(planned_total, budget)

        # And the safety-net invariant no longer trips for this plan.
        ctrl._check_plan_budget_invariant("buy", planned_total, budget, "side_refresh_plan")
        self.assertEqual([], self._events(ctrl, "range_ladder_plan_budget_invariant_violation"))

    def test_every_kept_level_clears_min_order_quote(self):
        ctrl, mdp, balances = self._plan_ctrl(
            buy_prices=self.PRICES, weights=PRODUCTION_WEIGHTS,
            owned_quote=str(DASH_BUDGET), amount_inc="0.0001", price_inc="0.01",
        )
        plan = ctrl._plan_buy_book()
        for lid, amt in plan.items():
            idx = [ctrl._buy_level_id(i) for i in range(len(ctrl.config.buy_prices))].index(lid)
            qprice = _floor(ctrl.config.buy_prices[idx], D("0.01"))
            self.assertGreaterEqual(amt * qprice, ctrl.config.min_order_quote,
                                    f"{lid} fell below min_order_quote")


class TestPlanBudgetProperty(_PlanHarness):

    AMOUNT_INCREMENTS = ["0.000001", "0.0001", "0.01"]
    PRICE_INCREMENTS = ["0.01", "0.001"]

    def test_buy_plan_never_exceeds_effective_budget(self):
        rng = random.Random(20260712)
        for trial in range(40):
            n = rng.choice([3, 5, 7])
            if n == 7 and rng.random() < 0.7:
                weights = list(PRODUCTION_WEIGHTS)
            else:
                weights = [str(rng.choice([1, 1, 2, 3, 4, 0.5])) for _ in range(n)]
            # Strictly-descending config prices below the bid, with 3 decimals so the
            # price grid rounds them down.
            top = D(str(round(rng.uniform(15.0, 24.0), 3)))
            prices = [str(top - D("0.337") * i) for i in range(n)]
            budget = D(str(round(rng.uniform(20.0, 4000.0), 7)))
            amount_inc = rng.choice(self.AMOUNT_INCREMENTS)
            price_inc = rng.choice(self.PRICE_INCREMENTS)

            ctrl, mdp, balances = self._plan_ctrl(
                buy_prices=prices, weights=weights, owned_quote=str(budget),
                amount_inc=amount_inc, price_inc=price_inc,
            )
            effective = ctrl._side_rebuild_budget_quote()
            plan = ctrl._plan_buy_book()
            level_prices = {ctrl._buy_level_id(i): p
                            for i, p in enumerate(ctrl.config.buy_prices)}
            planned_total = sum((amt * level_prices[lid] for lid, amt in plan.items()),
                                Decimal("0"))
            self.assertLessEqual(
                planned_total, effective,
                f"trial {trial}: planned {planned_total} > budget {effective} "
                f"(prices={prices}, weights={weights}, inc={amount_inc}/{price_inc})")
            for lid, amt in plan.items():
                self.assertGreaterEqual(
                    amt * level_prices[lid], ctrl.config.min_order_quote,
                    f"trial {trial}: {lid} below min_order_quote")

    def test_sell_plan_never_exceeds_effective_base_budget(self):
        rng = random.Random(20260713)
        for trial in range(40):
            n = rng.choice([3, 5, 7])
            weights = [str(rng.choice([1, 1, 2, 3, 0.5])) for _ in range(n)]
            bottom = D(str(round(rng.uniform(25.0, 30.0), 3)))
            prices = [str(bottom + D("0.413") * i) for i in range(n)]
            owned_base = D(str(round(rng.uniform(1.0, 500.0), 6)))
            amount_inc = rng.choice(self.AMOUNT_INCREMENTS)
            price_inc = rng.choice(self.PRICE_INCREMENTS)

            ctrl, mdp, balances = self._sell_plan_ctrl(
                sell_prices=prices, weights=weights, owned_base=str(owned_base),
                amount_inc=amount_inc, price_inc=price_inc,
            )
            effective = ctrl._side_rebuild_budget_base()
            plan = ctrl._plan_sell_book()
            planned_total = sum(plan.values(), Decimal("0"))
            self.assertLessEqual(
                planned_total, effective,
                f"trial {trial}: planned base {planned_total} > budget {effective}")


class TestQuantizeAmountDown(_PlanHarness):

    def test_round_half_up_provider_is_stepped_down(self):
        """A provider that rounds to NEAREST must never let the planner round an amount
        up (the sum of rounded-up levels is exactly how a plan overruns its budget)."""
        ctrl, mdp, balances = self._plan_ctrl(
            buy_prices=["24.183", "23.847", "23.511"], weights=["1", "1", "1"],
            owned_quote="100", amount_inc="0.0001", price_inc="0.01",
        )
        from decimal import ROUND_HALF_UP
        mdp.quantize_order_amount.side_effect = (
            lambda c, p, amt: D(amt).quantize(D("1"), rounding=ROUND_HALF_UP))
        self.assertLessEqual(ctrl._quantize_amount_down(D("10.9")), D("10.9"))
        self.assertLessEqual(ctrl._quantize_amount_down(D("10.5")), D("10.5"))
        self.assertEqual(D("10"), ctrl._quantize_amount_down(D("10.4")))
        self.assertEqual(D("0"), ctrl._quantize_amount_down(D("0")))

    def test_floor_provider_passes_through(self):
        ctrl, mdp, balances = self._plan_ctrl(
            buy_prices=["24.183", "23.847", "23.511"], weights=["1", "1", "1"],
            owned_quote="100", amount_inc="0.0001", price_inc="0.01",
        )
        self.assertEqual(D("10.1234"), ctrl._quantize_amount_down(D("10.12345")))


if __name__ == "__main__":
    unittest.main()
