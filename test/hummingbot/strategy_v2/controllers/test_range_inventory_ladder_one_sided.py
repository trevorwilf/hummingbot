"""One-sided / graceful wind-down mode tests (enable_buys / enable_sells /
cancel_disabled_side_orders).

The price lists stay fully populated (validators, indexers, and regime computation are
untouched); the three hot-updatable booleans gate PLACEMENT and CANCELLATION only:

- `_mark_side_dirty` drops every trigger for a disabled side (fill cross-side, cooldown
  lapse, global timer, watchdogs, reconcile) -- event-refresh mode places nothing.
- `_create_buy_actions` / `_create_sell_actions` return [] first thing for a disabled
  side -- legacy mode (creates every cycle) places nothing either.
- `stop_actions_proposal` stops a disabled side's resting orders (bypass-marked, no
  residual cooldown) when cancel_disabled_side_orders=True, WITHOUT returning early, so
  the enabled side's normal refresh continues the same cycle. When False the resting
  orders are left to fill naturally.
- `_run_empty_side_watchdog` skips disabled sides entirely (no WARNING spam) and resets
  their watchdog state so re-enabling starts clean.
- `_create_out_of_range_actions` never fires an exit on a disabled side.

Covered (Section 5 of the design):
 1.  enable_buys=False -> no new buys from ANY trigger; sell side fully unaffected.
 2.  enable_sells=False -> symmetric; a buy fill accumulates base with no sell placed.
 3.  cancel_disabled_side_orders=True + resting buys -> exactly those stopped (bypassed).
 4.  cancel_disabled_side_orders=False -> resting buys remain; no new buys.
 5.  all-default flags -> identical action/stop output to a baseline run, BOTH modes.
 6.  both sides disabled -> soft-pause; existing cancelled iff the cancel flag; ledger intact.
 7.  disabled side never fires the empty-side / under-deployed watchdogs.
 8.  re-enable -> the side rebuilds on the next trigger from the current budget.
 9.  legacy mode: disabled side places nothing; enabled side still refreshes by age.
 10. out_of_range_action='market_exit' + disabled side out of band -> no exit.
 plus: config defaults/permissiveness, hot-update without a config rebuild, observability
 (custom_info keys, status banner, once-per-transition disabled/re-enabled events).
"""
import unittest
from decimal import Decimal

from hummingbot.core.data_type.common import PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (
    D,
    _filled_exec,
    _Harness,
    _make_mdp,
    _materialize,
    _resting,
)

from range_inventory_ladder import RangeInventoryLadderConfig  # noqa: E402

BASE_OWNED = "1.06036199"
QUOTE_OWNED = "200"


def _set_prices(mdp, mid, bid, ask):
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]


