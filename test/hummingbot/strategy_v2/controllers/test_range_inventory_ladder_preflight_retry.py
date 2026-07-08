"""Preflight-retry fix tests (the 2026-07-07 19:18 Kraken dust-sell failure).

Observed: a buy fill triggered the sell refresh; all 8 cancels confirmed within ~1s; the
re-propose then hit the framework budget preflight, which consulted the connector's STALE
cached balance (available XMR = only the freshly bought 0.0154), resized the first sell to
dust and dropped the remaining 8 terminally. The dust order defeated the zero-level
watchdog's emptiness check; 1.06 XMR sat idle for 23+ minutes.

Under test:
- Intended-vs-live reconciliation: per-level placement tracking with backoff
  (reconcile_retry_seconds) and a cap (reconcile_max_attempts) per refresh generation;
  heal rebuilds place ONLY the missing rungs, sized from the current effective budget.
- Preflight feedback: on_budget_preflight_result() marks the wave for immediate retry;
  live dust orders (below preflight_min_fill_ratio of intended) are stopped and retried.
- Post-cancel balance refresh: one forced connector._update_balances() per wave once the
  cancels close, plus a placement gate until the freed collateral is visible in the cache
  or post_cancel_balance_timeout_seconds elapses.
"""
import asyncio
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import (  # noqa: E402
    CreateExecutorAction,
    StopExecutorAction,
)

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731

OWNED = "1.06036199"


def _make_mdp(*, balances, mid=335, bid=334.9, ask=335.1, now=1000.0, connector_style="kraken"):
    """balances: {asset: [total, available]} (mutable). connector_style:
    'kraken'  -> REST-poll balances, LIMIT_MAKER supported
    'nonkyc'  -> WS balance push style flag, LIMIT only (fallback path)"""
    mdp = MagicMock()
    mdp.time.return_value = now
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)
    connector = MagicMock()
    if connector_style == "nonkyc":
        connector.supported_order_types.return_value = [OrderType.LIMIT]
        connector.is_balance_settling = False
    else:
        connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    connector._update_balances = AsyncMock()
    mdp.get_connector.return_value = connector
    return mdp


def _resting(level_id, side, price, amount, eid, status=RunnableStatus.RUNNING, close_ts=None):
    ex = MagicMock()
    ex.id = eid
    ex.status = status
    ex.is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    ex.timestamp = 0.0
    ex.close_timestamp = close_ts
    ex.connector_name = "nonkyc"
    ex.custom_info = {}
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = D(price)
    cfg.amount = D(amount)
    ex.config = cfg
    return ex


def _filled_exec(level_id, side, price, eid, *, filled_base, filled_quote, close_ts, fees="0"):
    ex = _resting(level_id, side, price, filled_base, eid,
                  status=RunnableStatus.TERMINATED, close_ts=close_ts)
    ex.custom_info = {
        "filled_amount_base": D(filled_base),
        "filled_amount_quote": D(filled_quote),
        "cum_fees_quote": D(fees),
    }
    return ex


def _terminate(executors, close_ts):
    for ex in executors:
        ex.status = RunnableStatus.TERMINATED
        ex.is_active = False
        ex.close_timestamp = close_ts


