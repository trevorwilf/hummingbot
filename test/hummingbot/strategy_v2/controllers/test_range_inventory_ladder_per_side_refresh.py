"""Behavior-contract tests for the per-side cooldown + immediate cross-side refresh model
(event_refresh_enabled=True) in range_inventory_ladder.

Five triggers (see the controller docstring):
  - Buy fill  -> immediately refresh the SELL ladder + reset the buy cooldown.
  - Sell fill -> immediately refresh the BUY ladder + reset the sell cooldown.
  - buy_cooldown_time lapses (no buy fill)  -> re-center the BUY ladder once.
  - sell_cooldown_time lapses (no sell fill) -> re-center the SELL ladder once.
  - executor_refresh_time (global) -> refresh BOTH ladders.

NOTE on flag timing: update_processed_data() evaluates the triggers and SETS the *_side_dirty
intents; determine_executor_actions() then APPLIES them (cancel) and clears a side's flag once
its rebuild has been issued. So trigger/cooldown assertions are made right after
update_processed_data(), and cancel assertions after determine_executor_actions().

Every test drives the REAL controller; balances / prices / time / quantization / executors_info
are mocked so the suite is deterministic. Assertions target behavior that fails if the per-side
model is reverted (no tautological assertIsNotNone-style checks).
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
    """balances: {asset: (total, available)}. Mutate between cycles to simulate fills/deposits."""
    mdp = MagicMock()
    mdp.time.return_value = now

    def price_by_type(conn, pair, pt):
        return {PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]

    mdp.get_price_by_type.side_effect = price_by_type
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)

    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


def _set_prices(mdp, mid, bid, ask):
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]


def _make_config(**overrides):
    defaults = dict(
        id="ctrl-psr",
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
        executor_refresh_time=10_000,         # large -> global timer does not fire mid-test
        buy_cooldown_time=120,
        sell_cooldown_time=120,
        ledger_overclaim_reanchor_seconds=999_999,  # the v12 re-anchor is out of scope here
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


def _resting(level_id, side, price, amount, eid, status=RunnableStatus.RUNNING):
    ex = MagicMock()
    ex.id = eid
    ex.status = status
    ex.is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    ex.timestamp = 0.0
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


def _filling(level_id, side, price, eid, *, filled_base, filled_quote, fees="0",
             status=RunnableStatus.TERMINATED, close_ts=1000.0):
    """An executor reporting a CUMULATIVE fill via custom_info (a v13 booking source). Default
    TERMINATED + closed so it no longer reserves capital (a fully-filled order)."""
    ex = MagicMock()
    ex.id = eid
    ex.status = status
    ex.is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
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


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        config = _make_config(**config_overrides)
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

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value,
                    reserve_quote="0", reserve_base="0"):
        ctrl._state = {
            "initialized": True,
            "owned_quote": str(owned_quote),
            "owned_base": str(owned_base),
            "seed_value_quote": str(seed_value),
            "initial_managed_quote": str(owned_quote),
            "initial_claimed_base_amount": str(owned_base),
            "initial_reference_price": "335",
            "reserve_quote_balance": str(reserve_quote),
            "reserve_base_balance": str(reserve_base),
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True

    def _prime(self, ctrl, mdp, now):
        """Run one cycle to initialize the refresh timers, then reset to a quiet steady state
        (no side dirty, cooldowns unarmed, global timer just fired) so each test exercises a
        single trigger cleanly without the initial-placement refresh interfering."""
        mdp.time.return_value = now
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = False
        ctrl._sell_side_dirty = False
        ctrl._buy_dirty_reason = ""
        ctrl._sell_dirty_reason = ""
        ctrl._buy_cooldown_armed = False
        ctrl._sell_cooldown_armed = False
        ctrl._last_global_refresh_ts = now

    def _seed_deployed_buys(self, ctrl, mdp, now, balances, available_quote):
        """Fresh-build the buy ladder from `available_quote`, return RUNNING resting executors
        matching the created book, then flip the wallet to fully-deployed (available 0, total =
        the deployed value) so a fresh rebuild reproduces the resting book (steady state)."""
        balances["USDT"] = (D(available_quote), D(available_quote))
        mdp.time.return_value = now
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = True
        ctrl._defer_buy_creates_this_cycle = False
        creates = ctrl._create_buy_actions()
        resting = [_resting(a.executor_config.level_id, TradeType.BUY, a.executor_config.price,
                            a.executor_config.amount, f"rb{i}") for i, a in enumerate(creates)]
        deployed = sum((a.executor_config.amount * a.executor_config.price for a in creates), D(0))
        balances["USDT"] = (deployed, D(0))   # the quote is now reserved on the book
        return resting, deployed

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


# =================================================== 1 & 2: immediate cross-side refresh

class TestCrossSideRefresh(_Harness):

    def test_buy_fill_refreshes_sell_only_and_resets_buy_cooldown(self):
        balances = {"XMR": (D("1.0"), D("1.0")), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=200, owned_base="0.2", seed_value=400)
        sells = [_resting("sell_350", TradeType.SELL, "350", "0.01", "s0"),
                 _resting("sell_355", TradeType.SELL, "355", "0.01", "s1"),
                 _resting("sell_360", TradeType.SELL, "360", "0.01", "s2")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)

        # A BUY rung fills (quote -> base): owned_base up, wallet base up (more to sell).
        ctrl.executors_info = list(sells) + [
            _filling("buy_321", TradeType.BUY, "321", "b0",
                     filled_base="0.3", filled_quote="96.3", fees="0.2")]
        balances["XMR"] = (D("1.3"), D("1.3"))

        self._update(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_buy_fill_this_cycle)
        # A buy fill dirties the SELL side, never the buy side, and arms ITS OWN cooldown.
        self.assertTrue(ctrl._sell_side_dirty)
        self.assertFalse(ctrl._buy_side_dirty)
        self.assertEqual(ctrl._sell_dirty_reason, "buy_fill")
        self.assertTrue(ctrl._buy_cooldown_armed)
        self.assertEqual(ctrl._last_buy_fill_ts, 1010.0)

        actions = ctrl.determine_executor_actions()
        stopped_ids = {a.executor_id for a in self._stops(actions)}
        self.assertEqual(stopped_ids, {"s0", "s1", "s2"})  # SELL ladder refreshed; no buy stopped

    def test_sell_fill_refreshes_buy_only_and_resets_sell_cooldown(self):
        balances = {"XMR": (D("0.4"), D("0.4")), "USDT": (D(40), D(40))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=40, owned_base="0.5", seed_value=400)
        buys = [_resting("buy_321", TradeType.BUY, "321", "0.01", "b0"),
                _resting("buy_318", TradeType.BUY, "318", "0.01", "b1"),
                _resting("buy_315", TradeType.BUY, "315", "0.01", "b2")]
        ctrl.executors_info = list(buys)
        self._prime(ctrl, mdp, 1000.0)

        ctrl.executors_info = list(buys) + [
            _filling("sell_350", TradeType.SELL, "350", "s0",
                     filled_base="0.3", filled_quote="105", fees="0.2")]
        balances["USDT"] = (D(145), D(145))

        self._update(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_sell_fill_this_cycle)
        self.assertTrue(ctrl._buy_side_dirty)
        self.assertFalse(ctrl._sell_side_dirty)
        self.assertEqual(ctrl._buy_dirty_reason, "sell_fill")
        self.assertTrue(ctrl._sell_cooldown_armed)
        self.assertEqual(ctrl._last_sell_fill_ts, 1010.0)

        actions = ctrl.determine_executor_actions()
        stopped_ids = {a.executor_id for a in self._stops(actions)}
        self.assertEqual(stopped_ids, {"b0", "b1", "b2"})

    def test_cross_side_rebuild_deploys_more_next_cycle(self):
        """After a sell fill cancels the buys, the next cycle recreates a LARGER buy ladder off
        the new quote (cancel cycle N -> recreate cycle N+1)."""
        balances = {"XMR": (D("0.4"), D("0.4")), "USDT": (D(40), D(40))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=40, owned_base="0.5", seed_value=400)
        buys = [_resting("buy_321", TradeType.BUY, "321", "0.02", "b0")]
        ctrl.executors_info = list(buys)
        self._prime(ctrl, mdp, 1000.0)

        seller = _filling("sell_350", TradeType.SELL, "350", "s0",
                          filled_base="0.3", filled_quote="105", fees="0.2")
        ctrl.executors_info = list(buys) + [seller]
        balances["USDT"] = (D(145), D(145))
        actions_n = self._full(ctrl, mdp, 1010.0)
        self.assertEqual({a.executor_id for a in self._stops(actions_n)}, {"b0"})
        self.assertTrue(ctrl._defer_buy_creates_this_cycle)  # creates deferred this cycle

        # Cycle N+1: the cancelled buy is gone; the buy side rebuilds off the larger quote.
        ctrl.executors_info = [seller]
        actions_n1 = self._full(ctrl, mdp, 1011.0)
        buy_creates = [a for a in self._creates(actions_n1) if a.executor_config.side == TradeType.BUY]
        self.assertTrue(buy_creates, "buy ladder should rebuild next cycle")
        deployed = sum((a.executor_config.amount * a.executor_config.price for a in buy_creates), D(0))
        self.assertGreater(deployed, D(40))      # more than the original 40 USDT wallet pre-fill
        self.assertFalse(ctrl._buy_side_dirty)   # refresh complete -> flag cleared


# =================================================== 3 & 6: no same-side dirty / independent timers

class TestTriggerIsolation(_Harness):

    def test_buy_fill_never_dirties_buy_side(self):
        balances = {"XMR": (D("1.0"), D("1.0")), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=200, owned_base="0.2", seed_value=400)
        self._prime(ctrl, mdp, 1000.0)
        ctrl.executors_info = [_filling("buy_321", TradeType.BUY, "321", "b0",
                                        filled_base="0.3", filled_quote="96.3", fees="0.2")]
        balances["XMR"] = (D("1.3"), D("1.3"))
        self._update(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_buy_fill_this_cycle)
        self.assertFalse(ctrl._buy_side_dirty)
        self.assertTrue(ctrl._sell_side_dirty)

    def test_sell_fill_never_dirties_sell_side(self):
        balances = {"XMR": (D("0.4"), D("0.4")), "USDT": (D(40), D(40))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=40, owned_base="0.5", seed_value=400)
        self._prime(ctrl, mdp, 1000.0)
        ctrl.executors_info = [_filling("sell_350", TradeType.SELL, "350", "s0",
                                        filled_base="0.3", filled_quote="105", fees="0.2")]
        balances["USDT"] = (D(145), D(145))
        self._update(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_sell_fill_this_cycle)
        self.assertFalse(ctrl._sell_side_dirty)
        self.assertTrue(ctrl._buy_side_dirty)

    def test_sell_fill_does_not_reset_buy_cooldown_and_vice_versa(self):
        balances = {"XMR": (D("0.4"), D("0.4")), "USDT": (D(40), D(40))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=40, owned_base="0.5", seed_value=400)
        self._prime(ctrl, mdp, 1000.0)
        # Pretend a buy fill happened at t=900 (buy cooldown armed).
        ctrl._buy_cooldown_armed = True
        ctrl._last_buy_fill_ts = 900.0
        # A SELL fills at t=1010: it must NOT touch the buy cooldown timestamp.
        ctrl.executors_info = [_filling("sell_350", TradeType.SELL, "350", "s0",
                                        filled_base="0.3", filled_quote="105", fees="0.2")]
        balances["USDT"] = (D(145), D(145))
        self._update(ctrl, mdp, 1010.0)
        self.assertEqual(ctrl._last_buy_fill_ts, 900.0)    # untouched by the sell fill
        self.assertTrue(ctrl._buy_cooldown_armed)
        self.assertEqual(ctrl._last_sell_fill_ts, 1010.0)  # sell fill set its own
        self.assertTrue(ctrl._sell_cooldown_armed)


# =================================================== 4 & 5: cooldown lapse re-center (once)

class TestCooldownLapse(_Harness):

    def test_buy_cooldown_lapse_recenters_buy_once(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D("64.2"), D(0))}  # ~64 reserved by the buy
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, buy_cooldown_time=120)
        self._init_state(ctrl, owned_quote="64.2", owned_base=0, seed_value=400)
        buys = [_resting("buy_321", TradeType.BUY, "321", "0.2", "b0")]
        ctrl.executors_info = list(buys)
        self._prime(ctrl, mdp, 1000.0)
        ctrl._buy_cooldown_armed = True
        ctrl._last_buy_fill_ts = 1000.0

        # Within cooldown (t=1100 < 120s) -> no re-center.
        self._update(ctrl, mdp, 1100.0)
        self.assertFalse(ctrl._buy_side_dirty)

        # Market moves so a rebuild lands on a different rung (forces a real re-center).
        _set_prices(mdp, 316.6, 316.5, 316.7)

        # Past cooldown (t=1130 > 120s) -> buy re-centers exactly once.
        self._update(ctrl, mdp, 1130.0)
        self.assertTrue(ctrl._buy_side_dirty)
        self.assertEqual(ctrl._buy_dirty_reason, "buy_cooldown_lapsed")
        self.assertFalse(ctrl._buy_cooldown_armed)  # disarmed -> fires once
        actions = ctrl.determine_executor_actions()
        self.assertEqual({a.executor_id for a in self._stops(actions)}, {"b0"})

        # Complete the rebuild (the cancel freed ~64 to the wallet), then confirm the cooldown
        # does NOT re-fire: it disarmed on lapse and only a NEW buy fill can re-arm it.
        ctrl.executors_info = []
        balances["USDT"] = (D("64.2"), D("64.2"))
        self._full(ctrl, mdp, 1131.0)         # rebuild lands -> buy dirty clears
        self.assertFalse(ctrl._buy_cooldown_armed)
        self._update(ctrl, mdp, 1300.0)       # well past another cooldown window
        self.assertFalse(ctrl._buy_side_dirty)
        self.assertFalse(ctrl._buy_cooldown_armed)

    def test_sell_cooldown_lapse_recenters_sell_once(self):
        balances = {"XMR": (D("0.2"), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, sell_cooldown_time=90)
        self._init_state(ctrl, owned_quote=0, owned_base="0.2", seed_value=400)
        sells = [_resting("sell_360", TradeType.SELL, "360", "0.2", "s0")]
        ctrl.executors_info = list(sells)
        self._prime(ctrl, mdp, 1000.0)
        ctrl._sell_cooldown_armed = True
        ctrl._last_sell_fill_ts = 1000.0
        _set_prices(mdp, 349.6, 349.5, 349.6)  # nearest sell rung changes -> real re-center

        self._update(ctrl, mdp, 1100.0)  # >90s
        self.assertTrue(ctrl._sell_side_dirty)
        self.assertEqual(ctrl._sell_dirty_reason, "sell_cooldown_lapsed")
        self.assertFalse(ctrl._sell_cooldown_armed)
        actions = ctrl.determine_executor_actions()
        self.assertEqual({a.executor_id for a in self._stops(actions)}, {"s0"})


# =================================================== 7: 305 -> 312 re-center

class TestRecenter305to312(_Harness):

    def test_lone_far_rung_relocates_to_nearest_under_price(self):
        # Buys ran down to 305; price recovered to ~315. The lone funded 305 rung must relocate to
        # the nearest rung under price (315), not stay at 305.
        balances = {"XMR": (D(0), D(0)), "USDT": (D("6.1"), D(0))}  # the 305 order holds ~6.1
        mdp = _make_mdp(balances=balances, mid=315.6, bid=315.5, ask=315.7)
        ctrl = self._build(
            mdp,
            buy_prices=[Decimal("321"), Decimal("318"), Decimal("315"), Decimal("312"), Decimal("305")],
            buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("350"), Decimal("355")],
            sell_amounts_pct=[Decimal("1"), Decimal("1")],
            min_order_quote=Decimal("5"),
            buy_cooldown_time=120,
        )
        self._init_state(ctrl, owned_quote="6.1", owned_base=0, seed_value=400)
        lone = _resting("buy_305", TradeType.BUY, "305", "0.02", "b305")  # ~6.1 USDT reserved
        ctrl.executors_info = [lone]
        self._prime(ctrl, mdp, 1000.0)
        ctrl._buy_cooldown_armed = True
        ctrl._last_buy_fill_ts = 1000.0

        # Cooldown lapses -> buy re-centers: the lone 305 rung is cancelled (rung set changed).
        actions = self._full(ctrl, mdp, 1130.0)
        self.assertEqual({a.executor_id for a in self._stops(actions)}, {"b305"})

        # Next cycle: the 305 cancel returned ~6.1 to the wallet; the rebuild lands on the NEAREST
        # eligible rung under price (315), not 305.
        ctrl.executors_info = []
        balances["USDT"] = (D("6.1"), D("6.1"))   # freed by the cancel
        actions2 = self._full(ctrl, mdp, 1131.0)
        buy_creates = [a for a in self._creates(actions2) if a.executor_config.side == TradeType.BUY]
        self.assertTrue(buy_creates)
        prices = {a.executor_config.price for a in buy_creates}
        self.assertEqual(prices, {Decimal("315")})   # nearest under 315.5
        self.assertNotIn(Decimal("305"), prices)


# =================================================== 8: global timer

class TestGlobalTimer(_Harness):

    def test_global_timer_refreshes_both_sides_independent_of_cooldowns(self):
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(150), D(150))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, executor_refresh_time=600, buy_cooldown_time=99999, sell_cooldown_time=99999)
        self._init_state(ctrl, owned_quote=150, owned_base="0.5", seed_value=400)
        book = [_resting("buy_321", TradeType.BUY, "321", "0.01", "b0"),
                _resting("sell_350", TradeType.SELL, "350", "0.01", "s0")]
        ctrl.executors_info = list(book)
        self._prime(ctrl, mdp, 1000.0)   # sets _last_global_refresh_ts = 1000

        # Before the interval -> nothing dirty.
        self._update(ctrl, mdp, 1300.0)
        self.assertFalse(ctrl._buy_side_dirty or ctrl._sell_side_dirty)

        # Past the global interval (>600s) -> BOTH sides refresh, with cooldowns never armed.
        self._update(ctrl, mdp, 1700.0)
        self.assertTrue(ctrl._buy_side_dirty and ctrl._sell_side_dirty)
        self.assertEqual(ctrl._buy_dirty_reason, "global_timer")
        self.assertEqual(ctrl._sell_dirty_reason, "global_timer")
        self.assertFalse(ctrl._buy_cooldown_armed or ctrl._sell_cooldown_armed)
        actions = ctrl.determine_executor_actions()
        self.assertEqual({a.executor_id for a in self._stops(actions)}, {"b0", "s0"})


# =================================================== 9 & 10: no-op and dust guards

class TestGuards(_Harness):

    def test_noop_guard_skips_refresh_when_book_unchanged(self):
        # Steady-state, fully-deployed buy book. A global refresh with NO change is a no-op.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, executor_refresh_time=600)
        self._init_state(ctrl, owned_quote=200, owned_base=0, seed_value=400)
        resting, deployed = self._seed_deployed_buys(ctrl, mdp, 1000.0, balances, available_quote=200)
        ctrl.executors_info = list(resting)
        self._prime(ctrl, mdp, 1000.0)
        self._update(ctrl, mdp, 1000.0)
        self.assertTrue(ctrl._plan_buy_book())                         # there IS a book to reproduce
        self.assertTrue(ctrl._side_refresh_converged(TradeType.BUY))   # rebuild == resting

        # Global timer fires (both dirty) but the buy refresh is a no-op -> no cancels.
        actions = self._full(ctrl, mdp, 1700.0)
        self.assertEqual(self._stops(actions), [])
        skipped = self._events(ctrl, "range_ladder_side_refresh_skipped")
        self.assertTrue(any(c.kwargs.get("side") == "buy" for c in skipped))
        self.assertFalse(ctrl._buy_side_dirty)   # cleared without churn

    def test_dust_guard_tiny_fill_does_not_churn_opposite_ladder(self):
        # A sub-min_order_quote sell fill credits dust quote; the buy ladder must NOT churn.
        balances = {"XMR": (D("0.02"), D("0.02")), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, min_order_quote=Decimal("5"))
        self._init_state(ctrl, owned_quote=200, owned_base="0.02", seed_value=400)
        resting, deployed = self._seed_deployed_buys(ctrl, mdp, 1000.0, balances, available_quote=200)
        ctrl.executors_info = list(resting)
        self._prime(ctrl, mdp, 1000.0)

        # Tiny sell fill: 0.005 base -> ~1.7 quote (< min_order_quote 5). Dust into the wallet.
        ctrl.executors_info = list(resting) + [
            _filling("sell_350", TradeType.SELL, "350", "s0",
                     filled_base="0.005", filled_quote="1.7", fees="0")]
        balances["USDT"] = (deployed + D("1.7"), D("1.7"))   # dust available only
        self._update(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_sell_fill_this_cycle)
        self.assertTrue(ctrl._buy_side_dirty)   # the trigger fired...

        # ...but the dust can neither add a rung nor shift the total by a whole order -> no cancel.
        actions = ctrl.determine_executor_actions()
        self.assertEqual(self._stops(actions), [])


# =================================================== 11: no self-trigger loop

class TestNoSelfTriggerLoop(_Harness):

    def test_placement_does_not_arm_cooldowns_or_redirty(self):
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(150), D(150))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=150, owned_base="0.5", seed_value=400)
        # Initial placement cycle: both sides build.
        actions = self._full(ctrl, mdp, 1000.0)
        self.assertTrue(self._creates(actions))
        # A placement is not a fill: cooldown timers stay unarmed and sides clear.
        self.assertFalse(ctrl._buy_cooldown_armed or ctrl._sell_cooldown_armed)
        self.assertFalse(ctrl._buy_side_dirty or ctrl._sell_side_dirty)
        self.assertEqual(ctrl._last_buy_fill_ts, 0.0)
        self.assertEqual(ctrl._last_sell_fill_ts, 0.0)
        # A second idle cycle (no fills) does not re-dirty or stop anything.
        actions2 = self._full(ctrl, mdp, 1001.0)
        self.assertEqual(self._stops(actions2), [])
        self.assertFalse(ctrl._buy_side_dirty or ctrl._sell_side_dirty)


# =================================================== 12: planner / weights / compression

class TestWeightsWeldedAndPlanner(_Harness):

    def test_planner_matches_create_path_on_fresh_build(self):
        # The guards rely on the planner mirroring the live create path. On a fresh build (no
        # resting orders) they must produce the identical {level_id: amount} book.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(120), D(120))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp,
                           buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
                           buy_amounts_pct=[Decimal("3"), Decimal("2"), Decimal("1")])
        self._init_state(ctrl, owned_quote=120, owned_base=0, seed_value=400)
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = True
        ctrl._defer_buy_creates_this_cycle = False
        planned = ctrl._plan_buy_book()
        created = {a.executor_config.level_id: a.executor_config.amount
                   for a in ctrl._create_buy_actions()}
        self.assertEqual(planned, created)
        self.assertTrue(planned)

    def test_weights_welded_to_their_price_rungs(self):
        # With weights 3:2:1 the 321 rung gets the largest notional, 315 the smallest -- weights
        # do not float onto whichever rung is nearest.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(120), D(120))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp,
                           buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
                           buy_amounts_pct=[Decimal("3"), Decimal("2"), Decimal("1")])
        self._init_state(ctrl, owned_quote=120, owned_base=0, seed_value=400)
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = True
        by_level = {a.executor_config.level_id: a.executor_config.amount * a.executor_config.price
                    for a in ctrl._create_buy_actions()}
        self.assertGreater(by_level["buy_321"], by_level["buy_318"])
        self.assertGreater(by_level["buy_318"], by_level["buy_315"])

    def test_compression_keeps_nearest_when_budget_small(self):
        # Tiny budget funds only one rung -> the NEAREST-to-price rung is kept (321), not a far one.
        balances = {"XMR": (D(0), D(0)), "USDT": (D("6"), D("6"))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp,
                           buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
                           buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
                           min_order_quote=Decimal("5"))
        self._init_state(ctrl, owned_quote=6, owned_base=0, seed_value=400)
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = True
        creates = ctrl._create_buy_actions()
        self.assertEqual([a.executor_config.price for a in creates], [Decimal("321")])


# =================================================== 13 & 14: managed fund (Point 4)

class TestManagedFund(_Harness):

    def test_deposit_does_not_raise_fund_or_ceiling(self):
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=100, owned_base="0.5", seed_value=300)
        ctrl.executors_info = []
        asyncio.run(ctrl.update_processed_data())
        owned_q0 = ctrl._state["owned_quote"]
        owned_b0 = ctrl._state["owned_base"]
        fund0 = ctrl.processed_data["managed_fund_value_quote"]
        ceil0 = ctrl.processed_data["deploy_ceiling_quote"]

        # Deposit 5000 USDT, no fill (no order changed).
        balances["USDT"] = (D(5100), D(5100))
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual(ctrl._state["owned_quote"], owned_q0)
        self.assertEqual(ctrl._state["owned_base"], owned_b0)
        self.assertEqual(ctrl.processed_data["managed_fund_value_quote"], fund0)
        self.assertEqual(ctrl.processed_data["deploy_ceiling_quote"], ceil0)

    def test_fill_growth_deploys_up_to_max_fund_value_quote(self):
        # Realized growth lifts the managed fund; the deploy ceiling is hard-capped at
        # max_fund_value_quote (5000) and the wallet-sized budgets honor that cap.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(20000), D(20000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, max_fund_value_quote=Decimal("5000"))
        self._init_state(ctrl, owned_quote=8000, owned_base=0, seed_value=300)
        ctrl.executors_info = []
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual(ctrl.processed_data["deploy_ceiling_quote"], D(5000))
        free_buy = ctrl.processed_data["free_buy_budget_quote"]
        free_sell = ctrl.processed_data["free_sell_budget_base"]
        deployed = free_buy + free_sell * D(300)
        self.assertLessEqual(deployed, D(5000))
        self.assertEqual(deployed, D(5000))

    def test_ceiling_compounds_below_cap(self):
        ctrl = self._build(_make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                                     mid=300, bid=299, ask=301))
        self.assertEqual(ctrl._compute_deploy_ceiling(D(300), D(900)), D(900))     # compounds
        self.assertEqual(ctrl._compute_deploy_ceiling(D(300), D(100)), D(300))     # never below seed
        self.assertEqual(ctrl._compute_deploy_ceiling(D(300), D(99999)), D(5000))  # capped at 5000


# =================================================== 15 & 16: legacy compat / toggle off

class TestLegacyCompatAndToggle(_Harness):

    def test_legacy_cooldown_time_defaults_both_split_timers(self):
        cfg = _make_config(cooldown_time=77, buy_cooldown_time=None, sell_cooldown_time=None)
        self.assertEqual(cfg.effective_buy_cooldown_time, 77)
        self.assertEqual(cfg.effective_sell_cooldown_time, 77)

    def test_split_timer_overrides_legacy(self):
        cfg = _make_config(cooldown_time=77, buy_cooldown_time=10, sell_cooldown_time=20)
        self.assertEqual(cfg.effective_buy_cooldown_time, 10)
        self.assertEqual(cfg.effective_sell_cooldown_time, 20)

    def test_toggle_off_reverts_to_legacy_age_refresh(self):
        # event_refresh_enabled=False: an aged-past-refresh order is cancelled by the legacy
        # per-executor-age path, and the per-side dirty machinery never engages.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, event_refresh_enabled=False, executor_refresh_time=600)
        self._init_state(ctrl, owned_quote=200, owned_base=0, seed_value=400)
        mdp.time.return_value = 10_000.0
        aged = _resting("buy_321", TradeType.BUY, "321", "0.1", "b0")
        aged.timestamp = 10_000.0 - 700  # age 700 > 600
        ctrl.executors_info = [aged]
        stops = ctrl.stop_actions_proposal()
        self.assertEqual({a.executor_id for a in stops}, {"b0"})  # legacy age-refresh cancel
        self.assertFalse(ctrl._buy_side_dirty)                    # per-side machinery untouched

    def test_toggle_off_uses_per_level_cooldown(self):
        # Legacy per-level cooldown active when toggled off: a just-closed level is blocked.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, event_refresh_enabled=False, cooldown_time=30)
        self._init_state(ctrl, owned_quote=200, owned_base=0, seed_value=400)
        mdp.time.return_value = 10_000.0
        closed = _resting("buy_321", TradeType.BUY, "321", "0.1", "b0",
                          status=RunnableStatus.TERMINATED)
        closed.close_timestamp = 10_000.0 - 1  # within 30s cooldown
        ctrl.executors_info = [closed]
        self.assertIn("buy_321", ctrl._recently_closed_level_ids())

    def test_event_mode_ignores_per_level_cooldown(self):
        # In event mode a closed level is NOT parked on a per-level cooldown (the per-side model
        # governs instead) -- only LIVE orders block their level.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, event_refresh_enabled=True, cooldown_time=30)
        self._init_state(ctrl, owned_quote=200, owned_base=0, seed_value=400)
        mdp.time.return_value = 10_000.0
        closed = _resting("buy_321", TradeType.BUY, "321", "0.1", "b0",
                          status=RunnableStatus.TERMINATED)
        closed.close_timestamp = 10_000.0 - 1
        ctrl.executors_info = [closed]
        self.assertNotIn("buy_321", ctrl._recently_closed_level_ids())


# =================================================== 17: invariants intact after refresh

class TestInvariantsIntact(_Harness):

    def test_passive_and_min_notional_respected_on_rebuild(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(120), D(120))}
        mdp = _make_mdp(balances=balances, mid=335, bid=320.5, ask=349.5)
        ctrl = self._build(mdp,
                           buy_prices=[Decimal("325"), Decimal("320"), Decimal("315")],
                           buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
                           sell_prices=[Decimal("350"), Decimal("355")],
                           sell_amounts_pct=[Decimal("1"), Decimal("1")],
                           min_order_quote=Decimal("5"))
        self._init_state(ctrl, owned_quote=120, owned_base=0, seed_value=400)
        asyncio.run(ctrl.update_processed_data())
        ctrl._buy_side_dirty = True
        creates = ctrl._create_buy_actions()
        for a in creates:
            self.assertLess(a.executor_config.price, D("320.5"))  # passive: below best_bid
            self.assertGreaterEqual(a.executor_config.amount * a.executor_config.price, D(5))
        self.assertNotIn(Decimal("325"), {a.executor_config.price for a in creates})  # 325 > bid

    def test_ceiling_throttle_holds_after_refresh(self):
        balances = {"XMR": (D(50), D(50)), "USDT": (D(50000), D(50000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, max_fund_value_quote=Decimal("1000"))
        self._init_state(ctrl, owned_quote=500, owned_base="1", seed_value=1000)
        asyncio.run(ctrl.update_processed_data())
        free_buy = ctrl.processed_data["free_buy_budget_quote"]
        free_sell = ctrl.processed_data["free_sell_budget_base"]
        self.assertLessEqual(free_buy + free_sell * D(300), D(1000))


# =================================================== regression

class TestPerSideRegression(_Harness):

    def test_state_schema_version_unchanged(self):
        self.assertEqual(RangeInventoryLadderController.STATE_SCHEMA_VERSION, 10)

    def test_new_config_fields_are_updatable(self):
        for name in ("event_refresh_enabled", "buy_cooldown_time", "sell_cooldown_time",
                     "max_fund_value_quote", "executor_refresh_time"):
            extra = RangeInventoryLadderConfig.model_fields[name].json_schema_extra or {}
            self.assertTrue(extra.get("is_updatable", False), name)

    def test_max_fund_default_is_5000(self):
        cfg = RangeInventoryLadderConfig(
            id="t", controller_name="range_inventory_ladder", controller_type="market_making",
            connector_name="nonkyc", trading_pair="XMR-USDT", total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")])
        self.assertEqual(cfg.max_fund_value_quote, Decimal("5000"))
        self.assertTrue(cfg.event_refresh_enabled)


if __name__ == "__main__":
    unittest.main()