class _OneSidedHarness(_Harness):
    """Harness defaults (from _Harness._build): buy_prices 321/318/315 (highest->lowest),
    sell_prices 350/355/360 (lowest->highest), min_order_quote=5, event refresh ON,
    per-side cooldowns 120s, global refresh 100000s."""

    def _dual_funded(self, *, mid=335, bid=334.9, ask=335.1, base=BASE_OWNED,
                     quote=QUOTE_OWNED, **overrides):
        balances = {"XMR": [D(base), D(base)], "USDT": [D(quote), D(quote)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=quote, owned_base=base, seed_value=800)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    def _quote_funded(self, *, mid=335, bid=334.9, ask=335.1, quote=QUOTE_OWNED, **overrides):
        balances = {"XMR": [D(0), D(0)], "USDT": [D(quote), D(quote)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=quote, owned_base=0, seed_value=quote)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    def _base_funded(self, *, mid, bid, ask, base=BASE_OWNED, **overrides):
        balances = {"XMR": [D(base), D(base)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=0, owned_base=base, seed_value=400)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    @staticmethod
    def _side_creates(actions, side):
        return [a for a in _OneSidedHarness._creates(actions)
                if a.executor_config.side == side]

    @staticmethod
    def _oob_creates(actions):
        return [a for a in _OneSidedHarness._creates(actions)
                if getattr(a.executor_config, "level_id", "") in ("buy_oob", "sell_oob")]


# ============================================== 1. buy side disabled, no trigger places


class TestBuySideDisabledNeverPlaces(_OneSidedHarness):

    def test_mark_side_dirty_gate_drops_every_reason(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        for reason in ("initial_placement", "sell_fill", "buy_cooldown_lapsed",
                       "global_timer", "empty_side_watchdog", "underdeployed_watchdog",
                       "ladder_reconcile"):
            with self.subTest(reason=reason):
                ctrl._mark_side_dirty("buy", reason)
                self.assertFalse(ctrl._buy_side_dirty)
                self.assertEqual("", ctrl._buy_dirty_reason)
        # The gate is side-specific: the sell side still marks normally.
        ctrl._mark_side_dirty("sell", "global_timer")
        self.assertTrue(ctrl._sell_side_dirty)
        self.assertEqual("global_timer", ctrl._sell_dirty_reason)

    def test_global_timer_trigger_rebuilds_only_the_sell_side(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        ctrl._last_global_refresh_ts = -1_000_000.0  # force the global timer to lapse
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertFalse(ctrl._buy_side_dirty)
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))
        sells = self._side_creates(actions, TradeType.SELL)
        self.assertEqual({"sell_350", "sell_355", "sell_360"},
                         {a.executor_config.level_id for a in sells})

    def test_buy_cooldown_lapse_disarms_without_marking_dirty(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        ctrl._buy_cooldown_armed = True
        ctrl._last_buy_fill_ts = 500.0  # 120s cooldown lapsed long ago
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertFalse(ctrl._buy_cooldown_armed)   # lapse consumed as usual
        self.assertFalse(ctrl._buy_side_dirty)       # ... but never scheduled a rebuild
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))

    def test_sell_fill_cross_side_trigger_is_dropped(self):
        ctrl, mdp, balances = self._dual_funded(enable_buys=False)
        fill = _filled_exec("sell_350", TradeType.SELL, "350", "f0",
                            filled_base="0.3", filled_quote="105",
                            close_ts=1000.5)
        ctrl.executors_info = [fill]
        balances["XMR"] = [D(BASE_OWNED) - D("0.3"), D(BASE_OWNED) - D("0.3")]
        balances["USDT"] = [D(QUOTE_OWNED) + D("105"), D(QUOTE_OWNED) + D("105")]
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertTrue(ctrl._sell_cooldown_armed)   # own-side cooldown still arms
        self.assertFalse(ctrl._buy_side_dirty)       # cross-side dirty dropped
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))

    def test_initial_placement_builds_only_the_sell_side(self):
        balances = {"XMR": [D(BASE_OWNED), D(BASE_OWNED)], "USDT": [D(QUOTE_OWNED), D(QUOTE_OWNED)]}
        mdp = _make_mdp(balances=balances)
        ctrl = self._build(mdp, enable_buys=False)
        self._init_state(ctrl, owned_quote=QUOTE_OWNED, owned_base=BASE_OWNED, seed_value=800)
        # No _prime reset: let the REAL initial_placement trigger run.
        actions = self._full(ctrl, mdp, 1000.0)
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))
        self.assertEqual({"sell_350", "sell_355", "sell_360"},
                         {a.executor_config.level_id
                          for a in self._side_creates(actions, TradeType.SELL)})


# ============================================== 2. sell side disabled: accumulate-only


