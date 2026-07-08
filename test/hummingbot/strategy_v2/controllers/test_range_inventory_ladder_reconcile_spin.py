"""Reconcile-spin fix tests (the 2026-07-08 12:58 UTC Kraken XMR/USD spin loop).

The round-2 preflight-retry gate timed out against a still-stale balance, then the
reconciliation retried ~every second, placing one preflight-resized SELL fragment per pass
and inflating the remaining rungs each re-plan until 88% of sell inventory sat idle for 1.5h+.

Covered here (the scenarios not already pinned in test_range_inventory_ladder_preflight_retry):
 1. 12:58 replay: stale-for-multiple-attempts then settle -> zero fragments, heal attempts
    spaced >= reconcile_retry_seconds, forced balance refresh re-requested per attempt, full
    pinned ladder on settlement, attempt count small.
 3. Pinned-intent immutability across attempts with a shifting effective budget; pins adjust
    DOWNWARD only for confirmed fills against those levels during the heal window.
 4. Fragment healing: 38/40/81% fragments dust-stopped, a 95% order left alone.
 5. Resize suppression: wave creates go all-or-none (min_fill_ratio=1) until the freed
    collateral is confirmed visible; normal ratio resumes afterward.
 6. Under-deployment watchdog backstop: unit behavior PLUS the end-to-end chain (retry cap
    exhausted -> wave TTL expiry -> balance settles -> watchdog -> full deployment), for both
    the empty-side and the stranded-fragment shapes.
 7. Cap semantics: with 15s spacing and cap 10, the cap warning fires only after the retry
    window genuinely spans >= 150s.
 8. Fast path no-regression: balance settles inside the gate window (NonKYC-style WS push)
    -> single placement pass, normal min_fill_ratio, zero heal retries, zero fragments.
 9. Log volume: the controller emits <=1 feedback INFO summary per heal attempt (per-level
    feedback is DEBUG); the orchestrator collapses per-level WARNINGs into one summary.
"""
import asyncio
import logging
import unittest
from decimal import ROUND_DOWN, Decimal
from unittest.mock import MagicMock, PropertyMock, patch

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (
    D,
    OWNED,
    _Harness,
    _make_mdp,
    _materialize,
    _resting,
    _terminate,
)

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.markets_recorder import MarketsRecorder
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction


# ================================ 1 + 7: the 12:58 replay (stale for several attempts)

class TestSpinReplay(_Harness):

    def _issue_then_stall(self):
        """Cancels close, the gate times out on a stale cache, the full plan issues, nothing
        materializes (the preflight drops it all). Returns (ctrl, mdp, balances, issued)."""
        ctrl, mdp, balances = self._sell_wave_setup()          # gate = 5 (pinned in the harness)
        self._full(ctrl, mdp, 2002.0)                          # refresh fires, gate holds
        actions = self._full(ctrl, mdp, 2008.0)                # gate timeout -> issue full plan
        issued = self._creates(actions)
        self.assertEqual(3, len(issued))
        return ctrl, mdp, balances, issued

    def test_no_fragments_spaced_retries_full_ladder_on_settle(self):
        ctrl, mdp, balances, issued = self._issue_then_stall()
        pinned = {a.executor_config.level_id: a.executor_config.amount for a in issued}
        connector = mdp.get_connector.return_value
        refresh_calls_at_issue = connector._update_balances.call_count

        # Balance stays stale across several heal attempts. Assert: every emitted create is at
        # its PINNED amount (never a fragment), heal ATTEMPTS are spaced >= reconcile_retry_seconds
        # in wall-clock (no tick-rate spin), and the forced refresh is re-requested per attempt.
        heal_event_wall_times = []
        t = 2009.0
        for _ in range(60):
            actions = self._full(ctrl, mdp, t)
            for a in self._creates(actions):
                self.assertEqual(pinned[a.executor_config.level_id], a.executor_config.amount,
                                 "a heal must re-propose the pinned amount, never a fragment")
            heals = self._events(ctrl, "range_ladder_intended_vs_live_heal")
            while len(heal_event_wall_times) < len(heals):
                heal_event_wall_times.append(t)                # wall time this attempt fired
            if len(heals) >= 3:
                break
            t += 2.0

        self.assertGreaterEqual(len(heal_event_wall_times), 3)
        # Heal attempts are spaced by at least the retry backoff (allow the 2s tick grid slack).
        spacing = [heal_event_wall_times[i] - heal_event_wall_times[i - 1]
                   for i in range(1, len(heal_event_wall_times))]
        for gap in spacing:
            self.assertGreaterEqual(gap, 15.0 - 0.001)
        # The forced balance refresh was re-requested (not one-shot).
        self.assertGreater(connector._update_balances.call_count, refresh_calls_at_issue)

        # The cache settles; the FULL pinned ladder places at the pinned amounts and resolves.
        balances["XMR"] = [D(OWNED), D(OWNED)]
        t += 16.0
        placed = []
        for _ in range(6):
            actions = self._full(ctrl, mdp, t)
            placed += self._creates(actions)
            if ctrl._refresh_wave["sell"] is None:
                break
            if self._creates(actions):
                ctrl.executors_info = _materialize(self._creates(actions))
            t += 1.0
        placed_amt = {a.executor_config.level_id: a.executor_config.amount for a in placed}
        self.assertEqual(pinned, placed_amt)                   # full ladder, pinned amounts
        self.assertIsNone(ctrl._refresh_wave["sell"])


