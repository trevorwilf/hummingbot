"""Refresh-time budget race fixes (the 2026-07-07 live Kraken XMR-USD deadlock).

Observed failure: at a sell-ladder refresh the controller sized the rebuild from
free_sell_budget_base=7E-8 because all 1.06 XMR was still reserved inside the 8 live sell
orders the refresh was about to replace; min-notional compression dropped every level, the
8 orders were cancelled anyway, and with zero executors nothing ever re-triggered placement
-- full free budget, zero orders, permanent deadlock. A second race lost a fill-triggered
buy rebuild when the orchestrator deferred its creates on a stop/create conflict and never
re-proposed them.

Fixes under test:
1. Deterministic refresh budget: a refresh cancel wave records the cancelled executors'
   reservations; budgets credit them back (exact ledger credit for still-closing executors,
   min(released, cached-held) for the stale wallet cache) so the rebuild is sized from the
   funds the cancels return -- never waiting on exchange balance updates.
2. post_refresh_settle_seconds (already wired): cancel wave first, quiet window, re-place.
3. Zero-level watchdog (empty_side_watchdog_seconds, default 60): an empty-but-fundable
   side is forced dirty; an armed per-side cooldown is respected at most once.
4. Deferred creates re-proposed: issued rebuilds whose creates never materialize are
   re-proposed the next cycle; a newer refresh supersedes older pending proposals.
5. Compression guard: a refresh whose candidate set is empty while live orders exist and
   the effective budget could fund a level ABORTS instead of cancelling into nothing.
"""
import asyncio
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

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


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
    """balances: {asset: [total, available]} -- mutable lists so tests can flip them."""
    mdp = MagicMock()
    mdp.time.return_value = now
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)
    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


def _resting(level_id, side, price, amount, eid, status=RunnableStatus.RUNNING, timestamp=0.0):
    ex = MagicMock()
    ex.id = eid
    ex.status = status
    ex.is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    ex.timestamp = timestamp
    ex.close_timestamp = None
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


def _filling(level_id, side, price, eid, *, filled_base, filled_quote, fees="0", close_ts=1000.0):
    ex = MagicMock()
    ex.id = eid
    ex.status = RunnableStatus.TERMINATED
    ex.is_active = False
    ex.timestamp = 0.0
    ex.close_timestamp = close_ts
    ex.connector_name = "nonkyc"
    ex.custom_info = {
        "filled_amount_base": D(filled_base),
        "filled_amount_quote": D(filled_quote),
        "cum_fees_quote": D(fees),
    }
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = D(price)
    cfg.amount = D("1")
    ex.config = cfg
    return ex


def _terminate(executors, close_ts):
    for ex in executors:
        ex.status = RunnableStatus.TERMINATED
        ex.is_active = False
        ex.close_timestamp = close_ts


