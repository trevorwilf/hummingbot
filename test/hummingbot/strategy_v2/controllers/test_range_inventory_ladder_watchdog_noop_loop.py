"""Under-deployment watchdog fire/skip infinite-loop fix tests (2026-07-09..12 production).

The watchdog fired 234 times over ~20h (one instance) and 138 times over ~12h (another),
exactly every underdeployed_watchdog_seconds, and 140/140 forced re-centers were then
skipped as noop_or_dust: a small rung-weight/quantization residual (0.006 base, ~$1.90,
above min_order_quote 1) passed the watchdog's raw free-budget threshold while the
planner's rebuild reproduced the resting book exactly.

Under test (fix rules):
 1+3. Fully-deployed suppression via the planner's OWN noop test (_side_refresh_converged):
      a converged side NEVER fires, observable once per episode via
      range_ladder_watchdog_suppressed_fully_deployed.
 2.   Noop backoff: a forced refresh skipped as noop_or_dust resets the under-deployment
      timer and doubles the next fire's threshold (2x per consecutive noop, capped 8x);
      a changed free budget resets the backoff.
 4.   Fresh-balance gate: the first fire of an episode forces a connector balance refresh
      and re-centers only if the condition persists on fresh data.
"""
import unittest
from decimal import ROUND_DOWN

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (
    D,
    _Harness,
    _make_mdp,
    _resting,
)

from hummingbot.core.data_type.common import TradeType

SELL_PRICES = ["350", "355", "360", "365", "370", "375", "380", "385", "390"]

FIRED = "range_ladder_underdeployed_watchdog_fired"
SUPPRESSED = "range_ladder_watchdog_suppressed_fully_deployed"
SKIPPED = "range_ladder_side_refresh_skipped"
REFRESH_REQUESTED = "range_ladder_underdeployed_balance_refresh_requested"


class _WatchdogHarness(_Harness):

    def _fully_deployed_setup(self, **config_overrides):
        """Production replay shape: 9 planned sell levels, 9 live executors placed exactly
        as the planner would place them, and a 0.006-base residual (~$2, above
        min_order_quote=1 in quote terms) left over from quantization."""
        balances = {"XMR": [D("0.906"), D("0.906")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances)
        # Coarse amount grid so the plan floors 0.906/9 = 0.100666.. to 0.10 per rung,
        # leaving the 0.006 residual observed in production.
        mdp.quantize_order_amount.side_effect = (
            lambda c, p, amt: D(amt).quantize(D("0.01"), rounding=ROUND_DOWN))
        overrides = dict(
            sell_prices=[D(p) for p in SELL_PRICES],
            sell_amounts_pct=[D("1")] * len(SELL_PRICES),
            min_order_quote=D("1"),
            underdeployed_watchdog_seconds=300,
        )
        overrides.update(config_overrides)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=0, owned_base="0.906", seed_value=400)
        self._prime(ctrl, mdp, 1000.0)

        plan = ctrl._plan_sell_book()
        self.assertEqual(9, len(plan), f"setup must plan all 9 rungs, got {plan}")
        self.assertTrue(all(amt == D("0.10") for amt in plan.values()), plan)

        prices = {f"sell_{p}": p for p in SELL_PRICES}
        resting = [_resting(lid, TradeType.SELL, prices[lid], str(amt), f"e{i}")
                   for i, (lid, amt) in enumerate(sorted(plan.items()))]
        ctrl.executors_info = resting
        balances["XMR"] = [D("0.906"), D("0.006")]  # 0.90 reserved on the exchange
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances


class TestFullyDeployedSuppression(_WatchdogHarness):

    def test_production_replay_no_fire_when_plan_matches_resting_book(self):
        ctrl, mdp, balances = self._fully_deployed_setup()

        # The OLD fire condition holds: the residual passes the raw free-budget test.
        self.assertEqual(D("0.006"), ctrl.processed_data["free_sell_budget_base"])
        self.assertTrue(ctrl._side_free_budget_funds_a_level("sell"))

        # Many watchdog windows elapse; the side stays converged -> zero fires, ever.
        for t in (1301.0, 1400.0, 1602.0, 1750.0, 2000.0, 2500.0, 3000.0, 5000.0):
            self._full(ctrl, mdp, t)
        self.assertEqual([], self._events(ctrl, FIRED))
        self.assertEqual([], self._events(ctrl, SKIPPED))
        self.assertIsNone(ctrl._side_underdeployed_since["sell"])

        # The suppression is observable exactly once per episode.
        suppressed = self._events(ctrl, SUPPRESSED)
        self.assertEqual(1, len(suppressed))
        self.assertEqual("sell", suppressed[0].kwargs["side"])
        self.assertEqual(9, suppressed[0].kwargs["live_executors"])
        self.assertEqual(9, suppressed[0].kwargs["planned_levels"])

    def test_suppression_relogs_on_a_new_episode(self):
        ctrl, mdp, balances = self._fully_deployed_setup()
        self._full(ctrl, mdp, 1301.0)
        self.assertEqual(1, len(self._events(ctrl, SUPPRESSED)))

        # Budget drops below one level's worth -> the episode ends (no suppression at all).
        balances["XMR"] = [D("0.9005"), D("0.0005")]
        self._full(ctrl, mdp, 1400.0)
        # The residual returns -> a NEW suppression episode logs once more.
        balances["XMR"] = [D("0.906"), D("0.006")]
        self._full(ctrl, mdp, 1500.0)
        self._full(ctrl, mdp, 1900.0)
        self.assertEqual(2, len(self._events(ctrl, SUPPRESSED)))
        self.assertEqual([], self._events(ctrl, FIRED))