# ================================ 3: pinned-intent immutability across a shifting budget

class TestPinnedImmutability(_Harness):

    def test_reproposed_amounts_never_reinflate(self):
        ctrl, mdp, balances = self._sell_wave_setup()
        self._full(ctrl, mdp, 2002.0)
        issued = self._creates(self._full(ctrl, mdp, 2008.0))
        pinned = {a.executor_config.level_id: a.executor_config.amount for a in issued}

        # Shrink the effective budget between attempts (the 2026-07-08 condition that made the
        # old re-plan re-normalize the far rungs upward). The pinned amounts must not move.
        seen_amounts = {lvl: set() for lvl in pinned}
        t = 2009.0
        for shrink in ("0.9", "0.8", "0.7"):
            balances["XMR"] = [D(OWNED), D("7E-8")]            # still stale
            actions = self._full(ctrl, mdp, t)
            for a in self._creates(actions):
                seen_amounts[a.executor_config.level_id].add(a.executor_config.amount)
            # let the gate time out so the pinned heal actually emits
            t += 16.0
            actions = self._full(ctrl, mdp, t)
            for a in self._creates(actions):
                seen_amounts[a.executor_config.level_id].add(a.executor_config.amount)
            t += 1.0

        for lvl, amounts in seen_amounts.items():
            self.assertTrue(amounts <= {pinned[lvl]},
                            f"{lvl} re-proposed a non-pinned amount {amounts} vs {pinned[lvl]}")
        # The pinned intent recorded on the wave equals the original issuance.
        record = ctrl._refresh_wave["sell"]
        self.assertEqual(pinned, dict(record["intended"]))


# ================================ 4: fragment healing thresholds