class TestSellSideDisabledAccumulates(_OneSidedHarness):

    def test_mark_side_dirty_gate_is_symmetric(self):
        ctrl, mdp, _ = self._dual_funded(enable_sells=False)
        for reason in ("initial_placement", "buy_fill", "sell_cooldown_lapsed", "global_timer"):
            with self.subTest(reason=reason):
                ctrl._mark_side_dirty("sell", reason)
                self.assertFalse(ctrl._sell_side_dirty)
        ctrl._mark_side_dirty("buy", "global_timer")
        self.assertTrue(ctrl._buy_side_dirty)

    def test_buy_fill_books_base_into_ledger_but_places_no_sell(self):
        ctrl, mdp, balances = self._quote_funded(enable_sells=False)
        # A buy at 318 filled 0.3 XMR for 95.4 USDT.
        fill = _filled_exec("buy_318", TradeType.BUY, "318", "f0",
                            filled_base="0.3", filled_quote="95.4", close_ts=1000.5)
        ctrl.executors_info = [fill]
        balances["USDT"] = [D("104.6"), D("104.6")]
        balances["XMR"] = [D("0.3"), D("0.3")]
        actions = self._full(ctrl, mdp, 1001.0)
        # The fill was booked: the acquired base is IN the managed fund / sell budget ...
        self.assertEqual(D("0.3"), ctrl.processed_data["managed_base_total"])
        self.assertEqual(D("0.3"), ctrl.processed_data["free_sell_budget_base"])
        # ... its own-side cooldown armed as usual ...
        self.assertTrue(ctrl._buy_cooldown_armed)
        # ... but the cross-side sell refresh was dropped: nothing sells, base accumulates.
        self.assertFalse(ctrl._sell_side_dirty)
        self.assertEqual([], self._side_creates(actions, TradeType.SELL))

    def test_buy_side_fully_unaffected(self):
        ctrl, mdp, _ = self._quote_funded(enable_sells=False)
        ctrl._mark_side_dirty("buy", "global_timer")
        actions = self._full(ctrl, mdp, 1001.0)
        buys = self._side_creates(actions, TradeType.BUY)
        self.assertEqual({"buy_321", "buy_318", "buy_315"},
                         {a.executor_config.level_id for a in buys})
        self.assertEqual([], self._side_creates(actions, TradeType.SELL))


# ============================================== 3 + 4. cancel_disabled_side_orders


class TestCancelDisabledSideOrders(_OneSidedHarness):

    def _with_resting_both_sides(self, **overrides):
        ctrl, mdp, balances = self._dual_funded(**overrides)
        resting = [
            _resting("buy_321", TradeType.BUY, "321", "0.2", "b0"),
            _resting("buy_318", TradeType.BUY, "318", "0.2", "b1"),
            _resting("sell_350", TradeType.SELL, "350", "0.5", "s0"),
            _resting("sell_355", TradeType.SELL, "355", "0.5", "s1"),
        ]
        ctrl.executors_info = resting
        return ctrl, mdp, balances, resting

    def test_true_stops_exactly_the_disabled_side_bypass_marked(self):
        ctrl, mdp, _, _ = self._with_resting_both_sides(
            enable_buys=False, cancel_disabled_side_orders=True)
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual({"b0", "b1"}, {a.executor_id for a in self._stops(actions)})
        # Bypass-marked: the cancel starts no residual cooldown on those levels.
        self.assertTrue(ctrl._should_bypass_level_cooldown("buy_321"))
        self.assertTrue(ctrl._should_bypass_level_cooldown("buy_318"))
        self.assertFalse(ctrl._should_bypass_level_cooldown("sell_350"))
        events = self._events(ctrl, "range_ladder_side_disabled_stop")
        self.assertEqual({("b0", "buy_321", "buy"), ("b1", "buy_318", "buy")},
                         {(e.kwargs["executor_id"], e.kwargs["level_id"], e.kwargs["side"])
                          for e in events})

    def test_true_does_not_return_early_enabled_side_refreshes_same_cycle(self):
        ctrl, mdp, _, resting = self._with_resting_both_sides(
            enable_buys=False, cancel_disabled_side_orders=True)
        # A sell-side refresh trigger pending in the SAME cycle as the disabled-buy cancel:
        # remove one sell so the rebuild is not a converged noop.
        ctrl.executors_info = [e for e in resting if e.id != "s1"]
        ctrl._mark_side_dirty("sell", "global_timer")
        actions = self._full(ctrl, mdp, 1001.0)
        stop_ids = {a.executor_id for a in self._stops(actions)}
        self.assertIn("b0", stop_ids)
        self.assertIn("b1", stop_ids)
        self.assertIn("s0", stop_ids)  # the sell refresh cancel ran in the same cycle

    def test_false_leaves_resting_orders_and_freezes_placement(self):
        ctrl, mdp, _, _ = self._with_resting_both_sides(
            enable_buys=False, cancel_disabled_side_orders=False)
        for t in (1001.0, 1002.0, 1003.0):
            actions = self._full(ctrl, mdp, t)
            buy_stops = [a for a in self._stops(actions)
                         if a.executor_id in ("b0", "b1")]
            self.assertEqual([], buy_stops)
            self.assertEqual([], self._side_creates(actions, TradeType.BUY))
        self.assertEqual([], self._events(ctrl, "range_ladder_side_disabled_stop"))

    def test_stale_dirty_flag_on_disabled_side_is_cleared_without_cancelling(self):
        # The dirty flag was set BEFORE the side was disabled (hot flip mid-refresh):
        # disabling aborts the pending refresh -- with cancel=False the resting orders
        # must ride, and the flag must not spin the refresh path forever.
        ctrl, mdp, _, _ = self._with_resting_both_sides(
            enable_buys=False, cancel_disabled_side_orders=False)
        ctrl._buy_side_dirty = True
        ctrl._buy_dirty_reason = "global_timer"
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._stops(actions))
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))
        self.assertFalse(ctrl._buy_side_dirty)
        self.assertEqual("", ctrl._buy_dirty_reason)

    def test_no_duplicate_stops_when_dirty_and_cancel_true(self):
        ctrl, mdp, _, _ = self._with_resting_both_sides(
            enable_buys=False, cancel_disabled_side_orders=True)
        ctrl._buy_side_dirty = True  # stale pre-disable dirty flag
        ctrl._buy_dirty_reason = "global_timer"
        actions = self._full(ctrl, mdp, 1001.0)
        stop_ids = [a.executor_id for a in self._stops(actions)]
        self.assertEqual(sorted(stop_ids), sorted(set(stop_ids)))  # each stopped ONCE
        self.assertEqual({"b0", "b1"}, set(stop_ids))