def _materialize(creates, prefix="m"):
    return [_resting(a.executor_config.level_id, a.executor_config.side,
                     a.executor_config.price, a.executor_config.amount, f"{prefix}{i}")
            for i, a in enumerate(creates)]


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        defaults = dict(
            id="ctrl-pfr",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("200"),
            max_fund_value_quote=Decimal("5000"),
            buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
            buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
            sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            min_order_quote=Decimal("5"),
            executor_refresh_time=100_000,
            buy_cooldown_time=120,
            sell_cooldown_time=120,
            ledger_overclaim_reanchor_seconds=999_999,
            fee_rate=Decimal("0"),
        )
        defaults.update(config_overrides)
        config = RangeInventoryLadderConfig(**defaults)
        controller = RangeInventoryLadderController(
            config, market_data_provider=mdp, actions_queue=MagicMock()
        )
        controller._emit_structured = MagicMock()
        patcher = patch.object(
            type(controller), "state_path", new_callable=PropertyMock,
            return_value=self._state_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        controller.executors_info = []
        controller.positions_held = []
        return controller

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value):
        ctrl._state = {
            "initialized": True,
            "owned_quote": str(owned_quote),
            "owned_base": str(owned_base),
            "seed_value_quote": str(seed_value),
            "initial_managed_quote": str(owned_quote),
            "initial_claimed_base_amount": str(owned_base),
            "initial_reference_price": "335",
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True

    def _prime(self, ctrl, mdp, now):
        mdp.time.return_value = now
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = False
        ctrl._sell_side_dirty = False
        ctrl._buy_dirty_reason = ""
        ctrl._sell_dirty_reason = ""
        ctrl._buy_cooldown_armed = False
        ctrl._sell_cooldown_armed = False
        ctrl._last_global_refresh_ts = now
        ctrl._side_empty_since = {"buy": None, "sell": None}
        ctrl._refresh_wave = {"buy": None, "sell": None}

    def _full(self, ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())
        return ctrl.determine_executor_actions()

    @staticmethod
    def _stops(actions):
        return [a for a in actions if isinstance(a, StopExecutorAction)]

    @staticmethod
    def _creates(actions):
        return [a for a in actions if isinstance(a, CreateExecutorAction)]

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]

    def _sell_wave_setup(self, connector_style="kraken", **config_overrides):
        """The 19:18 shape: all base inside two resting sells, stale wallet cache."""
        balances = {"XMR": [D(OWNED), D("7E-8")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, connector_style=connector_style)
        ctrl = self._build(mdp, **config_overrides)
        self._init_state(ctrl, owned_quote=0, owned_base=OWNED, seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "0.6", "s0"),
                 _resting("sell_355", TradeType.SELL, "355", "0.46036199", "s1")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "buy_fill"
        actions = self._full(ctrl, mdp, 2000.0)
        assert {"s0", "s1"} == {a.executor_id for a in self._stops(actions)}
        _terminate(sells, 2001.0)  # Kraken confirmed the cancels within ~1s
        return ctrl, mdp, balances


# ================================ 1 + 9: the observed failure, both connector styles

class TestStaleBalanceRegression(_Harness):

    def _run_regression(self, connector_style):
        ctrl, mdp, balances = self._sell_wave_setup(connector_style=connector_style)
        connector = mdp.get_connector.return_value

        # Cancels closed; the forced balance refresh fires and the gate holds placement
        # while the cache still shows 7E-8 available.
        actions = self._full(ctrl, mdp, 2002.0)
        self.assertEqual(1, connector._update_balances.call_count)
        self.assertEqual([], self._creates(actions))
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_post_cancel_balance_refresh")))

        actions = self._full(ctrl, mdp, 2005.0)          # still stale, still inside 5s gate
        self.assertEqual([], self._creates(actions))

        actions = self._full(ctrl, mdp, 2008.0)          # gate timeout -> place anyway
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_post_cancel_balance_gate_timeout")))
        issued = self._creates(actions)
        self.assertEqual(3, len(issued))
        intended_levels = {a.executor_config.level_id for a in issued}
        # ... and the budget preflight drops ALL of them on the stale snapshot
        # (simulated: no executor ever materializes).

        actions = self._full(ctrl, mdp, 2009.0)          # heal attempt 1 (immediate)
        heals = self._events(ctrl, "range_ladder_intended_vs_live_heal")
        self.assertEqual(1, len(heals))
        self.assertEqual(sorted(intended_levels), heals[0].kwargs["missing_levels"])
        retry_1 = self._creates(actions)
        self.assertEqual(intended_levels, {a.executor_config.level_id for a in retry_1})

        actions = self._full(ctrl, mdp, 2012.0)          # inside the 15s backoff
        self.assertEqual([], self._creates(actions))

        # The mock balance finally catches up; the next retry is placed and materializes.
        balances["XMR"] = [D(OWNED), D(OWNED)]
        actions = self._full(ctrl, mdp, 2025.0)          # heal attempt 2
        retry_2 = self._creates(actions)
        self.assertEqual(intended_levels, {a.executor_config.level_id for a in retry_2})
        ctrl.executors_info = _materialize(retry_2)

        actions = self._full(ctrl, mdp, 2026.0)
        self.assertEqual([], self._creates(actions))     # nothing left to heal
        self.assertIsNone(ctrl._refresh_wave["sell"])    # wave resolved
        # The final live ladder equals the intended ladder.
        live = {e.config.level_id: e.config.amount for e in ctrl.executors_info}
        self.assertEqual({a.executor_config.level_id: a.executor_config.amount for a in retry_2}, live)
        total = sum(live.values(), D(0))
        self.assertGreaterEqual(total, D(OWNED) - D("0.000003"))
        return retry_2

    def test_kraken_style_rest_poll_balances(self):
        self._run_regression("kraken")

    def test_nonkyc_style_limit_fallback(self):
        creates = self._run_regression("nonkyc")
        for action in creates:                            # no LIMIT_MAKER on nonkyc
            self.assertEqual(ExecutionStrategy.LIMIT, action.executor_config.execution_strategy)


# ================================ 2: dust-resize handling (controller backstop)

class TestDustResizeBackstop(_Harness):

    def test_live_dust_order_is_stopped_and_retried_at_full_size(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        actions = self._full(ctrl, mdp, 2002.0)
        issued = self._creates(actions)
        self.assertEqual(3, len(issued))
        intended_350 = next(a.executor_config.amount for a in issued
                            if a.executor_config.level_id == "sell_350")

        # The preflight resized the first order to dust (the 0.0154-of-0.0512 case) and
        # dropped the rest: only a dust-sized sell_350 materializes.
        dust = _resting("sell_350", TradeType.SELL, "350", str(intended_350 * D("0.1")), "dust0")
        ctrl.executors_info = [dust]

        actions = self._full(ctrl, mdp, 2003.0)
        dust_events = self._events(ctrl, "range_ladder_reconcile_dust_stop")
        self.assertEqual(1, len(dust_events))
        self.assertEqual("sell_350", dust_events[0].kwargs["level_id"])
        self.assertEqual({"dust0"}, {a.executor_id for a in self._stops(actions)})
        self.assertEqual([], self._creates(actions))     # its cancel must settle first
        self.assertIn("sell_350", ctrl._refresh_wave["sell"]["intended"])  # still owed

        _terminate([dust], 2004.0)
        balances["XMR"] = [D(OWNED), D(OWNED)]
        actions = self._full(ctrl, mdp, 2005.0)          # heal: all 3 at full size
        healed = self._creates(actions)
        self.assertEqual({"sell_350", "sell_355", "sell_360"},
                         {a.executor_config.level_id for a in healed})
        healed_350 = next(a.executor_config.amount for a in healed
                          if a.executor_config.level_id == "sell_350")
        self.assertGreaterEqual(healed_350, intended_350 * D("0.25"))

    def test_adequately_sized_order_is_satisfied_not_stopped(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        keep = next(a for a in issued if a.executor_config.level_id == "sell_350")
        # 30% of intended >= the 25% ratio -> satisfied, never stopped.
        ok_order = _resting("sell_350", TradeType.SELL, "350",
                            str(keep.executor_config.amount * D("0.3")), "ok0")
        ctrl.executors_info = [ok_order]

        actions = self._full(ctrl, mdp, 2003.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_reconcile_dust_stop"))
        self.assertEqual([], self._stops(actions))
        self.assertNotIn("sell_350", ctrl._refresh_wave["sell"]["intended"])


# ================================ 3: intended-vs-live over arbitrary subsets

class TestReconciliationSubsets(_Harness):

    def _issue(self, **overrides):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0,
                                                    **overrides)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        self.assertEqual(3, len(issued))
        return ctrl, mdp, balances, issued

    def test_one_of_three_missing_heals_only_the_gap(self):
        ctrl, mdp, balances, issued = self._issue()
        survivors = _materialize([a for a in issued
                                  if a.executor_config.level_id != "sell_360"])
        ctrl.executors_info = survivors
        # Wallet: the two survivors' base is held; the missing rung's base is "free" but the
        # cache is still stale-low -- the wave credit covers it.
        held = sum((e.config.amount for e in survivors), D(0))
        balances["XMR"] = [D(OWNED), D(OWNED) - held]

        actions = self._full(ctrl, mdp, 2003.0)
        self.assertEqual([], self._stops(actions))       # survivors untouched
        healed = self._creates(actions)
        self.assertEqual(["sell_360"], [a.executor_config.level_id for a in healed])
        self.assertLessEqual(healed[0].executor_config.amount, D(OWNED) - held)

    def test_all_three_missing_heals_everything_with_legacy_event(self):
        ctrl, mdp, balances, issued = self._issue()
        actions = self._full(ctrl, mdp, 2003.0)          # nothing materialized
        healed = self._creates(actions)
        self.assertEqual({a.executor_config.level_id for a in issued},
                         {a.executor_config.level_id for a in healed})
        # backward-compatible event for the full-loss case
        legacy = self._events(ctrl, "range_ladder_deferred_creates_reproposed")
        self.assertEqual(1, len(legacy))
        self.assertEqual(3, legacy[0].kwargs["deferred_count"])

    def test_all_three_live_resolves_without_healing(self):
        ctrl, mdp, balances, issued = self._issue()
        ctrl.executors_info = _materialize(issued)
        actions = self._full(ctrl, mdp, 2003.0)
        self.assertEqual([], self._creates(actions))
        self.assertEqual([], self._events(ctrl, "range_ladder_intended_vs_live_heal"))
        self.assertIsNone(ctrl._refresh_wave["sell"])


# ================================ 4 + 8: retry cap, no preflight weakening

class TestRetryCap(_Harness):

    def test_retries_stop_at_cap_with_warning_and_reset_on_new_generation(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0,
                                                    reconcile_retry_seconds=15,
                                                    reconcile_max_attempts=3)
        self._full(ctrl, mdp, 2002.0)                    # issuance; balance never recovers

        t = 2003.0
        for expected_attempt in (1, 2, 3):
            actions = self._full(ctrl, mdp, t)
            heals = self._events(ctrl, "range_ladder_intended_vs_live_heal")
            self.assertEqual(expected_attempt, len(heals))
            self.assertEqual(expected_attempt, heals[-1].kwargs["attempt"])
            self.assertEqual(3, len(self._creates(actions)))
            t += 16.0

        # Cap reached: exactly one WARNING event, then silence -- no unbounded order spam,
        # and (genuinely insufficient balance) the orders stay dropped: the preflight was
        # never bypassed, the controller simply stops re-proposing.
        for _ in range(4):
            actions = self._full(ctrl, mdp, t)
            self.assertEqual([], self._creates(actions))
            t += 20.0
        cap_events = self._events(ctrl, "range_ladder_reconcile_retry_cap")
        self.assertEqual(1, len(cap_events))
        self.assertEqual(3, cap_events[0].kwargs["attempts"])
        # The zero-level watchdog yields to the capped wave: no watchdog-driven spam either.
        self.assertEqual([], self._events(ctrl, "range_ladder_empty_side_watchdog_fired"))

        # A newer refresh generation resets the retry state and healing resumes.
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())
        ctrl._mark_side_dirty("sell", "buy_fill")
        actions = ctrl.determine_executor_actions()
        self.assertEqual(3, len(self._creates(actions)))
        record = ctrl._refresh_wave["sell"]
        self.assertEqual(0, record["attempts"])
        self.assertFalse(record["cap_warned"])
        actions = self._full(ctrl, mdp, t + 1.0)         # dropped again -> healing resumed
        self.assertEqual(3, len(self._creates(actions)))
        self.assertEqual(4, len(self._events(ctrl, "range_ladder_intended_vs_live_heal")))


# ================================ 5: supersession

class TestSupersession(_Harness):

    def test_newer_refresh_replaces_pending_intent(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        self._full(ctrl, mdp, 2002.0)                    # issuance, dropped
        self._full(ctrl, mdp, 2003.0)                    # heal attempt 1, dropped again
        self.assertEqual(1, ctrl._refresh_wave["sell"]["attempts"])

        # A newer refresh trigger owns the side before the next retry.
        mdp.time.return_value = 2010.0
        asyncio.run(ctrl.update_processed_data())
        ctrl._mark_side_dirty("sell", "sell_cooldown_lapsed")
        actions = ctrl.determine_executor_actions()
        newest = self._creates(actions)
        self.assertEqual(3, len(newest))
        level_ids = [a.executor_config.level_id for a in newest]
        self.assertEqual(len(level_ids), len(set(level_ids)))          # no duplicates
        record = ctrl._refresh_wave["sell"]
        self.assertEqual(set(level_ids), set(record["intended"].keys()))  # intent REPLACED
        self.assertEqual(0, record["attempts"])                        # retry state reset

        ctrl.executors_info = _materialize(newest)
        self._full(ctrl, mdp, 2011.0)
        self.assertIsNone(ctrl._refresh_wave["sell"])                  # only the newest placed


# ================================ 6: post-cancel balance refresh + gate

class TestPostCancelBalanceRefresh(_Harness):

    def test_refresh_fires_once_after_cancels_close_and_before_placement(self):
        ctrl, mdp, balances = self._sell_wave_setup()
        connector = mdp.get_connector.return_value
        self.assertEqual(0, connector._update_balances.call_count)  # not before close

        self._full(ctrl, mdp, 2002.0)
        self.assertEqual(1, connector._update_balances.call_count)
        self._full(ctrl, mdp, 2003.0)
        self._full(ctrl, mdp, 2004.0)
        self.assertEqual(1, connector._update_balances.call_count)  # one-shot per wave

    def test_gate_releases_early_when_balance_catches_up(self):
        ctrl, mdp, balances = self._sell_wave_setup()
        actions = self._full(ctrl, mdp, 2002.0)
        self.assertEqual([], self._creates(actions))     # gated on the stale cache

        balances["XMR"] = [D(OWNED), D(OWNED)]           # the refresh landed
        actions = self._full(ctrl, mdp, 2003.0)          # well inside the 5s window
        self.assertEqual(3, len(self._creates(actions)))
        self.assertEqual([], self._events(ctrl, "range_ladder_post_cancel_balance_gate_timeout"))

    def test_timeout_zero_disables_gate_but_still_fires_refresh(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        connector = mdp.get_connector.return_value
        actions = self._full(ctrl, mdp, 2002.0)
        self.assertEqual(3, len(self._creates(actions)))  # no gate
        self.assertEqual(1, connector._update_balances.call_count)


# ================================ 7: partial fill during the retry window

class TestPartialFillDuringRetry(_Harness):

    def test_fill_is_satisfied_and_recomputed_amounts_do_not_overcommit(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        filled_action = next(a for a in issued if a.executor_config.level_id == "sell_350")
        filled_base = filled_action.executor_config.amount

        # sell_350 materialized AND filled during the retry window; siblings were dropped.
        fill = _filled_exec("sell_350", TradeType.SELL, "350", "f0",
                            filled_base=str(filled_base),
                            filled_quote=str(filled_base * D("350")),
                            close_ts=2003.0)
        ctrl.executors_info = [fill]
        remaining_base = D(OWNED) - filled_base
        balances["XMR"] = [remaining_base, remaining_base]
        balances["USDT"] = [filled_base * D("350"), filled_base * D("350")]

        actions = self._full(ctrl, mdp, 2004.0)
        # The booked sell fill legitimately dirties the BUY side too (cross-side refresh
        # deploying the new quote) -- assert on the SELL-side heal specifically.
        healed = [a for a in self._creates(actions) if a.executor_config.side == TradeType.SELL]
        healed_levels = {a.executor_config.level_id for a in healed}
        self.assertEqual({"sell_355", "sell_360"}, healed_levels)   # the FILL is never re-placed
        total = sum((a.executor_config.amount for a in healed), D(0))
        self.assertLessEqual(total, remaining_base)                 # no over-commit
        self.assertGreater(total, remaining_base * D("0.95"))
        # ... and the cross-side buy deployment spends only the fill's proceeds.
        buys = [a for a in self._creates(actions) if a.executor_config.side == TradeType.BUY]
        buy_notional = sum((a.executor_config.amount * a.executor_config.price for a in buys), D(0))
        self.assertLessEqual(buy_notional, filled_base * D("350"))


# ================================ preflight feedback hook

class TestPreflightFeedback(_Harness):

    def test_feedback_resets_backoff_for_immediate_retry(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        self._full(ctrl, mdp, 2003.0)                    # heal attempt 1 -> backoff to 2018
        record = ctrl._refresh_wave["sell"]
        self.assertGreater(record["next_retry_ts"], 2003.0)

        ctrl.on_budget_preflight_result(
            action=issued[0], result="dropped",
            original_amount=issued[0].executor_config.amount,
            adjusted_amount=Decimal("0"), reason="insufficient_balance")

        self.assertEqual(0.0, record["next_retry_ts"])
        self.assertEqual(1, record["preflight_drops"])
        feedback = self._events(ctrl, "range_ladder_preflight_feedback")
        self.assertEqual(1, len(feedback))
        self.assertEqual("sell", feedback[0].kwargs["side"])

        actions = self._full(ctrl, mdp, 2004.0)          # retried despite the 15s backoff
        self.assertEqual(3, len(self._creates(actions)))

    def test_actions_carry_min_fill_ratio(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        for action in issued:
            self.assertEqual(D("0.25"), action.min_fill_ratio)

    def test_ratio_zero_disables_the_hint(self):
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0,
                                                    preflight_min_fill_ratio=Decimal("0"))
        issued = self._creates(self._full(ctrl, mdp, 2002.0))
        for action in issued:
            self.assertIsNone(action.min_fill_ratio)


# ================================ plan/budget invariant (2026-07-08 addendum)

class TestPlanBudgetInvariant(_Harness):

    EVENT = "range_ladder_plan_budget_invariant_violation"

    def _ctrl(self):
        balances = {"XMR": [D("1.0"), D("1.0")], "USDT": [D(100), D(100)]}
        mdp = _make_mdp(balances=balances)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=100, owned_base="1.0", seed_value=435)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl

    def test_violation_warns_and_emits_once_per_window(self):
        ctrl = self._ctrl()
        ctrl._check_plan_budget_invariant("sell", D("1.1"), D("1.0"), "unit")
        events = self._events(ctrl, self.EVENT)
        self.assertEqual(1, len(events))
        self.assertEqual("sell", events[0].kwargs["side"])
        self.assertEqual("0.1", events[0].kwargs["excess"])
        ctrl._check_plan_budget_invariant("sell", D("1.2"), D("1.0"), "unit")  # rate-limited
        self.assertEqual(1, len(self._events(ctrl, self.EVENT)))

    def test_within_epsilon_is_silent(self):
        ctrl = self._ctrl()
        ctrl._check_plan_budget_invariant("sell", D("1.0"), D("1.0"), "unit")
        ctrl._check_plan_budget_invariant("sell", D("1.0000000001"), D("1.0"), "unit")
        ctrl._check_plan_budget_invariant("buy", D("99.99"), D("100"), "unit")
        self.assertEqual([], self._events(ctrl, self.EVENT))

    def test_normal_refresh_and_issuance_never_violate(self):
        # The whole 7E-8 lifecycle from the sell-wave setup: plans and issued creates are
        # sized FROM the budget, so the invariant must stay silent end to end.
        ctrl, mdp, balances = self._sell_wave_setup(post_cancel_balance_timeout_seconds=0)
        self._full(ctrl, mdp, 2002.0)
        self._full(ctrl, mdp, 2003.0)
        self.assertEqual([], self._events(ctrl, self.EVENT))


# ================================ 2026-07-08 session replay (quantified acceptance)

class TestJuly8SessionReplay(_Harness):
    """Replays the corrected 2026-07-08 production sequence with mocked post-cancel balance
    staleness. Acceptance criterion from the prompt: BOTH sides must end at their FULL
    intended ladders within the reconciliation retry window -- the buy side at all 7 rungs
    (~$147.46 reserved, not the observed 4 rungs / $47.57), the sell side at all 8 rungs
    (~0.92210209 XMR) -- and the plan/budget invariant must hold throughout."""

    BUY_PRICES = ["333", "331.5", "330", "328.5", "327", "325.5", "324"]           # 7 rungs
    SELL_PRICES = ["335.5", "337", "338.5", "340", "341.5", "343", "344.5", "346"]  # 8 rungs
    OWNED_QUOTE = D("147.46")
    OWNED_BASE = D("0.92210209")

    def _session(self):
        # Wallet at 00:56: $34.43 settled + $113.03 inside the 7 resting buys; all base
        # inside the 7 resting sells (5E-8 settled).
        balances = {"XMR": [self.OWNED_BASE, D("5E-8")], "USDT": [self.OWNED_QUOTE, D("34.43")]}
        mdp = _make_mdp(balances=balances, mid=334.4, bid=334.3, ask=334.5)
        ctrl = self._build(
            mdp,
            buy_prices=[Decimal(p) for p in self.BUY_PRICES],
            buy_amounts_pct=[Decimal("1")] * 7,
            sell_prices=[Decimal(p) for p in self.SELL_PRICES],
            sell_amounts_pct=[Decimal("1")] * 8,
        )
        self._init_state(ctrl, owned_quote=str(self.OWNED_QUOTE),
                         owned_base=str(self.OWNED_BASE), seed_value=500)
        resting_buys = []
        for i, price in enumerate(self.BUY_PRICES):
            amount = (D("113.03") / 7 / D(price)).quantize(D("1e-8"))
            resting_buys.append(_resting(f"buy_{price.rstrip('0').rstrip('.')}", TradeType.BUY,
                                         price, str(amount), f"ob{i}"))
        resting_sells = []
        for i, price in enumerate(self.SELL_PRICES[:7]):   # 7 resting; the re-plan rests 8
            amount = (self.OWNED_BASE / 7).quantize(D("1e-8"))
            resting_sells.append(_resting(f"sell_{price.rstrip('0').rstrip('.')}", TradeType.SELL,
                                          price, str(amount), f"os{i}"))
        ctrl.executors_info = resting_buys + resting_sells
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances, resting_buys, resting_sells

    def _heal_side_to_full(self, ctrl, mdp, balances, *, side, t0, resting, released_asset,
                           materialize_first_n):
        """One side's 2026-07-08 shape: refresh -> cancels confirmed fast -> stale balance ->
        gate -> issuance -> partial/zero materialization -> reconciliation heals to full."""
        actions = self._full(ctrl, mdp, t0)
        stops = self._stops(actions)
        self.assertEqual({e.id for e in resting}, {a.executor_id for a in stops})
        _terminate(resting, t0 + 1.0)                     # cancels confirmed within ~1s

        actions = self._full(ctrl, mdp, t0 + 2.0)         # stale cache -> gate holds
        self.assertEqual([], self._creates(actions))

        actions = self._full(ctrl, mdp, t0 + 8.0)         # gate timeout -> full plan issued
        issued = [a for a in self._creates(actions) if a.executor_config.side == side]
        # ... the preflight (stale snapshot) lets only the first N through; the rest drop.
        survivors = _materialize(issued[:materialize_first_n], prefix=f"{released_asset}m")
        ctrl.executors_info = [e for e in ctrl.executors_info] + survivors

        heal_t = t0 + 9.0
        for _ in range(12):                               # within the retry window
            actions = self._full(ctrl, mdp, heal_t)
            healed = [a for a in self._creates(actions) if a.executor_config.side == side]
            if healed:
                survivors += _materialize(healed, prefix=f"{released_asset}h{int(heal_t)}")
                ctrl.executors_info = [e for e in ctrl.executors_info] + survivors[-len(healed):]
                # balances settle while the retries run
                if released_asset == "USDT":
                    reserved = sum((e.config.amount * e.config.price for e in survivors), D(0))
                    balances["USDT"] = [self.OWNED_QUOTE, max(D(0), self.OWNED_QUOTE - reserved)]
                else:
                    reserved = sum((e.config.amount for e in survivors), D(0))
                    balances["XMR"] = [self.OWNED_BASE, max(D(0), self.OWNED_BASE - reserved)]
            if ctrl._refresh_wave[("buy" if side == TradeType.BUY else "sell")] is None:
                break
            heal_t += 16.0
        return issued

    def test_both_sides_reach_full_intended_ladders(self):
        ctrl, mdp, balances, resting_buys, resting_sells = self._session()

        # --- 00:56: sell fill -> buy-side event refresh (the side that NEVER recovered).
        ctrl._buy_side_dirty = True
        ctrl._buy_dirty_reason = "sell_fill"
        self._heal_side_to_full(ctrl, mdp, balances, side=TradeType.BUY, t0=2000.0,
                                resting=resting_buys, released_asset="USDT",
                                materialize_first_n=4)    # observed: 4 of 7 placed

        live_buys = [e for e in ctrl.executors_info
                     if e.config.side == TradeType.BUY and e.is_active]
        buy_reserved = sum((e.config.amount * e.config.price for e in live_buys), D(0))
        self.assertEqual(7, len(live_buys))               # all 7 rungs, not 4
        self.assertGreater(buy_reserved, D("146"))        # ~147.46 reserved, not 47.57
        self.assertLessEqual(buy_reserved, self.OWNED_QUOTE)
        self.assertIsNone(ctrl._refresh_wave["buy"])      # buy wave resolved

        # --- 01:56: sell cooldown refresh; ALL 8 rungs dropped on the stale 5E-8 snapshot.
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        self._heal_side_to_full(ctrl, mdp, balances, side=TradeType.SELL, t0=2600.0,
                                resting=resting_sells, released_asset="XMR",
                                materialize_first_n=0)    # observed: dust + 7 drops

        live_sells = [e for e in ctrl.executors_info
                      if e.config.side == TradeType.SELL and e.is_active]
        sell_reserved = sum((e.config.amount for e in live_sells), D(0))
        self.assertEqual(8, len(live_sells))              # the full 8-rung re-plan
        self.assertGreater(sell_reserved, D("0.9220"))    # ~0.92210209, a perfect fit
        self.assertLessEqual(sell_reserved, self.OWNED_BASE)
        self.assertIsNone(ctrl._refresh_wave["sell"])     # sell wave resolved

        # The retracted over-planning bug: the invariant must have stayed silent throughout,
        # and no side hit the retry cap.
        self.assertEqual([], self._events(ctrl, "range_ladder_plan_budget_invariant_violation"))
        self.assertEqual([], self._events(ctrl, "range_ladder_reconcile_retry_cap"))


if __name__ == "__main__":
    unittest.main()