class TestFragmentHealing(_Harness):

    def test_38_40_81_percent_stopped_95_left_alone(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        pinned = {a.executor_config.level_id: a.executor_config.amount for a in issued}

        # Three rungs materialize as preflight fragments; make a fourth notion by reusing 95%.
        frags = [
            _resting("sell_350", TradeType.SELL, "350", str(pinned["sell_350"] * D("0.81")), "f350"),
            _resting("sell_355", TradeType.SELL, "355", str(pinned["sell_355"] * D("0.40")), "f355"),
            _resting("sell_360", TradeType.SELL, "360", str(pinned["sell_360"] * D("0.95")), "f360"),
        ]
        ctrl.executors_info = list(frags)

        actions = self._full(ctrl, mdp, 2003.0)
        stopped = {a.executor_id for a in self._stops(actions)}
        self.assertEqual({"f350", "f355"}, stopped)            # 81% and 40% are under-sized
        self.assertNotIn("f360", stopped)                      # 95% clears the 0.90 bar
        dust_events = {e.kwargs["level_id"] for e in self._events(ctrl, "range_ladder_reconcile_dust_stop")}
        self.assertEqual({"sell_350", "sell_355"}, dust_events)
        # The two under-sized rungs stay owed; the 95% one is satisfied and pruned.
        self.assertIn("sell_350", ctrl._refresh_wave["sell"]["intended"])
        self.assertIn("sell_355", ctrl._refresh_wave["sell"]["intended"])
        self.assertNotIn("sell_360", ctrl._refresh_wave["sell"]["intended"])

        # Once the cancels settle and balance is present, the two rungs re-place at FULL size.
        _terminate([frags[0], frags[1]], 2004.0)
        ctrl.executors_info = [frags[2]]
        balances["XMR"] = [D(OWNED), D(OWNED)]
        healed = self._creates(self._full(ctrl, mdp, 2005.0))
        healed_amt = {a.executor_config.level_id: a.executor_config.amount for a in healed}
        self.assertEqual({"sell_350", "sell_355"}, set(healed_amt))
        self.assertEqual(pinned["sell_350"], healed_amt["sell_350"])
        self.assertEqual(pinned["sell_355"], healed_amt["sell_355"])


# ================================ 5: resize suppression during the stale window

class TestResizeSuppression(_Harness):

    def test_all_or_none_until_balance_confirmed_then_normal_ratio(self):
        ctrl, mdp, balances = self._sell_wave_setup()          # gate = 5
        # Cancels closed; the refresh fires and the gate is active -> stale suspected.
        self._full(ctrl, mdp, 2002.0)
        self.assertTrue(ctrl._wave_stale_suspected("sell"))
        self.assertEqual(Decimal("1"), ctrl._action_min_fill_ratio("sell"))   # drop, don't resize

        # The freed collateral appears; the gate confirms it and normal resize behavior resumes.
        balances["XMR"] = [D(OWNED), D(OWNED)]
        self._full(ctrl, mdp, 2003.0)
        self.assertFalse(ctrl._wave_stale_suspected("sell"))
        self.assertEqual(D("0.25"), ctrl._action_min_fill_ratio("sell"))

    def test_gate_disabled_falls_back_to_normal_ratio(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        self._full(ctrl, mdp, 2002.0)
        # Gate disabled: the operator opted out, so suppression is off (legacy 0.25 resize).
        self.assertFalse(ctrl._wave_stale_suspected("sell"))
        self.assertEqual(D("0.25"), ctrl._action_min_fill_ratio("sell"))


# ================================ 6: under-deployment watchdog backstop

class TestUnderdeployedWatchdog(_Harness):

    def _underdeployed_ctrl(self, **overrides):
        # One resting sell holds 0.1 XMR; ~0.9 XMR sits free in the wallet (>= one $5 level at
        # ~335) -- an under-deployed side with idle fundable budget and no pending wave.
        balances = {"XMR": [D("1.0"), D("0.9")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances)
        ctrl = self._build(mdp, underdeployed_watchdog_seconds=300, **overrides)
        self._init_state(ctrl, owned_quote=0, owned_base="1.0", seed_value=400)
        ctrl.executors_info = [_resting("sell_350", TradeType.SELL, "350", "0.1", "live0")]
        self._prime(ctrl, mdp, 1000.0)
        ctrl._side_underdeployed_since = {"buy": None, "sell": None}
        return ctrl, mdp, balances

    @staticmethod
    def _tick(ctrl, mdp, t):
        import asyncio
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    def test_fires_after_window_and_forces_recenter(self):
        ctrl, mdp, balances = self._underdeployed_ctrl()
        self._tick(ctrl, mdp, 1000.0)                          # first observation, arm timer
        self.assertEqual(1000.0, ctrl._side_underdeployed_since["sell"])
        self.assertEqual([], self._events(ctrl, "range_ladder_underdeployed_watchdog_fired"))

        self._tick(ctrl, mdp, 1200.0)                          # 200s < 300s -> not yet
        self.assertEqual([], self._events(ctrl, "range_ladder_underdeployed_watchdog_fired"))

        self._tick(ctrl, mdp, 1301.0)                          # 301s >= 300s -> fire
        fired = self._events(ctrl, "range_ladder_underdeployed_watchdog_fired")
        self.assertEqual(1, len(fired))
        self.assertEqual("sell", fired[0].kwargs["side"])
        self.assertTrue(ctrl._sell_side_dirty)
        self.assertEqual("underdeployed_watchdog", ctrl._sell_dirty_reason)

    def test_yields_while_a_wave_is_pending(self):
        ctrl, mdp, balances = self._underdeployed_ctrl()
        # An unresolved refresh wave owns the side: the reconciliation/cap must stay meaningful.
        ctrl._refresh_wave["sell"] = ctrl._new_wave_record(1000.0, D("0.5"), {"x"})
        self._tick(ctrl, mdp, 1000.0)
        self._tick(ctrl, mdp, 1400.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_underdeployed_watchdog_fired"))
        self.assertIsNone(ctrl._side_underdeployed_since["sell"])

    def test_does_not_fire_when_fully_deployed(self):
        # No free budget beyond one small level -> not under-deployed.
        balances = {"XMR": [D("1.0"), D("0.00001")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances)
        ctrl = self._build(mdp, underdeployed_watchdog_seconds=300)
        self._init_state(ctrl, owned_quote=0, owned_base="1.0", seed_value=400)
        ctrl.executors_info = [_resting("sell_350", TradeType.SELL, "350", "0.99999", "live0")]
        self._prime(ctrl, mdp, 1000.0)
        ctrl._side_underdeployed_since = {"buy": None, "sell": None}
        self._tick(ctrl, mdp, 1000.0)
        self._tick(ctrl, mdp, 1400.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_underdeployed_watchdog_fired"))
        self.assertIsNone(ctrl._side_underdeployed_since["sell"])


# ================================ 3 (clause): pins adjust downward for heal-window fills

class TestFillAdjustment(_Harness):

    def _stopped_fragment(self, fraction, filled_fraction):
        """Issue the wave, materialize one sell_350 fragment at `fraction` of its pin, let the
        reconcile dust-stop it, then close it having filled `filled_fraction` of the pin."""
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        pinned = {a.executor_config.level_id: a.executor_config.amount for a in issued}
        frag = _resting("sell_350", TradeType.SELL, "350",
                        str(pinned["sell_350"] * D(fraction)), "frag0")
        ctrl.executors_info = [frag]
        actions = self._full(ctrl, mdp, 2003.0)                 # dust-stop queued
        assert {"frag0"} == {a.executor_id for a in self._stops(actions)}
        _terminate([frag], 2004.0)                              # closes DURING the heal window
        frag.custom_info = {"filled_amount_base": pinned["sell_350"] * D(filled_fraction)}
        balances["XMR"] = [D(OWNED), D(OWNED)]                  # settle so the heal can place
        return ctrl, mdp, pinned

    def test_partial_fill_trims_the_pin(self):
        ctrl, mdp, pinned = self._stopped_fragment(fraction="0.38", filled_fraction="0.2")
        healed = self._creates(self._full(ctrl, mdp, 2005.0))
        healed_amt = {a.executor_config.level_id: a.executor_config.amount for a in healed}
        expected_350 = (pinned["sell_350"] * D("0.8")).quantize(D("0.000001"), rounding=ROUND_DOWN)
        self.assertEqual(expected_350, healed_amt["sell_350"])  # pin - filled, not the full pin
        self.assertEqual(pinned["sell_355"], healed_amt["sell_355"])  # untouched pins unchanged
        self.assertEqual(pinned["sell_360"], healed_amt["sell_360"])
        adj = self._events(ctrl, "range_ladder_reconcile_fill_adjustment")
        self.assertEqual(1, len(adj))
        self.assertEqual("sell_350", adj[0].kwargs["level_id"])

    def test_fully_filled_fragment_prunes_the_level(self):
        ctrl, mdp, pinned = self._stopped_fragment(fraction="0.38", filled_fraction="1.0")
        healed = self._creates(self._full(ctrl, mdp, 2005.0))
        healed_levels = {a.executor_config.level_id for a in healed}
        self.assertEqual({"sell_355", "sell_360"}, healed_levels)   # consumed pin = success
        self.assertNotIn("sell_350", ctrl._refresh_wave["sell"]["intended"])

    def test_original_wave_cancels_never_trim_the_pin(self):
        # s0/s1 closed BEFORE issuance -- their fills funded the pins; no adjustment applies.
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        for ex in ctrl.executors_info:
            ex.custom_info = {"filled_amount_base": D("0.01")}
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        self.assertEqual(3, len(issued))
        self._full(ctrl, mdp, 2003.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_reconcile_fill_adjustment"))


# ================================ 6: the end-to-end backstop chain (cap -> TTL -> settle)

class TestBackstopChain(_Harness):

    def test_cap_then_ttl_then_settle_recovers_via_empty_side_watchdog(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0,
                                                    reconcile_max_attempts=2)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))   # full plan, all dropped
        self.assertEqual(3, len(issued))

        self._full(ctrl, mdp, 2003.0)                           # heal attempt 1
        self._full(ctrl, mdp, 2018.0)                           # heal attempt 2 -> cap reached
        self._full(ctrl, mdp, 2035.0)                           # past next_retry -> cap warning
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_reconcile_retry_cap")))
        self.assertIsNotNone(ctrl._refresh_wave["sell"])        # wave persists, watchdog yields
        self.assertEqual([], self._events(ctrl, "range_ladder_empty_side_watchdog_fired"))

        balances["XMR"] = [D(OWNED), D(OWNED)]                  # the exchange finally settles
        self._full(ctrl, mdp, 2902.0)                           # > started_ts + 900 -> TTL drop
        self.assertIsNone(ctrl._refresh_wave["sell"])

        actions = self._full(ctrl, mdp, 2965.0)                 # 63s empty -> watchdog fires
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_empty_side_watchdog_fired")))
        recovered = self._creates(actions)
        self.assertEqual(3, len(recovered))                     # full re-center, no manual action
        total = sum((a.executor_config.amount for a in recovered), D(0))
        self.assertGreaterEqual(total, D(OWNED) - D("0.000003"))
        ctrl.executors_info = _materialize(recovered)
        self._full(ctrl, mdp, 2966.0)
        self.assertIsNone(ctrl._refresh_wave["sell"])           # fully deployed, wave resolved

    def test_stranded_fragment_recovers_via_underdeployed_watchdog(self):
        # The post-TTL 2026-07-08 shape reconstructed directly: fragments hold the side
        # non-empty (defeating the empty-side watchdog) while most inventory sits free.
        balances = {"XMR": [D("1.06"), D("0.91")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base="1.06", seed_value=400)
        frag = _resting("sell_350", TradeType.SELL, "350", "0.15", "frag0")
        ctrl.executors_info = [frag]
        self._prime(ctrl, mdp, 1000.0)
        ctrl._side_underdeployed_since = {"buy": None, "sell": None}

        mdp.time.return_value = 1005.0
        asyncio.run(ctrl.update_processed_data())               # arms the under-deployed timer
        self.assertEqual(1005.0, ctrl._side_underdeployed_since["sell"])

        mdp.time.return_value = 1306.0
        asyncio.run(ctrl.update_processed_data())               # 301s >= 300s -> fires
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_underdeployed_watchdog_fired")))
        self.assertTrue(ctrl._sell_side_dirty)

        actions = ctrl.determine_executor_actions()             # re-center: the fragment cancels
        self.assertEqual({"frag0"}, {a.executor_id for a in self._stops(actions)})
        _terminate([frag], 1307.0)
        balances["XMR"] = [D("1.06"), D("1.06")]                # cancel frees the held base

        rebuilt = self._creates(self._full(ctrl, mdp, 1308.0))  # full ladder from the freed budget
        self.assertEqual(3, len(rebuilt))
        total = sum((a.executor_config.amount for a in rebuilt), D(0))
        self.assertGreaterEqual(total, D("1.05"))               # ~all inventory redeployed
        self.assertLessEqual(total, D("1.06"))
        ctrl.executors_info = _materialize(rebuilt)
        self._full(ctrl, mdp, 1309.0)
        self.assertIsNone(ctrl._refresh_wave["sell"])           # full deployment, wave resolved


# ================================ 7: cap semantics span the genuine retry window

class TestCapWindow(_Harness):

    def test_cap_warning_only_after_spanning_150s(self):
        # Defaults: reconcile_retry_seconds=15, reconcile_max_attempts=10. Balance permanently
        # stale; tick at 1s. The 10 attempts must span >= 135s and the cap warning must not
        # fire before issue + 150s (attempt 10's own backoff must lapse first).
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        self.assertEqual(3, len(issued))
        issue_time = 2002.0

        cap_time = None
        t = 2003.0
        while t < 2300.0:
            self._full(ctrl, mdp, t)
            if self._events(ctrl, "range_ladder_reconcile_retry_cap"):
                cap_time = t
                break
            t += 1.0

        heals = self._events(ctrl, "range_ladder_intended_vs_live_heal")
        self.assertEqual(10, len(heals))                        # the full cap was used
        self.assertIsNotNone(cap_time)
        self.assertGreaterEqual(cap_time - issue_time, 150.0)   # genuinely spans the window


# ================================ 8: fast path no-regression (NonKYC-style WS settle)

class TestFastPathNoRegression(_Harness):

    def test_settle_inside_gate_window_places_once_with_normal_ratio(self):
        ctrl, mdp, balances = self._sell_wave_setup()           # gate = 5s
        balances["XMR"] = [D(OWNED), D(OWNED)]                  # WS push lands immediately

        actions = self._full(ctrl, mdp, 2002.0)                 # single placement pass
        placed = self._creates(actions)
        self.assertEqual(3, len(placed))
        for action in placed:
            self.assertEqual(D("0.25"), action.min_fill_ratio)  # normal ratio, not all-or-none
        self.assertEqual([], self._events(ctrl, "range_ladder_post_cancel_balance_gate_timeout"))
        ctrl.executors_info = _materialize(placed)

        self._full(ctrl, mdp, 2003.0)
        self.assertIsNone(ctrl._refresh_wave["sell"])           # resolved, no retries needed
        self.assertEqual([], self._events(ctrl, "range_ladder_intended_vs_live_heal"))
        self.assertEqual([], self._events(ctrl, "range_ladder_preflight_feedback"))
        self.assertEqual([], self._events(ctrl, "range_ladder_reconcile_dust_stop"))


# ================================ 9: log volume (controller feedback + orchestrator summary)

class TestLogVolume(_Harness):

    def test_controller_feedback_is_debug_not_info(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        with self.assertLogs(ctrl.logger(), level="DEBUG") as cm:
            ctrl.on_budget_preflight_result(
                action=issued[0], result="dropped",
                original_amount=issued[0].executor_config.amount,
                adjusted_amount=Decimal("0"), reason="insufficient_balance")
        infos = [r for r in cm.records if r.levelno >= logging.INFO and "budget preflight" in r.getMessage()]
        self.assertEqual([], infos)                            # per-level feedback demoted to DEBUG
        debugs = [r for r in cm.records if r.levelno == logging.DEBUG and "budget preflight" in r.getMessage()]
        self.assertEqual(1, len(debugs))


class TestOrchestratorLogSummary(unittest.TestCase):
    """The orchestrator collapses per-level preflight WARNINGs into ONE summary per pass."""

    @patch.object(MarketsRecorder, "get_instance")
    def setUp(self, markets_recorder: MagicMock):
        markets_recorder.return_value = MagicMock(spec=MarketsRecorder)
        strategy = MagicMock(spec=StrategyV2Base)
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

    def _wire(self, adjusted):
        bc = MagicMock()
        bc.adjust_candidate_and_lock_available_collateral = \
            lambda candidate, all_or_none=False: setattr(candidate, "amount", adjusted) or candidate
        bc.reset_locked_collateral = MagicMock()
        connector = MagicMock()
        connector.budget_checker = bc
        self.mock_strategy.connectors = {"binance": connector}

    def _action(self, amount, min_fill_ratio=None):
        cfg = PositionExecutorConfig(timestamp=1, connector_name="binance", trading_pair="ETH-USDT",
                                     side=TradeType.BUY, entry_price=Decimal(100), amount=Decimal(amount))
        return CreateExecutorAction(executor_config=cfg, controller_id="ladder",
                                    min_fill_ratio=min_fill_ratio)

    def test_one_summary_warning_per_pass_not_per_level(self):
        self._wire(Decimal("0"))                               # everything drops
        actions = [self._action("50"), self._action("40"), self._action("30")]
        with self.assertLogs(self.orchestrator.logger(), level="WARNING") as cm:
            surviving = self.orchestrator._preflight_budget_check(actions)
        self.assertEqual([], surviving)
        warnings = [r for r in cm.records if r.levelno == logging.WARNING]
        self.assertEqual(1, len(warnings))                     # single summary, not 3 + summary
        self.assertIn("dropped 3", warnings[0].getMessage())


if __name__ == "__main__":
    unittest.main()