# ============================================== 5. default flags = byte-for-byte baseline


class TestDefaultFlagsRegression(_OneSidedHarness):

    @staticmethod
    def _serialize(actions):
        out = []
        for a in actions:
            if hasattr(a, "executor_config"):
                cfg = a.executor_config
                out.append(("create", cfg.level_id, cfg.side.name, str(cfg.price), str(cfg.amount)))
            else:
                out.append(("stop", a.executor_id))
        return out

    def _scripted_run(self, **overrides):
        """A deterministic multi-cycle script: initial build -> materialize -> quiet cycle
        -> forced refresh trigger. Returns the per-cycle serialized action log."""
        ctrl, mdp, _ = self._dual_funded(**overrides)
        log = []
        ctrl._mark_side_dirty("buy", "global_timer")
        ctrl._mark_side_dirty("sell", "global_timer")
        actions = self._full(ctrl, mdp, 1001.0)
        log.append(self._serialize(actions))
        ctrl.executors_info = _materialize(self._creates(actions))
        log.append(self._serialize(self._full(ctrl, mdp, 1002.0)))  # quiet cycle
        ctrl._mark_side_dirty("buy", "sell_fill")                   # forced refresh
        log.append(self._serialize(self._full(ctrl, mdp, 1200.0)))
        log.append(self._serialize(self._full(ctrl, mdp, 1201.0)))
        return log

    def _scripted_legacy_run(self, **overrides):
        overrides.setdefault("event_refresh_enabled", False)
        overrides.setdefault("executor_refresh_time", 100)
        ctrl, mdp, _ = self._dual_funded(**overrides)
        log = [self._serialize(self._full(ctrl, mdp, 1001.0))]      # places every cycle
        resting = _materialize(self._creates(self._full(ctrl, mdp, 1002.0)))
        ctrl.executors_info = resting                               # timestamp 0 -> stale
        log.append(self._serialize(self._full(ctrl, mdp, 1105.0)))  # age refresh cancels
        log.append(self._serialize(self._full(ctrl, mdp, 1106.0)))
        return log

    def test_event_mode_defaults_identical_to_baseline(self):
        baseline = self._scripted_run()
        explicit = self._scripted_run(enable_buys=True, enable_sells=True,
                                      cancel_disabled_side_orders=True)
        self.assertEqual(baseline, explicit)
        self.assertTrue(any(step for step in baseline))  # the script actually did work

    def test_legacy_mode_defaults_identical_to_baseline(self):
        baseline = self._scripted_legacy_run()
        explicit = self._scripted_legacy_run(enable_buys=True, enable_sells=True,
                                             cancel_disabled_side_orders=True)
        self.assertEqual(baseline, explicit)
        self.assertTrue(any(step for step in baseline))

    def test_defaults_emit_no_one_sided_events(self):
        ctrl, mdp, _ = self._dual_funded()
        ctrl._mark_side_dirty("buy", "global_timer")
        self._full(ctrl, mdp, 1001.0)
        self._full(ctrl, mdp, 1002.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_side_disabled"))
        self.assertEqual([], self._events(ctrl, "range_ladder_side_disabled_stop"))
        self.assertEqual([], self._events(ctrl, "range_ladder_side_reenabled"))


# ============================================== 6. both sides disabled = soft-pause


class TestBothSidesDisabled(_OneSidedHarness):

    def test_config_permits_both_disabled(self):
        config = RangeInventoryLadderConfig(
            id="cfg-both-off",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("200"),
            buy_prices=[D("321"), D("318"), D("315")],
            sell_prices=[D("350"), D("355"), D("360")],
            enable_buys=False,
            enable_sells=False,
        )
        self.assertFalse(config.enable_buys)
        self.assertFalse(config.enable_sells)

    def test_soft_pause_places_nothing_and_keeps_ledger_intact(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False, enable_sells=False)
        ctrl._last_global_refresh_ts = -1_000_000.0  # every trigger firing at once
        for t in (1001.0, 1002.0, 1100.0):
            actions = self._full(ctrl, mdp, t)
            self.assertEqual([], self._creates(actions))
            self.assertEqual([], self._stops(actions))
        # Ledger / fund / seed state untouched by the pause.
        self.assertEqual(QUOTE_OWNED, ctrl._state["owned_quote"])
        self.assertEqual(BASE_OWNED, ctrl._state["owned_base"])
        self.assertEqual("800", ctrl._state["seed_value_quote"])
        self.assertEqual(D(QUOTE_OWNED), ctrl.processed_data["managed_quote_total"])
        self.assertEqual(D(BASE_OWNED), ctrl.processed_data["managed_base_total"])

    def test_soft_pause_cancels_existing_iff_flag(self):
        for cancel_flag, expected_stop_ids in ((True, {"b0", "s0"}), (False, set())):
            with self.subTest(cancel=cancel_flag):
                ctrl, mdp, _ = self._dual_funded(enable_buys=False, enable_sells=False,
                                                 cancel_disabled_side_orders=cancel_flag)
                ctrl.executors_info = [
                    _resting("buy_321", TradeType.BUY, "321", "0.2", "b0"),
                    _resting("sell_350", TradeType.SELL, "350", "0.5", "s0"),
                ]
                actions = self._full(ctrl, mdp, 1001.0)
                self.assertEqual(expected_stop_ids,
                                 {a.executor_id for a in self._stops(actions)})
                self.assertEqual([], self._creates(actions))

    def test_resumes_cleanly_when_a_side_is_reenabled(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False, enable_sells=False)
        self._full(ctrl, mdp, 1001.0)
        ctrl.config.enable_sells = True
        ctrl._mark_side_dirty("sell", "global_timer")
        actions = self._full(ctrl, mdp, 1002.0)
        self.assertEqual({"sell_350", "sell_355", "sell_360"},
                         {a.executor_config.level_id
                          for a in self._side_creates(actions, TradeType.SELL)})
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))