class TestNoopBackoff(_WatchdogHarness):

    def _diverged_setup(self):
        """The defensive rule-2 shape: the watchdog-time evaluation says a refresh WOULD
        change the book, but the apply-time evaluation no-ops. Reproduced by keying the
        planner-convergence answer off the dirty flag (not dirty during the watchdog
        evaluation, dirty during the refresh apply)."""
        ctrl, mdp, balances = self._fully_deployed_setup()
        ctrl._side_refresh_converged = lambda side: ctrl._sell_side_dirty
        return ctrl, mdp, balances

    def test_noop_skip_resets_timer_and_engages_exponential_backoff(self):
        ctrl, mdp, balances = self._diverged_setup()
        connector = mdp.get_connector.return_value

        # Episode timer seeds on the first evaluation after the setup cycles.
        self._full(ctrl, mdp, 1001.0)
        self.assertEqual(1001.0, ctrl._side_underdeployed_since["sell"])

        # Threshold crossed -> rule 4: the balance refresh fires FIRST, no re-center yet.
        self._full(ctrl, mdp, 1302.0)
        self.assertEqual([], self._events(ctrl, FIRED))
        self.assertEqual(1, len(self._events(ctrl, REFRESH_REQUESTED)))
        self.assertGreaterEqual(connector._update_balances.call_count, 1)

        # Condition persists past the fresh-balance grace -> fire #1; the forced refresh
        # then no-ops -> timer reset + backoff streak 1.
        self._full(ctrl, mdp, 1308.0)
        self.assertEqual(1, len(self._events(ctrl, FIRED)))
        skipped = self._events(ctrl, SKIPPED)
        self.assertEqual(1, len(skipped))
        self.assertEqual("underdeployed_watchdog", skipped[0].kwargs["reason"])
        self.assertEqual("noop_or_dust", skipped[0].kwargs["guard"])
        self.assertEqual(1, ctrl._underdeployed_noop_streak["sell"])
        self.assertIsNone(ctrl._side_underdeployed_since["sell"])
        self.assertFalse(ctrl._sell_side_dirty)

        # The timer re-seeds; with backoff 2x the next fire needs 600s, so the 300s-cadence
        # spam is gone: nothing fires through the whole old-cadence window.
        self._full(ctrl, mdp, 1400.0)  # timer re-seeds here
        for t in (1701.0, 1800.0, 1900.0, 1999.0):
            self._full(ctrl, mdp, t)
        self.assertEqual(1, len(self._events(ctrl, FIRED)))

        # 600s after the re-seed the cycle repeats: refresh gate, then fire #2, noop -> 4x.
        self._full(ctrl, mdp, 2001.0)   # >= 1400 + 600 -> fresh-balance request
        self.assertEqual(1, len(self._events(ctrl, FIRED)))
        self._full(ctrl, mdp, 2008.0)   # grace passed -> fire #2
        self.assertEqual(2, len(self._events(ctrl, FIRED)))
        self.assertEqual(2, ctrl._underdeployed_noop_streak["sell"])

        # Backoff is now 4x (1200s): a full 1200s window stays quiet.
        self._full(ctrl, mdp, 2100.0)   # timer re-seeds
        for t in (2500.0, 2800.0, 3100.0, 3299.0):
            self._full(ctrl, mdp, t)
        self.assertEqual(2, len(self._events(ctrl, FIRED)))

    def test_backoff_multiplier_caps_at_8x(self):
        ctrl, mdp, balances = self._diverged_setup()
        ctrl._underdeployed_noop_streak["sell"] = 7  # 2**7 = 128 -> capped to 8
        ctrl._underdeployed_noop_budget["sell"] = D("0.006")  # unchanged budget
        self._full(ctrl, mdp, 1001.0)   # timer seeds
        for t in (1302.0, 2000.0, 3000.0, 3400.0):
            self._full(ctrl, mdp, t)    # 8x window = 2400s -> quiet until 3401
        self.assertEqual([], self._events(ctrl, FIRED))
        self._full(ctrl, mdp, 3402.0)   # threshold crossed -> refresh gate
        self._full(ctrl, mdp, 3408.0)   # grace passed -> fire
        self.assertEqual(1, len(self._events(ctrl, FIRED)))
        self.assertEqual(8, self._events(ctrl, FIRED)[0].kwargs["backoff_multiplier"])

    def test_budget_change_resets_the_backoff(self):
        ctrl, mdp, balances = self._diverged_setup()
        ctrl._underdeployed_noop_streak["sell"] = 3
        ctrl._underdeployed_noop_budget["sell"] = D("0.006")
        self._full(ctrl, mdp, 1001.0)   # timer seeds

        # The free budget changes (0.006 -> 0.016; both the ledger and the wallet move,
        # since the budget is min(owned_free, wallet)): backoff resets, the plain 300s
        # threshold applies again.
        ctrl._state["owned_base"] = "0.916"
        balances["XMR"] = [D("0.916"), D("0.016")]
        self._full(ctrl, mdp, 1302.0)   # crossed at 1x -> fresh-balance request
        self.assertEqual(0, ctrl._underdeployed_noop_streak["sell"])
        self._full(ctrl, mdp, 1308.0)   # grace passed -> fires at the base interval
        self.assertEqual(1, len(self._events(ctrl, FIRED)))

    def test_real_work_resets_the_noop_streak(self):
        ctrl, mdp, balances = self._fully_deployed_setup()
        ctrl._underdeployed_noop_streak["sell"] = 2
        ctrl._underdeployed_noop_budget["sell"] = D("0.006")
        # A genuinely-diverged side: force the watchdog AND the apply step to see change
        # (drop one resting executor so the rebuild differs from the resting book).
        ctrl.executors_info = ctrl.executors_info[:-1]
        balances["XMR"] = [D("0.906"), D("0.106")]
        self._full(ctrl, mdp, 1001.0)
        # streak 2 with a CHANGED budget resets at evaluation -> base threshold applies.
        self._full(ctrl, mdp, 1302.0)   # fresh-balance request
        self._full(ctrl, mdp, 1308.0)   # fire; apply cancels for real this time
        self.assertEqual(1, len(self._events(ctrl, FIRED)))
        self.assertEqual(0, ctrl._underdeployed_noop_streak["sell"])
        self.assertIsNone(ctrl._underdeployed_noop_budget["sell"])