def _shutting_down(executors):
    for ex in executors:
        ex.status = RunnableStatus.SHUTTING_DOWN
        ex.is_active = False


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        defaults = dict(
            id="ctrl-rbf",
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
            executor_refresh_time=10_000,
            buy_cooldown_time=120,
            sell_cooldown_time=120,
            ledger_overclaim_reanchor_seconds=999_999,
            fee_rate=Decimal("0"),  # budget math asserted exactly; fee headroom has its own suite
            # These lifecycle tests assert tight cancel->recreate timing; the post-cancel
            # balance gate has its own suite (test_range_inventory_ladder_preflight_retry).
            post_cancel_balance_timeout_seconds=0,
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

    def _update(self, ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

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


# ============================================= 1: the 7E-8 sell-side collapse (regression)

class TestRefreshBudgetAddBackSell(_Harness):
    """Side with 100% of its funds reserved in its own live ladder: the refresh must compute
    the FULL effective budget and re-place all levels through the whole cancel lifecycle
    (RUNNING -> SHUTTING_DOWN -> TERMINATED with a stale wallet cache)."""

    OWNED = "1.06036199"

    def _deadlock_setup(self):
        # All 1.06036199 XMR is inside the two resting sells; the cached wallet shows 7E-8
        # available. No quote at all (isolates the sell side).
        balances = {"XMR": [D(self.OWNED), D("7E-8")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base=self.OWNED, seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "0.6", "s0"),
                 _resting("sell_355", TradeType.SELL, "355", "0.46036199", "s1")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances, sells

    def test_full_lifecycle_rebuilds_all_levels_with_full_budget(self):
        ctrl, mdp, balances, sells = self._deadlock_setup()

        # Trigger a sell refresh (cooldown-lapse style). The rebuild plans 3 rungs vs 2
        # resting -> not converged -> cancel wave.
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        actions = self._full(ctrl, mdp, 2000.0)
        self.assertEqual({"s0", "s1"}, {a.executor_id for a in self._stops(actions)})
        self.assertEqual([], self._creates(actions))       # same-cycle creates deferred
        self.assertTrue(ctrl._sell_side_dirty)             # rebuild still owed
        wave = ctrl._refresh_wave["sell"]
        self.assertIsNotNone(wave)
        self.assertEqual(D(self.OWNED), wave["released"])

        # Cancel in flight: executors SHUTTING_DOWN, wallet cache unchanged. The effective
        # budget must already be the FULL amount (this is where 7E-8 killed the live ladder).
        _shutting_down(sells)
        actions = self._full(ctrl, mdp, 2001.0)
        self.assertEqual(D(self.OWNED), ctrl.processed_data["free_sell_budget_base"])
        self.assertEqual([], self._creates(actions))       # levels blocked while SHUTTING_DOWN
        self.assertTrue(ctrl._sell_side_dirty)             # NOT cleared by a transient empty plan

        # Cancels done, wallet cache STILL stale (the Kraken poll lag): the rebuild must
        # place the full ladder from the wave credit, not 7E-8.
        _terminate(sells, 2002.0)
        actions = self._full(ctrl, mdp, 2003.0)
        creates = self._creates(actions)
        self.assertEqual(3, len(creates))                  # all rungs kept, none compressed away
        total_base = sum((a.executor_config.amount for a in creates), D(0))
        self.assertGreaterEqual(total_base, D(self.OWNED) - D("0.000003"))  # quantization only
        self.assertLessEqual(total_base, D(self.OWNED))
        self.assertFalse(ctrl._sell_side_dirty)

    def test_compression_never_sees_the_stale_free_budget(self):
        ctrl, mdp, balances, sells = self._deadlock_setup()
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        self._full(ctrl, mdp, 2000.0)
        _terminate(sells, 2001.0)
        self._full(ctrl, mdp, 2002.0)
        # No sell-compression event may ever report a (near-)zero budget during the wave.
        for call in self._events(ctrl, "range_ladder_compression_changed"):
            if call.kwargs.get("side") == "sell":
                self.assertGreaterEqual(D(call.kwargs["total_budget"]), D(self.OWNED) - D("0.000003"))

    def test_budget_types_stay_decimal(self):
        ctrl, mdp, balances, sells = self._deadlock_setup()
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        self._full(ctrl, mdp, 2000.0)
        _shutting_down(sells)
        self._update(ctrl, mdp, 2001.0)
        self.assertIsInstance(ctrl.processed_data["free_sell_budget_base"], Decimal)
        self.assertIsInstance(ctrl.processed_data["free_buy_budget_quote"], Decimal)
        self.assertIsInstance(ctrl.processed_data["refresh_wave_release_sell_base"], Decimal)
        self.assertIsInstance(ctrl._refresh_wave["sell"]["released"], Decimal)
        self.assertIsInstance(ctrl._wave_ledger_credit_base, Decimal)


# ============================================= 3: the 13:44 buy-side variant

class TestRefreshBudgetAddBackBuy(_Harness):

    def test_buy_rebuild_uses_full_effective_budget_not_free_slice(self):
        # free 0.18 USDT, ~91.4 locked in the outgoing buys: the rebuild must re-place the
        # FULL ladder (3 levels here), not 1 level from the 0.18 slice.
        balances = {"XMR": [D(0), D(0)], "USDT": [D("91.58"), D("0.18")]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote="91.58", owned_base=0, seed_value=400)
        buys = [_resting("buy_321", TradeType.BUY, "321", "0.2", "b0"),      # 64.2 quote
                _resting("buy_318", TradeType.BUY, "318", "0.08553459", "b1")]  # ~27.2 quote
        ctrl.executors_info = list(buys)
        self._prime(ctrl, mdp, 1000.0)

        ctrl._buy_side_dirty = True
        ctrl._buy_dirty_reason = "buy_cooldown_lapsed"
        actions = self._full(ctrl, mdp, 2000.0)
        self.assertEqual({"b0", "b1"}, {a.executor_id for a in self._stops(actions)})

        _terminate(buys, 2001.0)
        actions = self._full(ctrl, mdp, 2002.0)             # wallet cache STILL stale
        creates = self._creates(actions)
        self.assertEqual(3, len(creates))                   # full ladder, not 1 level
        total_notional = sum((a.executor_config.amount * a.executor_config.price for a in creates), D(0))
        self.assertGreater(total_notional, D("88"))         # ~91.58 deployed (quantization slack)
        self.assertLessEqual(total_notional, D("91.58"))


# ============================================= 2: partial replace (legacy per-age path)

class TestPartialReplaceCreditsOnlyReplacedLevels(_Harness):

    def test_only_aged_out_orders_reservation_credited(self):
        balances = {"XMR": [D("1.0"), D(0)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, event_refresh_enabled=False, executor_refresh_time=100,
                           cooldown_time=30)
        self._init_state(ctrl, owned_quote=0, owned_base="1.0", seed_value=400)
        s_old = _resting("sell_350", TradeType.SELL, "350", "0.6", "s-old", timestamp=0.0)
        s_new = _resting("sell_355", TradeType.SELL, "355", "0.4", "s-new", timestamp=1000.0)
        ctrl.executors_info = [s_old, s_new]
        self._update(ctrl, mdp, 1010.0)

        actions = ctrl.determine_executor_actions()          # only s_old aged out (age 1010 >= 100)
        self.assertEqual({"s-old"}, {a.executor_id for a in self._stops(actions)})

        wave = ctrl._refresh_wave["sell"]
        self.assertIsNotNone(wave)
        self.assertEqual(D("0.6"), wave["released"])         # ONLY the replaced level
        self.assertEqual({"s-old"}, wave["cancelled_ids"])

        _shutting_down([s_old])
        self._update(ctrl, mdp, 1011.0)
        # Kept order s_new stays excluded: effective free = exactly the replaced 0.6.
        self.assertEqual(D("0.6"), ctrl.processed_data["free_sell_budget_base"])


# ============================================= 4 + 8: zero-level watchdog

class TestEmptySideWatchdog(_Harness):

    def _empty_funded(self, **overrides):
        balances = {"XMR": [D("1.0"), D("1.0")], "USDT": [D(200), D(200)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=200, owned_base="1.0", seed_value=535)
        ctrl.executors_info = []
        self._prime(ctrl, mdp, 1000.0)   # quiet: no dirty flags, no cooldowns, no orders
        return ctrl, mdp

    def test_watchdog_replaces_empty_fundable_ladder_within_one_interval(self):
        ctrl, mdp = self._empty_funded()
        self._update(ctrl, mdp, 1010.0)                     # timers arm (both sides empty)
        self.assertEqual([], self._events(ctrl, "range_ladder_empty_side_watchdog_fired"))

        actions = self._full(ctrl, mdp, 1075.0)             # 65s > 60s watchdog interval
        fired = self._events(ctrl, "range_ladder_empty_side_watchdog_fired")
        self.assertEqual({"buy", "sell"}, {c.kwargs["side"] for c in fired})
        for call in fired:
            self.assertGreaterEqual(call.kwargs["empty_for_s"], 60.0)
        creates = self._creates(actions)                    # placement resumes SAME tick
        sides = {a.executor_config.side for a in creates}
        self.assertIn(TradeType.BUY, sides)
        self.assertIn(TradeType.SELL, sides)

    def test_watchdog_fires_once_and_does_not_spam(self):
        ctrl, mdp = self._empty_funded()
        self._update(ctrl, mdp, 1010.0)
        actions = self._full(ctrl, mdp, 1075.0)
        creates = self._creates(actions)
        self.assertGreater(len(creates), 0)
        # The orders are now live: subsequent cycles must not re-fire or re-create.
        ctrl.executors_info = [
            _resting(a.executor_config.level_id, a.executor_config.side,
                     a.executor_config.price, a.executor_config.amount, f"w{i}")
            for i, a in enumerate(creates)]
        for t in (1076.0, 1140.0, 1200.0):
            follow_up = self._full(ctrl, mdp, t)
            self.assertEqual([], self._creates(follow_up))
        self.assertEqual(2, len(self._events(ctrl, "range_ladder_empty_side_watchdog_fired")))  # buy+sell once each

    def test_watchdog_respects_armed_cooldown_then_normal_lapse_replaces(self):
        # Only the SELL side is fundable (no quote) so the buy watchdog stays silent.
        balances = {"XMR": [D("1.0"), D("1.0")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base="1.0", seed_value=400)
        ctrl.executors_info = []
        self._prime(ctrl, mdp, 1000.0)
        ctrl._sell_cooldown_armed = True                    # a recent sell fill armed it
        ctrl._last_sell_fill_ts = 1000.0

        self._update(ctrl, mdp, 1010.0)                     # timer arms
        self._full(ctrl, mdp, 1080.0)                       # 70s empty BUT cooldown armed
        self.assertEqual([], self._events(ctrl, "range_ladder_empty_side_watchdog_fired"))

        # The cooldown lapse itself re-centers the side (the watchdog defers to it).
        actions = self._full(ctrl, mdp, 1121.0)             # 120s sell cooldown lapsed
        self.assertFalse(ctrl._sell_cooldown_armed)
        creates = self._creates(actions)
        self.assertGreater(len(creates), 0)
        self.assertEqual([], self._events(ctrl, "range_ladder_empty_side_watchdog_fired"))

    def test_watchdog_ignores_unfundable_side(self):
        balances = {"XMR": [D(0), D(0)], "USDT": [D(0), D(0)]}   # nothing to deploy
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base=0, seed_value=400)
        ctrl.executors_info = []
        self._prime(ctrl, mdp, 1000.0)
        self._update(ctrl, mdp, 1010.0)
        self._full(ctrl, mdp, 1200.0)
        self.assertEqual([], self._events(ctrl, "range_ladder_empty_side_watchdog_fired"))

    def test_watchdog_config_validation(self):
        with self.assertRaises(Exception):
            self._build(_make_mdp(balances={"XMR": [D(0), D(0)], "USDT": [D(0), D(0)]},
                                  mid=335, bid=334.9, ask=335.1),
                        empty_side_watchdog_seconds=0)


# ============================================= 5: compression guard

class TestCompressionGuard(_Harness):

    def test_empty_candidate_set_with_live_orders_aborts_refresh(self):
        # Price sits ABOVE the whole sell range (350-360 vs ask 400): every rung fails the
        # passivity filter, so the candidate set is empty -- but the live orders hold real
        # inventory (budget could fund levels). The refresh must abort, cancel NOTHING.
        balances = {"XMR": [D("1.06"), D(0)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=399, bid=398, ask=400)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base="1.06", seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "0.6", "s0"),
                 _resting("sell_355", TradeType.SELL, "355", "0.46", "s1")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)

        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        actions = self._full(ctrl, mdp, 2000.0)

        self.assertEqual([], self._stops(actions))          # no cancels issued
        self.assertFalse(ctrl._sell_side_dirty)             # refresh aborted, not retried per-tick
        aborted = self._events(ctrl, "range_ladder_side_refresh_aborted_empty_plan")
        self.assertEqual(1, len(aborted))
        self.assertEqual("sell", aborted[0].kwargs["side"])
        self.assertEqual(2, aborted[0].kwargs["resting_levels"])
        self.assertGreater(D(aborted[0].kwargs["budget_notional"]), D("400"))  # ~1.06 * 399
        self.assertEqual([], self._events(ctrl, "range_ladder_side_refresh"))  # no cancel wave

    def test_dust_ladder_still_winds_down(self):
        # Budget genuinely below one level (dust): the guard must NOT block the cancel.
        balances = {"XMR": [D("0.001"), D(0)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=399, bid=398, ask=400)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base="0.001", seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "0.001", "s0")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        actions = self._full(ctrl, mdp, 2000.0)
        # 0.001 * 399 = 0.399 < min_order_quote 5 -> guard does not engage; wind-down allowed.
        self.assertEqual([], self._events(ctrl, "range_ladder_side_refresh_aborted_empty_plan"))
        self.assertEqual({"s0"}, {a.executor_id for a in self._stops(actions)})


# ============================================= 6: post_refresh_settle_seconds > 0

class TestPostRefreshSettleWithWaveCredit(_Harness):

    def test_cancel_wave_quiet_window_then_full_ladder(self):
        balances = {"XMR": [D("1.06036199"), D("7E-8")], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, post_refresh_settle_seconds=30)
        self._init_state(ctrl, owned_quote=0, owned_base="1.06036199", seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "0.6", "s0"),
                 _resting("sell_355", TradeType.SELL, "355", "0.46036199", "s1")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)

        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        actions = self._full(ctrl, mdp, 2000.0)             # cancel wave FIRST
        self.assertEqual(2, len(self._stops(actions)))
        self.assertEqual(2030.0, ctrl._refresh_quiet_until)

        _terminate(sells, 2001.0)
        actions = self._full(ctrl, mdp, 2010.0)             # inside the settle window
        self.assertEqual([], self._creates(actions))
        self.assertGreater(
            len(self._events(ctrl, "range_ladder_create_blocked_post_refresh_settle")), 0)

        # Window over, wallet cache STILL stale -> the wave credit sizes the full ladder.
        actions = self._full(ctrl, mdp, 2031.0)
        creates = self._creates(actions)
        self.assertEqual(3, len(creates))
        total_base = sum((a.executor_config.amount for a in creates), D(0))
        self.assertGreaterEqual(total_base, D("1.06036199") - D("0.000003"))


# ============================================= 9: NonKYC LIMIT fallback on refresh

class TestLimitFallbackOnRefresh(_Harness):

    def test_refresh_places_limit_orders_when_limit_maker_unsupported(self):
        balances = {"XMR": [D("1.0"), D(0)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        mdp.get_connector.return_value.supported_order_types.return_value = [OrderType.LIMIT]
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base="1.0", seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "1.0", "s0")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)

        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "sell_cooldown_lapsed"
        self._full(ctrl, mdp, 2000.0)
        _terminate(sells, 2001.0)
        actions = self._full(ctrl, mdp, 2002.0)
        creates = self._creates(actions)
        self.assertGreater(len(creates), 0)
        for action in creates:
            self.assertEqual(ExecutionStrategy.LIMIT, action.executor_config.execution_strategy)
            # passivity: sells rest above the ask
            self.assertGreater(action.executor_config.price, D("335.1"))


# ============================================= 10 + 11: deferred creates re-propose

class TestDeferredCreatesRepropose(_Harness):

    def _lost_creates_setup(self):
        """Fill-triggered buy refresh whose creates the orchestrator silently drops."""
        balances = {"XMR": [D(0), D(0)], "USDT": [D("91.58"), D("0.18")]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote="91.58", owned_base=0, seed_value=400)
        buy = _resting("buy_321", TradeType.BUY, "321", "0.28", "b0")
        ctrl.executors_info = [buy]
        self._prime(ctrl, mdp, 1000.0)

        # Fill-triggered opposite-side refresh (the 14:53 case).
        ctrl._buy_side_dirty = True
        ctrl._buy_dirty_reason = "sell_fill"
        actions = self._full(ctrl, mdp, 2000.0)
        self.assertEqual({"b0"}, {a.executor_id for a in self._stops(actions)})

        _terminate([buy], 2001.0)
        actions = self._full(ctrl, mdp, 2002.0)             # rebuild issued
        issued = self._creates(actions)
        self.assertGreater(len(issued), 0)
        self.assertFalse(ctrl._buy_side_dirty)
        # ... and the orchestrator drops them: executors_info never receives them.
        return ctrl, mdp, issued

    def test_dropped_creates_are_reproposed_next_cycle(self):
        ctrl, mdp, issued = self._lost_creates_setup()

        actions = self._full(ctrl, mdp, 2003.0)             # next cycle
        reproposed = self._events(ctrl, "range_ladder_deferred_creates_reproposed")
        self.assertEqual(1, len(reproposed))
        self.assertEqual("buy", reproposed[0].kwargs["side"])
        self.assertEqual(len(issued), reproposed[0].kwargs["deferred_count"])
        self.assertEqual(1, reproposed[0].kwargs["attempt"])
        self.assertGreater(reproposed[0].kwargs["deferral_age_s"], 0)

        creates = self._creates(actions)                    # re-issued THIS cycle
        self.assertEqual({a.executor_config.level_id for a in issued},
                         {a.executor_config.level_id for a in creates})

    def test_repropose_stops_once_orders_materialize(self):
        ctrl, mdp, issued = self._lost_creates_setup()
        actions = self._full(ctrl, mdp, 2003.0)             # re-propose #1
        creates = self._creates(actions)
        ctrl.executors_info = [
            _resting(a.executor_config.level_id, TradeType.BUY,
                     a.executor_config.price, a.executor_config.amount, f"n{i}")
            for i, a in enumerate(creates)]

        follow_up = self._full(ctrl, mdp, 2004.0)           # orders are live now
        self.assertEqual([], self._creates(follow_up))
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_deferred_creates_reproposed")))
        self.assertIsNone(ctrl._refresh_wave["buy"])        # wave resolved

    def test_newer_refresh_supersedes_pending_proposals(self):
        ctrl, mdp, issued = self._lost_creates_setup()

        # A NEWER trigger dirties the buy side before the re-propose runs.
        mdp.time.return_value = 2003.0
        asyncio.run(ctrl.update_processed_data())
        ctrl._mark_side_dirty("buy", "sell_fill")
        actions = ctrl.determine_executor_actions()

        # No re-propose event: the newer refresh owns the side; exactly ONE ladder placed.
        self.assertEqual([], self._events(ctrl, "range_ladder_deferred_creates_reproposed"))
        creates = self._creates(actions)
        self.assertGreater(len(creates), 0)
        level_ids = [a.executor_config.level_id for a in creates]
        self.assertEqual(len(level_ids), len(set(level_ids)))  # no duplicate levels


# ============================================= 12: trigger semantics preserved

class TestTriggerSemanticsUnchanged(_Harness):

    def test_sell_fill_refreshes_buy_immediately_sell_only_after_cooldown(self):
        balances = {"XMR": [D("0.4"), D("0.4")], "USDT": [D(40), D(40)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=40, owned_base="0.5", seed_value=400)
        self._prime(ctrl, mdp, 1000.0)

        ctrl.executors_info = [_filling("sell_350", TradeType.SELL, "350", "sf",
                                        filled_base="0.1", filled_quote="35")]
        balances["USDT"] = [D(75), D(75)]
        balances["XMR"] = [D("0.3"), D("0.3")]
        self._update(ctrl, mdp, 1010.0)

        self.assertTrue(ctrl._buy_side_dirty)               # immediate cross-side refresh
        self.assertEqual("sell_fill", ctrl._buy_dirty_reason)
        self.assertFalse(ctrl._sell_side_dirty)             # own side waits for its cooldown
        self.assertTrue(ctrl._sell_cooldown_armed)

        self._update(ctrl, mdp, 1010.0 + 119.0)             # cooldown not yet lapsed
        self.assertFalse(ctrl._sell_side_dirty)
        self._update(ctrl, mdp, 1010.0 + 121.0)             # lapsed -> re-center once
        self.assertTrue(ctrl._sell_side_dirty)
        self.assertEqual("sell_cooldown_lapsed", ctrl._sell_dirty_reason)
        self.assertFalse(ctrl._sell_cooldown_armed)

    def test_buy_fill_mirror_symmetric(self):
        balances = {"XMR": [D("1.0"), D("1.0")], "USDT": [D(200), D(200)]}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=200, owned_base="0.2", seed_value=400)
        self._prime(ctrl, mdp, 1000.0)

        ctrl.executors_info = [_filling("buy_321", TradeType.BUY, "321", "bf",
                                        filled_base="0.3", filled_quote="96.3")]
        balances["XMR"] = [D("1.3"), D("1.3")]
        self._update(ctrl, mdp, 1010.0)

        self.assertTrue(ctrl._sell_side_dirty)
        self.assertEqual("buy_fill", ctrl._sell_dirty_reason)
        self.assertFalse(ctrl._buy_side_dirty)
        self.assertTrue(ctrl._buy_cooldown_armed)

        self._update(ctrl, mdp, 1010.0 + 121.0)
        self.assertTrue(ctrl._buy_side_dirty)
        self.assertEqual("buy_cooldown_lapsed", ctrl._buy_dirty_reason)


if __name__ == "__main__":
    unittest.main()