# ============================================== 7. watchdogs stay silent on a disabled side


class TestWatchdogSkip(_OneSidedHarness):

    def test_empty_side_watchdog_never_fires_for_the_disabled_side(self):
        # Both sides empty AND fundable: without the skip, BOTH would fire after 60s.
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        t = 1001.0
        fired_sides = set()
        while t < 1200.0:
            self._full(ctrl, mdp, t)
            for e in self._events(ctrl, "range_ladder_empty_side_watchdog_fired"):
                fired_sides.add(e.kwargs["side"])
            # Consume the sell rebuild so the sell watchdog can re-evaluate cleanly.
            ctrl._sell_side_dirty = False
            ctrl._sell_dirty_reason = ""
            t += 20.0
        self.assertIn("sell", fired_sides)      # the enabled side's watchdog still works
        self.assertNotIn("buy", fired_sides)    # the disabled side never fires
        self.assertIsNone(ctrl._side_empty_since["buy"])
        self.assertFalse(ctrl._buy_side_dirty)

    def test_underdeployed_watchdog_state_is_reset_for_the_disabled_side(self):
        # A resting dust-sized buy plus a big free quote budget is the classic
        # under-deployed shape; the disabled side must skip it and clear its episode state.
        ctrl, mdp, _ = self._dual_funded(enable_buys=False,
                                         cancel_disabled_side_orders=False)
        ctrl.executors_info = [_resting("buy_321", TradeType.BUY, "321", "0.01", "b0")]
        ctrl._side_underdeployed_since["buy"] = 900.0        # stale pre-disable episode
        ctrl._underdeployed_suppressed_logged["buy"] = True
        ctrl._underdeployed_fresh_balance_ts["buy"] = 950.0
        t = 1001.0
        while t < 1400.0:                                    # past the 300s window
            self._full(ctrl, mdp, t)
            t += 60.0
        self.assertEqual([], [e for e in
                              self._events(ctrl, "range_ladder_underdeployed_watchdog_fired")
                              if e.kwargs["side"] == "buy"])
        self.assertIsNone(ctrl._side_underdeployed_since["buy"])
        self.assertFalse(ctrl._underdeployed_suppressed_logged["buy"])
        self.assertIsNone(ctrl._underdeployed_fresh_balance_ts["buy"])
        self.assertFalse(ctrl._buy_side_dirty)