class TestFreshBalanceGate(_WatchdogHarness):

    def _diverged_setup(self):
        ctrl, mdp, balances = self._fully_deployed_setup()
        ctrl._side_refresh_converged = lambda side: ctrl._sell_side_dirty
        return ctrl, mdp, balances

    def test_refresh_then_condition_clears_no_fire(self):
        """The production hypothesis: the stale cached budget cleared after a forced
        refresh -- with fresh data showing the side fully deployed, no re-center fires."""
        ctrl, mdp, balances = self._diverged_setup()
        self._full(ctrl, mdp, 1001.0)   # timer seeds
        self._full(ctrl, mdp, 1302.0)   # threshold -> balance refresh requested, deferred
        self.assertEqual(1, len(self._events(ctrl, REFRESH_REQUESTED)))
        self.assertEqual([], self._events(ctrl, FIRED))

        # The refreshed balance reveals the residual was stale dust below one level.
        balances["XMR"] = [D("0.9005"), D("0.0005")]
        self._full(ctrl, mdp, 1308.0)
        self._full(ctrl, mdp, 1700.0)
        self.assertEqual([], self._events(ctrl, FIRED))
        self.assertIsNone(ctrl._side_underdeployed_since["sell"])

    def test_condition_persisting_after_refresh_fires(self):
        ctrl, mdp, balances = self._diverged_setup()
        self._full(ctrl, mdp, 1001.0)
        self._full(ctrl, mdp, 1302.0)   # refresh requested, deferred
        self._full(ctrl, mdp, 1304.0)   # still inside the grace -> no fire yet
        self.assertEqual([], self._events(ctrl, FIRED))
        self._full(ctrl, mdp, 1308.0)   # grace passed, condition persists -> fire
        self.assertEqual(1, len(self._events(ctrl, FIRED)))


if __name__ == "__main__":
    unittest.main()
