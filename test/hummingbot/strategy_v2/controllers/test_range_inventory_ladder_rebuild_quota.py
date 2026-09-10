"""Rebuild planning must honor the same quote quota and fee headroom as placement.

BELLS repeatedly cancelled four fully deployed buys on 2026-09-10: the planner
added reservations back above the quota, but every rebuild applied the quota
again and recreated the identical book. Replay that shape with synthetic balances.
"""
import asyncio
from decimal import ROUND_DOWN
from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (
    D,
    _Harness,
    _make_mdp,
    _resting,
)

from hummingbot.core.data_type.common import TradeType


class TestRebuildQuoteQuota(_Harness):
    def _quota_setup(self, **overrides):
        balances = {"BELLS": [D("0.02"), D("0.02")],
                    "USDT": [D("1100"), D("230")]}
        mdp = _make_mdp(balances=balances, mid="0.05", bid="0.0499", ask="0.0501",
                        connector_style="nonkyc")
        mdp.quantize_order_amount.side_effect = (
            lambda c, p, amt: D(amt).quantize(D("0.01"), rounding=ROUND_DOWN))
        prices = [D(p) for p in ("0.049484", "0.049205", "0.048839", "0.048357")]
        config = dict(trading_pair="BELLS-USDT", total_amount_quote=D("234"),
                      shared_account_quote_quota=D("234"), fee_rate=D("0.002"),
                      buy_prices=prices, buy_amounts_pct=[D("25")] * 4,
                      enable_sells=False, min_order_quote=D("1"),
                      underdeployed_watchdog_seconds=300)
        config.update(overrides)
        ctrl = self._build(mdp, **config)
        self._init_state(ctrl, owned_quote="235.5", owned_base="0.02", seed_value="234")
        amounts = ["1179.84", "1186.53", "1195.42", "1207.33"]
        ctrl.executors_info = [
            _resting(ctrl._buy_level_id(i), TradeType.BUY, price, amount, f"b{i}")
            for i, (price, amount) in enumerate(zip(prices, amounts))]
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    def test_live_rebuild_plan_matches_post_cancel_placement(self):
        ctrl, mdp, balances = self._quota_setup()
        planned = ctrl._plan_buy_book()
        self.assertEqual(ctrl._resting_side_book(TradeType.BUY), planned)
        self.assertEqual(D("234") / D("1.002"), ctrl._side_rebuild_budget_quote())

        # The exchange releases principal and its fee hold after all cancels.
        reserved = ctrl.processed_data["active_buy_reserved_quote"]
        balances["USDT"][1] += reserved * D("1.002")
        ctrl.executors_info = []
        self._prime(ctrl, mdp, 1001.0)
        ctrl._buy_side_dirty = True
        created = {a.executor_config.level_id: a.executor_config.amount
                   for a in ctrl._create_buy_actions()}
        self.assertEqual(planned, created)

    def test_quota_saturated_book_stays_quiet_across_watchdog_windows(self):
        ctrl, mdp, _ = self._quota_setup()
        self.assertTrue(ctrl._side_free_budget_funds_a_level("buy"))
        for t in (1001, 1302, 1308, 1610, 1616, 1918, 1924, 2226, 2232):
            self.assertEqual([], self._full(ctrl, mdp, float(t)))
        self.assertEqual([], self._events(ctrl, "range_ladder_underdeployed_watchdog_fired"))
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_watchdog_suppressed_fully_deployed")))

    def test_missing_rung_still_triggers_repair(self):
        ctrl, mdp, balances = self._quota_setup()
        missing = ctrl.executors_info.pop()
        balances["USDT"][1] += missing.config.price * missing.config.amount * D("1.002")
        self._prime(ctrl, mdp, 1000.0)
        self.assertFalse(ctrl._side_refresh_converged(TradeType.BUY))
        self._full(ctrl, mdp, 1001.0)
        self._full(ctrl, mdp, 1302.0)
        self.assertEqual(3, len(self._stops(self._full(ctrl, mdp, 1308.0))))
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_underdeployed_watchdog_fired")))

    def test_quota_is_applied_before_fee_in_both_funding_modes(self):
        for ledger in (True, False):
            for fee in (D("0"), D("0.002")):
                with self.subTest(ledger=ledger, fee=fee):
                    ctrl, _, _ = self._quota_setup(ledger_funded_budgets=ledger, fee_rate=fee)
                    self.assertEqual(D("234") / (D("1") + fee), ctrl._side_rebuild_budget_quote())

    def test_unset_or_nonbinding_quota_preserves_existing_budget(self):
        for quota in (None, D("1000")):
            with self.subTest(quota=quota):
                ctrl, _, _ = self._quota_setup(shared_account_quote_quota=quota)
                p = ctrl.processed_data
                expected = p["free_buy_budget_quote"] + p["active_buy_reserved_quote"] / D("1.002")
                self.assertEqual(expected, ctrl._side_rebuild_budget_quote())

    def test_wave_credit_is_not_counted_twice(self):
        ctrl, mdp, _ = self._quota_setup()
        ctrl.executors_info.pop()  # A missing rung makes the rebuild non-noop.
        ctrl._buy_side_dirty = True
        ctrl._buy_dirty_reason = "global_timer"
        self.assertEqual(3, len(self._stops(self._full(ctrl, mdp, 1001.0))))
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual(ctrl.processed_data["active_buy_reserved_quote"], ctrl._wave_ledger_credit_quote)
        self.assertEqual(ctrl.processed_data["free_buy_budget_quote"], ctrl._side_rebuild_budget_quote())