# ============================================== 8. re-enable resumes normally


class TestReEnable(_OneSidedHarness):

    def test_reenabled_side_rebuilds_on_the_next_trigger(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._side_creates(actions, TradeType.BUY))
        ctrl.config.enable_buys = True                       # hot re-enable
        ctrl._last_global_refresh_ts = -1_000_000.0          # next trigger fires
        actions = self._full(ctrl, mdp, 1002.0)
        buys = self._side_creates(actions, TradeType.BUY)
        self.assertEqual({"buy_321", "buy_318", "buy_315"},
                         {a.executor_config.level_id for a in buys})
        # Sized from the current budget: total notional fits the free quote budget.
        issued = sum((a.executor_config.amount * a.executor_config.price for a in buys),
                     Decimal("0"))
        self.assertLessEqual(issued, ctrl.processed_data["free_buy_budget_quote"])

    def test_no_stale_watchdog_or_cooldown_state_blocks_the_rebuild(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        # While disabled, the watchdog skip keeps clearing this state every cycle.
        self._full(ctrl, mdp, 1001.0)
        self.assertIsNone(ctrl._side_empty_since["buy"])
        self.assertIsNone(ctrl._side_underdeployed_since["buy"])
        self.assertFalse(ctrl._buy_cooldown_armed)
        ctrl.config.enable_buys = True
        ctrl._mark_side_dirty("buy", "global_timer")         # gate no longer drops it
        self.assertTrue(ctrl._buy_side_dirty)
        actions = self._full(ctrl, mdp, 1002.0)
        self.assertEqual(3, len(self._side_creates(actions, TradeType.BUY)))


# ============================================== 9. legacy mode (event_refresh_enabled=False)


class TestLegacyMode(_OneSidedHarness):

    def test_disabled_side_places_nothing_enabled_side_places_every_cycle(self):
        ctrl, mdp, _ = self._dual_funded(event_refresh_enabled=False, enable_buys=False)
        for t in (1001.0, 1002.0, 1003.0):
            actions = self._full(ctrl, mdp, t)
            self.assertEqual([], self._side_creates(actions, TradeType.BUY))
            self.assertEqual({"sell_350", "sell_355", "sell_360"},
                             {a.executor_config.level_id
                              for a in self._side_creates(actions, TradeType.SELL)})

    def test_enabled_side_still_refreshes_by_age_disabled_side_rides(self):
        ctrl, mdp, _ = self._dual_funded(event_refresh_enabled=False,
                                         executor_refresh_time=100,
                                         enable_buys=False,
                                         cancel_disabled_side_orders=False)
        ctrl.executors_info = [
            _resting("buy_321", TradeType.BUY, "321", "0.2", "b0"),      # timestamp 0: stale
            _resting("sell_350", TradeType.SELL, "350", "0.5", "s0"),    # timestamp 0: stale
        ]
        actions = self._full(ctrl, mdp, 1105.0)
        stop_ids = {a.executor_id for a in self._stops(actions)}
        self.assertIn("s0", stop_ids)        # enabled side: age refresh as usual
        self.assertNotIn("b0", stop_ids)     # disabled side: left to fill naturally
        refresh_stops = self._events(ctrl, "range_ladder_refresh_stop")
        self.assertEqual({"s0"}, {e.kwargs["executor_id"] for e in refresh_stops})

    def test_cancel_true_stops_disabled_side_once_not_twice(self):
        ctrl, mdp, _ = self._dual_funded(event_refresh_enabled=False,
                                         executor_refresh_time=100,
                                         enable_buys=False,
                                         cancel_disabled_side_orders=True)
        # Old enough that the legacy age loop would ALSO want it -- the disabled-side stop
        # must own the cancel and the age loop must skip it (no duplicate stop actions).
        ctrl.executors_info = [_resting("buy_321", TradeType.BUY, "321", "0.2", "b0")]
        actions = self._full(ctrl, mdp, 1105.0)
        stop_ids = [a.executor_id for a in self._stops(actions)]
        self.assertEqual(["b0"], stop_ids)
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_side_disabled_stop")))
        self.assertEqual([], self._events(ctrl, "range_ladder_refresh_stop"))


# ============================================== 10. out-of-range exit interaction


class TestOutOfRangeExitInteraction(_OneSidedHarness):

    def test_disabled_sell_side_never_fires_the_exit_above_range(self):
        # Accumulate-only: spike out the top with enable_sells=False -> do nothing.
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit",
                                         enable_sells=False)
        self.assertEqual("above_sell_range", ctrl.processed_data["price_regime"])
        self.assertGreater(ctrl.processed_data["free_sell_budget_base"], D(0))
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        self.assertEqual([], self._events(ctrl, "range_ladder_out_of_range_exit"))

    def test_disabled_buy_side_never_fires_the_exit_below_range(self):
        ctrl, mdp, _ = self._quote_funded(mid=300, bid=299.9, ask=300.1,
                                          out_of_range_action="market_exit",
                                          enable_buys=False)
        self.assertEqual("below_buy_range", ctrl.processed_data["price_regime"])
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        self.assertEqual([], self._events(ctrl, "range_ladder_out_of_range_exit"))

    def test_enabled_side_exit_still_fires_when_the_other_side_is_disabled(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit",
                                         enable_buys=False)
        actions = self._full(ctrl, mdp, 1001.0)
        oob = self._oob_creates(actions)
        self.assertEqual(1, len(oob))
        self.assertEqual("sell_oob", oob[0].executor_config.level_id)

    def test_reenabling_the_side_rearms_the_exit(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit",
                                         enable_sells=False)
        self.assertEqual([], self._oob_creates(self._full(ctrl, mdp, 1001.0)))
        ctrl.config.enable_sells = True
        actions = self._full(ctrl, mdp, 1002.0)
        self.assertEqual(1, len(self._oob_creates(actions)))


# ============================================== config plumbing


class TestConfigPlumbing(_OneSidedHarness):

    def test_defaults_are_all_true(self):
        ctrl, mdp, _ = self._dual_funded()
        self.assertTrue(ctrl.config.enable_buys)
        self.assertTrue(ctrl.config.enable_sells)
        self.assertTrue(ctrl.config.cancel_disabled_side_orders)

    def test_flags_are_not_part_of_the_runtime_config_signature(self):
        ctrl, mdp, _ = self._dual_funded()
        sig_before = ctrl._runtime_config_signature()
        ctrl.config.enable_buys = False
        ctrl.config.enable_sells = False
        ctrl.config.cancel_disabled_side_orders = False
        self.assertEqual(sig_before, ctrl._runtime_config_signature())

    def test_hot_flip_never_triggers_a_config_rebuild_wave(self):
        ctrl, mdp, _ = self._dual_funded()
        self._full(ctrl, mdp, 1001.0)
        ctrl.config.enable_buys = False
        self._full(ctrl, mdp, 1002.0)
        self.assertFalse(ctrl._config_rebuild_pending)
        self.assertEqual([], self._events(ctrl, "range_ladder_runtime_config_rebuild_stop"))
        ctrl.config.enable_buys = True
        self._full(ctrl, mdp, 1003.0)
        self.assertFalse(ctrl._config_rebuild_pending)

    def test_price_list_validators_untouched(self):
        with self.assertRaises(Exception):
            RangeInventoryLadderConfig(
                id="cfg-empty",
                controller_name="range_inventory_ladder",
                controller_type="market_making",
                connector_name="nonkyc",
                trading_pair="XMR-USDT",
                total_amount_quote=Decimal("200"),
                buy_prices=[],                       # still rejected: the flags are the
                sell_prices=[D("350")],              # supported way to run one-sided
                enable_buys=False,
            )


# ============================================== observability


class TestObservability(_OneSidedHarness):

    def test_custom_info_reports_the_flags(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False,
                                         cancel_disabled_side_orders=False)
        info = ctrl.get_custom_info()
        self.assertEqual("False", info["enable_buys"])
        self.assertEqual("True", info["enable_sells"])
        self.assertEqual("False", info["cancel_disabled_side_orders"])

    def test_format_status_banner_when_a_side_is_disabled(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False)
        status = "\n".join(ctrl.to_format_status())
        self.assertIn("BUY SIDE DISABLED", status)
        self.assertIn("resting orders are cancelled", status)
        self.assertNotIn("SELL SIDE DISABLED", status)

    def test_format_status_banner_shows_ride_mode_and_both_sides(self):
        ctrl, mdp, _ = self._dual_funded(enable_buys=False, enable_sells=False,
                                         cancel_disabled_side_orders=False)
        status = "\n".join(ctrl.to_format_status())
        self.assertIn("BUY SIDE DISABLED", status)
        self.assertIn("SELL SIDE DISABLED", status)
        self.assertIn("left to fill naturally", status)

    def test_format_status_silent_with_defaults(self):
        ctrl, mdp, _ = self._dual_funded()
        status = "\n".join(ctrl.to_format_status())
        self.assertNotIn("SIDE DISABLED", status)

    def test_disabled_event_fires_once_per_transition_not_every_cycle(self):
        ctrl, mdp, _ = self._dual_funded()
        self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_side_disabled"))
        ctrl.config.enable_buys = False
        self._full(ctrl, mdp, 1002.0)
        self._full(ctrl, mdp, 1003.0)
        events = self._events(ctrl, "range_ladder_side_disabled")
        self.assertEqual(1, len(events))
        self.assertEqual("buy", events[0].kwargs["side"])
        self.assertTrue(events[0].kwargs["cancel_disabled_side_orders"])
        ctrl.config.enable_buys = True
        self._full(ctrl, mdp, 1004.0)
        self._full(ctrl, mdp, 1005.0)
        reenabled = self._events(ctrl, "range_ladder_side_reenabled")
        self.assertEqual(1, len(reenabled))
        self.assertEqual("buy", reenabled[0].kwargs["side"])

    def test_controller_started_disabled_emits_the_event_once(self):
        ctrl, mdp, _ = self._dual_funded(enable_sells=False)
        self._full(ctrl, mdp, 1001.0)
        self._full(ctrl, mdp, 1002.0)
        events = self._events(ctrl, "range_ladder_side_disabled")
        self.assertEqual(1, len(events))
        self.assertEqual("sell", events[0].kwargs["side"])


if __name__ == "__main__":
    unittest.main()
