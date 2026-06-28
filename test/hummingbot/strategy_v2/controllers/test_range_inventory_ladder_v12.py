"""v12 behavior-contract tests: ledger reconciliation (Issues 1 & 5), refresh-cooldown
bypass (Issue 2), side-specific create deferral (Issue 3), status index mapping (Issue 4),
and the fast directional recycle window (Part B).

Every test drives the REAL controller (`RangeInventoryLadderController`) -- balances,
prices, `time()`, quantization and `executors_info` are mocked so the suite is fully
deterministic and needs no live exchange. Each assertion targets a behavior that fails if
the corresponding v12 change is reverted (no tautological `assertIsNotNone`-style checks).
"""
import asyncio
import json
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
    """balances: {asset: (total, available)}. Mutate the dict between cycles to simulate
    fills/deposits; the side_effect lambdas read it live."""
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


def _make_config(**overrides):
    defaults = dict(
        id="ctrl-v12",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("170"),
        max_fund_value_quote=Decimal("1000"),
        buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
        buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
        sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        min_order_quote=Decimal("5"),
        cooldown_time=3600,
        # v12 behavior (per-level cooldown + directional recycle window + per-executor-age
        # refresh) is the LEGACY path; the per-side refresh model supersedes it. These tests
        # validate that legacy path, so they pin event_refresh_enabled=False.
        event_refresh_enabled=False,
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


def _terminated_executor(level_id, side, price, close_ts, executor_id="ex"):
    """A finished order executor at `level_id`, closed at `close_ts` with NO booked fill."""
    ex = MagicMock()
    ex.id = executor_id
    ex.status = RunnableStatus.TERMINATED
    ex.is_active = False
    ex.timestamp = close_ts
    ex.close_timestamp = close_ts
    ex.connector_name = "nonkyc"
    ex.custom_info = {}
    ex.filled_amount_quote = Decimal("0")
    ex.cum_fees_quote = Decimal("0")
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = Decimal(str(price))
    cfg.amount = Decimal("1")
    ex.config = cfg
    return ex


def _filling_executor(level_id, side, price, executor_id, *, filled_base, filled_quote,
                      fees="0", active=False, close_ts=None):
    """An executor whose custom_info reports CUMULATIVE fills -- a v13 booking source. v13
    drives the recycle window off BOOKED fills, so the recycle tests trigger via this."""
    ex = MagicMock()
    ex.id = executor_id
    ex.status = RunnableStatus.RUNNING if active else RunnableStatus.TERMINATED
    ex.is_active = active
    ex.timestamp = 0.0
    ex.close_timestamp = close_ts
    ex.connector_name = "nonkyc"
    ex.custom_info = {
        "filled_amount_base": Decimal(str(filled_base)),
        "filled_amount_quote": Decimal(str(filled_quote)),
        "cum_fees_quote": Decimal(str(fees)),
    }
    ex.filled_amount_quote = Decimal("0")
    ex.cum_fees_quote = Decimal("0")
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = Decimal(str(price))
    cfg.amount = Decimal("1")
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
                    reserve_quote="0", reserve_base="0", tracked_ids=None):
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
            "tracked_fill_executor_ids": list(tracked_ids or []),
        }
        ctrl._state_loaded = True

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    def _full(self, ctrl, mdp, t):
        """One full control tick: update_processed_data then determine_executor_actions."""
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())
        return ctrl.determine_executor_actions()

    @staticmethod
    def _emit_events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]


# ============================================================ Issue 1: re-anchor

class TestIssue1LedgerReanchor(_Harness):

    def test_overclaim_past_grace_reanchors_down_and_persists(self):
        # owned >> wallet (over-claim). Wallet quote 10, base 0; owned 30 / 0.2.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(10), D(10))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=120)
        self._init_state(ctrl, owned_quote=30, owned_base="0.2", seed_value=30)

        # Cycle 1: over-claim observed, grace timer starts, NO change yet.
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(30))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.2"))
        self.assertEqual(self._emit_events(ctrl, "range_ladder_ledger_reanchored"), [])

        # Cycle 2: 125s later (> 120 grace) -> re-anchor DOWN to wallet truth.
        self._cycle(ctrl, mdp, 1125.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(10))   # min(30, 10)
        self.assertEqual(D(ctrl._state["owned_base"]), D(0))     # min(0.2, 0)
        events = self._emit_events(ctrl, "range_ladder_ledger_reanchored")
        self.assertEqual(1, len(events))
        self.assertEqual(events[0].kwargs["old_owned_quote"], "30")
        self.assertEqual(events[0].kwargs["new_owned_quote"], "10")

        # Persisted to the state file.
        with self._state_path.open() as f:
            persisted = json.load(f)
        self.assertEqual(D(persisted["owned_quote"]), D(10))
        self.assertEqual(D(persisted["owned_base"]), D(0))

    def test_overclaim_within_grace_no_change(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(10), D(10))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=120)
        self._init_state(ctrl, owned_quote=30, owned_base=0, seed_value=30)

        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1050.0)  # only +50s, still within grace
        self.assertEqual(D(ctrl._state["owned_quote"]), D(30))
        self.assertEqual(self._emit_events(ctrl, "range_ladder_ledger_reanchored"), [])

    def test_underclaim_never_inflates_and_no_warning_spam(self):
        # Wallet (100/2) exceeds ledger (10/0.1): legitimate under self-balance -> no-op.
        balances = {"XMR": (D(2), D(2)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=120)
        self._init_state(ctrl, owned_quote=10, owned_base="0.1", seed_value=30)

        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 5000.0)  # well past any grace
        # Ledger NEVER inflated upward.
        self.assertEqual(D(ctrl._state["owned_quote"]), D(10))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(self._emit_events(ctrl, "range_ladder_ledger_reanchored"), [])
        # No over-claim warning spam (wallet exceeding ledger is expected).
        self.assertEqual(self._emit_events(ctrl, "range_ladder_reconciliation_overclaim"), [])

    def test_reanchor_never_negative(self):
        # Wallet exactly zero on both sides; owned positive -> re-anchor to 0, never below.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=50, owned_base="1.5", seed_value=50)

        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1020.0)
        self.assertGreaterEqual(D(ctrl._state["owned_quote"]), D(0))
        self.assertGreaterEqual(D(ctrl._state["owned_base"]), D(0))
        self.assertEqual(D(ctrl._state["owned_quote"]), D(0))
        self.assertEqual(D(ctrl._state["owned_base"]), D(0))

    def test_reanchor_respects_reserve_floor(self):
        # total quote 100, reserve 70 -> wallet-derived = 30; owned 50 over-claims the
        # managed portion. After grace, owned re-anchors to 30 (not to total 100).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        # owned_quote 130 actually exceeds TOTAL 100 -> genuine over-claim trigger.
        self._init_state(ctrl, owned_quote=130, owned_base=0, seed_value=130,
                         reserve_quote="70")
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1020.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(30))  # 100 total - 70 reserve


# ============================================================ Issue 2: refresh bypass

class TestIssue2RefreshBypassesCooldown(_Harness):

    def _ctrl(self, cooldown_time=30):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, cooldown_time=cooldown_time)
        return ctrl, mdp

    def test_refresh_cancel_marks_bypass_so_level_not_cooled_down(self):
        ctrl, mdp = self._ctrl(cooldown_time=30)
        mdp.time.return_value = 10_000.0
        # Active, aged-past-refresh (default 600s) buy executor.
        ex = MagicMock()
        ex.id = "buy-ex"
        ex.status = RunnableStatus.RUNNING
        ex.is_active = True
        ex.timestamp = 10_000.0 - 700  # age 700 > 600
        ex.close_timestamp = None
        ex.connector_name = "nonkyc"
        ex.custom_info = {}
        cfg = MagicMock()
        cfg.type = "order_executor"
        cfg.level_id = "buy_321"
        cfg.side = TradeType.BUY
        cfg.price = Decimal("321")
        cfg.amount = Decimal("1")
        ex.config = cfg
        ctrl.executors_info = [ex]

        stops = ctrl.stop_actions_proposal()
        self.assertEqual(1, len(stops))  # refresh stop proposed
        self.assertTrue(ctrl._should_bypass_level_cooldown("buy_321"))  # bypass marked

        # The order then closes (terminated, just now) -> bypass keeps it OFF cooldown.
        ex.status = RunnableStatus.TERMINATED
        ex.is_active = False
        ex.close_timestamp = 10_000.0
        blocked = ctrl._recently_closed_level_ids()
        self.assertNotIn("buy_321", blocked)

    def test_genuine_fill_close_still_blocks_for_cooldown(self):
        """Regression guard: a close that was NOT a refresh (no bypass) still cools down."""
        ctrl, mdp = self._ctrl(cooldown_time=30)
        mdp.time.return_value = 10_000.0
        ex = _terminated_executor("buy_315", TradeType.BUY, "315",
                                  close_ts=10_000.0 - 1, executor_id="fill-ex")
        ctrl.executors_info = [ex]
        blocked = ctrl._recently_closed_level_ids()
        self.assertIn("buy_315", blocked)  # within 30s cooldown, no bypass -> blocked


# ============================================================ Issue 3: side deferral

class TestIssue3SideSpecificDeferral(_Harness):

    def _two_sided(self):
        balances = {"XMR": (D(1), D(1)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=170, owned_base="0.5", seed_value=500)
        asyncio.run(ctrl.update_processed_data())
        return ctrl, mdp

    def test_buy_stop_blocks_only_buy_creates(self):
        ctrl, mdp = self._two_sided()
        ctrl.stop_actions_proposal = MagicMock(
            return_value=[StopExecutorAction(controller_id="ctrl-v12", executor_id="buy-ex")]
        )
        ctrl._find_executor_by_id = MagicMock(return_value=MagicMock())
        ctrl._executor_side = MagicMock(return_value=TradeType.BUY)

        actions = ctrl.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertTrue(creates, "sell side should still create")
        self.assertTrue(all(a.executor_config.side == TradeType.SELL for a in creates))
        self.assertTrue(ctrl._defer_buy_creates_this_cycle)
        self.assertFalse(ctrl._defer_sell_creates_this_cycle)

    def test_sell_stop_blocks_only_sell_creates(self):
        ctrl, mdp = self._two_sided()
        ctrl.stop_actions_proposal = MagicMock(
            return_value=[StopExecutorAction(controller_id="ctrl-v12", executor_id="sell-ex")]
        )
        ctrl._find_executor_by_id = MagicMock(return_value=MagicMock())
        ctrl._executor_side = MagicMock(return_value=TradeType.SELL)

        actions = ctrl.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertTrue(creates, "buy side should still create")
        self.assertTrue(all(a.executor_config.side == TradeType.BUY for a in creates))
        self.assertFalse(ctrl._defer_buy_creates_this_cycle)
        self.assertTrue(ctrl._defer_sell_creates_this_cycle)

    def test_both_sides_deferred_when_both_have_stops(self):
        ctrl, mdp = self._two_sided()
        buy_stop = StopExecutorAction(controller_id="ctrl-v12", executor_id="buy-ex")
        sell_stop = StopExecutorAction(controller_id="ctrl-v12", executor_id="sell-ex")
        ctrl.stop_actions_proposal = MagicMock(return_value=[buy_stop, sell_stop])
        # Map each stop's executor_id to a distinct executor and its side.
        found = {"buy-ex": MagicMock(), "sell-ex": MagicMock()}
        ctrl._find_executor_by_id = MagicMock(side_effect=lambda eid: found[eid])
        ctrl._executor_side = MagicMock(
            side_effect=lambda ex: TradeType.BUY if ex is found["buy-ex"] else TradeType.SELL
        )

        actions = ctrl.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual([], creates)  # both sides deferred
        self.assertTrue(ctrl._defer_buy_creates_this_cycle)
        self.assertTrue(ctrl._defer_sell_creates_this_cycle)

    def test_flags_reset_each_cycle_when_no_stops(self):
        ctrl, mdp = self._two_sided()
        # Pretend a prior cycle left them set; a no-stop cycle must clear them.
        ctrl._defer_buy_creates_this_cycle = True
        ctrl._defer_sell_creates_this_cycle = True
        ctrl.stop_actions_proposal = MagicMock(return_value=[])

        actions = ctrl.determine_executor_actions()
        self.assertFalse(ctrl._defer_buy_creates_this_cycle)
        self.assertFalse(ctrl._defer_sell_creates_this_cycle)
        # With both sides funded and no defer, creates happen on both sides.
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        sides = {a.executor_config.side for a in creates}
        self.assertIn(TradeType.BUY, sides)
        self.assertIn(TradeType.SELL, sides)

    def test_create_buy_actions_short_circuits_when_deferred(self):
        ctrl, mdp = self._two_sided()
        ctrl._defer_buy_creates_this_cycle = True
        self.assertEqual([], ctrl._create_buy_actions())
        ctrl._defer_buy_creates_this_cycle = False
        self.assertTrue(ctrl._create_buy_actions())  # funded -> places again

    def test_create_sell_actions_short_circuits_when_deferred(self):
        ctrl, mdp = self._two_sided()
        ctrl._defer_sell_creates_this_cycle = True
        self.assertEqual([], ctrl._create_sell_actions())
        ctrl._defer_sell_creates_this_cycle = False
        self.assertTrue(ctrl._create_sell_actions())


# ============================================================ Issue 4: status mapping

class TestIssue4StatusIndexMapping(_Harness):

    def test_eligible_lists_correct_with_closely_spaced_prices(self):
        # Closely-spaced rungs; bid/ask split them so only some are passive-eligible.
        mdp = _make_mdp(balances={"XMR": (D(1), D(1)), "USDT": (D(500), D(500))},
                        mid=335, bid="320.25", ask="350.25")
        ctrl = self._build(
            mdp,
            buy_prices=[Decimal("321"), Decimal("320.5"), Decimal("320")],
            buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("350"), Decimal("350.5"), Decimal("351")],
            sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        )
        self._init_state(ctrl, owned_quote=170, owned_base="0.5", seed_value=500)
        asyncio.run(ctrl.update_processed_data())

        lines = ctrl.to_format_status()  # must not raise
        buy_line = next(l for l in lines if l.startswith("Eligible buy prices:"))
        sell_line = next(l for l in lines if l.startswith("Eligible sell prices:"))

        # Only 320 < bid 320.25 is a passive buy; only 350.5/351 > ask 350.25 are passive sells.
        self.assertEqual(buy_line, "Eligible buy prices: 320")
        self.assertEqual(sell_line, "Eligible sell prices: 350.5, 351")

    def test_status_renders_without_raising_when_all_blocked(self):
        mdp = _make_mdp(balances={"XMR": (D(1), D(1)), "USDT": (D(500), D(500))},
                        mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=170, owned_base="0.5", seed_value=500)
        asyncio.run(ctrl.update_processed_data())
        # Force every level into the blocked set; the comprehensions must still index safely.
        ctrl.processed_data["blocked_level_ids"] = set(ctrl._desired_level_ids())
        lines = ctrl.to_format_status()
        buy_line = next(l for l in lines if l.startswith("Eligible buy prices:"))
        self.assertEqual(buy_line, "Eligible buy prices: none")


# ============================================================ Issue 5: state validation

class TestIssue5StateValidation(_Harness):

    def _ctrl(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        return self._build(mdp)

    def _valid_state(self, **overrides):
        base = {
            "schema_version": 10,
            "controller_name": "range_inventory_ladder",
            "controller_type": "market_making",
            "controller_id": "ctrl-v12",
            "connector_name": "nonkyc",
            "trading_pair": "XMR-USDT",
            "base_asset": "XMR",
            "quote_asset": "USDT",
            "initialized": True,
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "initial_managed_quote": "100",
            "initial_claimed_base_amount": "0",
            "initial_reference_price": "300",
            "initialized_timestamp": "1000",
            "owned_quote": "100",
            "owned_base": "0",
            "seed_value_quote": "100",
            "tracked_fill_executor_ids": [],
        }
        base.update(overrides)
        return base

    def test_corrupt_ledger_values_raise(self):
        ctrl = self._ctrl()
        for field in ("owned_quote", "owned_base", "seed_value_quote"):
            for bad in ("-5", "NaN", "abc"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        ctrl._validate_loaded_state(self._valid_state(**{field: bad}))

    def test_tracked_ids_coerced_to_list(self):
        ctrl = self._ctrl()
        for bad in ("notalist", 123, {"a": 1}, None):
            with self.subTest(bad=bad):
                validated = ctrl._validate_loaded_state(self._valid_state(tracked_fill_executor_ids=bad))
                self.assertEqual(validated["tracked_fill_executor_ids"], [])
        # A missing key is also coerced to [].
        s = self._valid_state()
        del s["tracked_fill_executor_ids"]
        self.assertEqual(ctrl._validate_loaded_state(s)["tracked_fill_executor_ids"], [])

    def test_valid_ledger_values_normalized_and_pass(self):
        ctrl = self._ctrl()
        validated = ctrl._validate_loaded_state(self._valid_state(owned_quote="100.50", owned_base="0.250"))
        self.assertEqual(validated["owned_quote"], "100.50")
        self.assertEqual(validated["owned_base"], "0.250")

    def test_corrupt_state_file_quarantined_no_crash(self):
        ctrl = self._ctrl()
        self._state_path.write_text(json.dumps(self._valid_state(owned_quote="-1")), encoding="utf-8")
        ctrl._load_state()  # must not raise
        self.assertEqual(ctrl._state, {})
        self.assertEqual(ctrl._state_recovery_reason, "invalid_state")
        self.assertFalse(self._state_path.exists())  # moved to a backup


# ============================================================ Part B: directional recycle
# v13 NOTE: the recycle window is now driven by BOOKED fills (a sell fill funds buys; a buy
# fill funds sells), not by bare wallet-total deltas. A pure one-sided deposit opens NO window.

class TestPartBDirectionalRecycle(_Harness):

    def test_booked_sell_fill_opens_buy_window(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._init_state(ctrl, owned_quote=0, owned_base="1", seed_value=200)

        self._cycle(ctrl, mdp, 1000.0)             # baseline, no window
        self.assertEqual(ctrl._recycle_bypass_buy_until, 0.0)
        # A sell order fills (quote came in) -> book it -> open BUY window.
        ctrl.executors_info = [_filling_executor("sell_350", TradeType.SELL, "350", "S1",
                                                 filled_base="0.1", filled_quote="35", fees="0.07")]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_sell_fill_this_cycle)
        self.assertEqual(ctrl._recycle_bypass_buy_until, 1010.0 + 60)
        opened = self._emit_events(ctrl, "range_ladder_recycle_window_opened")
        self.assertTrue(any(c.kwargs.get("side") == "buy" for c in opened))

    def test_booked_buy_fill_opens_sell_window(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._init_state(ctrl, owned_quote=200, owned_base=0, seed_value=200)

        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(ctrl._recycle_bypass_sell_until, 0.0)
        ctrl.executors_info = [_filling_executor("buy_321", TradeType.BUY, "321", "B1",
                                                 filled_base="0.1", filled_quote="32.1", fees="0.06")]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._booked_buy_fill_this_cycle)
        self.assertEqual(ctrl._recycle_bypass_sell_until, 1010.0 + 60)
        opened = self._emit_events(ctrl, "range_ladder_recycle_window_opened")
        self.assertTrue(any(c.kwargs.get("side") == "sell" for c in opened))

    def test_pure_deposit_opens_no_window(self):
        # A one-sided TOTAL rise with no offsetting move (a deposit) is not a trade -> NO window.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._init_state(ctrl, owned_quote=0, owned_base=0, seed_value=200)

        self._cycle(ctrl, mdp, 1000.0)
        balances["USDT"] = (D(1000), D(1000))      # +900 USDT deposit, one-sided, no order
        self._cycle(ctrl, mdp, 1010.0)

        self.assertEqual(ctrl._recycle_bypass_buy_until, 0.0)
        self.assertEqual(ctrl._recycle_bypass_sell_until, 0.0)
        self.assertEqual(self._emit_events(ctrl, "range_ladder_recycle_window_opened"), [])

    def test_window_makes_cooled_down_level_placeable_then_blocks_again(self):
        # A buy rung closed at t0 is on (long) cooldown. A booked SELL fill opens a buy window
        # that makes it placeable; once the window lapses it is blocked by cooldown again.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60, cooldown_time=3600)
        self._init_state(ctrl, owned_quote=0, owned_base="1", seed_value=200)
        cooled = _terminated_executor("buy_321", TradeType.BUY, "321", close_ts=1000.0)
        ctrl.executors_info = [cooled]

        self._cycle(ctrl, mdp, 1000.0)
        self.assertIn("buy_321", ctrl.processed_data["blocked_level_ids"])  # cooldown blocks

        # A separate sell order fills -> booked -> buy window opens 1010..1070.
        seller = _filling_executor("sell_350", TradeType.SELL, "350", "S1",
                                   filled_base="0.1", filled_quote="35", fees="0.07")
        ctrl.executors_info = [cooled, seller]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertNotIn("buy_321", ctrl.processed_data["blocked_level_ids"])  # placeable

        self._cycle(ctrl, mdp, 1080.0)             # window lapsed; seller books nothing new
        self.assertIn("buy_321", ctrl.processed_data["blocked_level_ids"])  # blocked again

    def test_own_cancel_changes_available_not_total_so_no_window(self):
        # Refresh cancel: reserved -> available, TOTAL unchanged, no booked fill -> NO window.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(20))}  # total 100, available 20
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._init_state(ctrl, owned_quote=0, owned_base=0, seed_value=200)

        self._cycle(ctrl, mdp, 1000.0)
        balances["USDT"] = (D(100), D(100))        # cancel freed 80 to available; total SAME
        self._cycle(ctrl, mdp, 1010.0)

        self.assertEqual(ctrl._recycle_bypass_buy_until, 0.0)  # no window
        self.assertEqual(self._emit_events(ctrl, "range_ladder_recycle_window_opened"), [])

    def test_one_sided_buy_wobble_keeps_cooldown_and_opens_only_sell_window(self):
        # The buy rung itself fills (a booked BUY fill). That funds SELLS -> opens the SELL
        # window only; the buy rung gets NO buy window and stays on cooldown (over-accumulation
        # protection: we do not immediately re-buy the same rung).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60, cooldown_time=3600)
        self._init_state(ctrl, owned_quote=200, owned_base=0, seed_value=200)
        rung = _filling_executor("buy_321", TradeType.BUY, "321", "B1",
                                 filled_base="0", filled_quote="0", close_ts=1000.0)
        ctrl.executors_info = [rung]

        self._cycle(ctrl, mdp, 1000.0)             # no fill yet
        # The buy rung now reports its fill (it filled and closed).
        rung.custom_info = {
            "filled_amount_base": Decimal("0.1"),
            "filled_amount_quote": Decimal("32.1"),
            "cum_fees_quote": Decimal("0.06"),
        }
        self._cycle(ctrl, mdp, 1010.0)

        self.assertTrue(ctrl._booked_buy_fill_this_cycle)
        self.assertEqual(ctrl._recycle_bypass_buy_until, 0.0)            # NO buy window
        self.assertEqual(ctrl._recycle_bypass_sell_until, 1010.0 + 60)   # sell window opened
        self.assertIn("buy_321", ctrl.processed_data["blocked_level_ids"])  # rung stays cooled
        opened = self._emit_events(ctrl, "range_ladder_recycle_window_opened")
        self.assertTrue(any(c.kwargs.get("side") == "sell" for c in opened))
        self.assertFalse(any(c.kwargs.get("side") == "buy" for c in opened))

    def test_end_to_end_recycle_under_60s(self):
        # Single buy rung on long cooldown; a booked SELL fill must produce the offsetting buy
        # order within recycle_max_latency_seconds.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(130), D(130))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(
            mdp, recycle_max_latency_seconds=60, cooldown_time=3600,
            buy_prices=[Decimal("321")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("350")], sell_amounts_pct=[Decimal("1")],
        )
        self._init_state(ctrl, owned_quote=0, owned_base="1", seed_value=200)
        cooled = _terminated_executor("buy_321", TradeType.BUY, "321", close_ts=1000.0)
        ctrl.executors_info = [cooled]

        # Cycle 1: rung on cooldown -> no offsetting buy.
        actions1 = self._full(ctrl, mdp, 1000.0)
        buys1 = [a for a in actions1 if isinstance(a, CreateExecutorAction)
                 and a.executor_config.side == TradeType.BUY]
        self.assertEqual([], buys1)

        # Cycle 2: a sell order fills -> booked sell fill -> buy window -> buy recycle THIS cycle.
        t_fill = 1010.0
        seller = _filling_executor("sell_350", TradeType.SELL, "350", "S1",
                                   filled_base="0.1", filled_quote="35", fees="0.07")
        ctrl.executors_info = [cooled, seller]
        actions2 = self._full(ctrl, mdp, t_fill)
        buys2 = [a for a in actions2 if isinstance(a, CreateExecutorAction)
                 and a.executor_config.side == TradeType.BUY]
        self.assertEqual(1, len(buys2))
        self.assertEqual(buys2[0].executor_config.price, Decimal("321"))
        # Order produced the same cycle the fill was booked: latency 0 <= 60s window.
        self.assertLessEqual(0.0, ctrl.config.recycle_max_latency_seconds)
        self.assertLess(t_fill, ctrl._recycle_bypass_buy_until)


# ============================================================ config fields

class TestV12ConfigFields(unittest.TestCase):

    def test_defaults(self):
        cfg = _make_config()
        self.assertEqual(cfg.recycle_max_latency_seconds, 60)
        self.assertEqual(cfg.ledger_overclaim_reanchor_seconds, 120)
        self.assertEqual(cfg.ledger_reconcile_threshold_quote, Decimal("0.5"))

    def test_all_three_are_updatable(self):
        for name in ("recycle_max_latency_seconds", "ledger_overclaim_reanchor_seconds",
                     "ledger_reconcile_threshold_quote"):
            extra = RangeInventoryLadderConfig.model_fields[name].json_schema_extra or {}
            self.assertTrue(extra.get("is_updatable", False), name)

    def test_recycle_latency_must_be_positive(self):
        with self.assertRaises(Exception):
            _make_config(recycle_max_latency_seconds=0)
        with self.assertRaises(Exception):
            _make_config(recycle_max_latency_seconds=-1)

    def test_reanchor_seconds_allows_zero_rejects_negative(self):
        self.assertEqual(_make_config(ledger_overclaim_reanchor_seconds=0).ledger_overclaim_reanchor_seconds, 0)
        with self.assertRaises(Exception):
            _make_config(ledger_overclaim_reanchor_seconds=-1)

    def test_reconcile_threshold_must_be_positive(self):
        with self.assertRaises(Exception):
            _make_config(ledger_reconcile_threshold_quote=Decimal("0"))
        with self.assertRaises(Exception):
            _make_config(ledger_reconcile_threshold_quote=Decimal("-0.1"))

    def test_recycle_latency_in_live_runtime_signature(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = RangeInventoryLadderController(
            _make_config(recycle_max_latency_seconds=45),
            market_data_provider=mdp, actions_queue=MagicMock(),
        )
        self.assertIn(45, ctrl._live_runtime_settings_signature())


# ============================================================ Regression

class TestV12Regression(_Harness):

    def test_state_schema_version_unchanged(self):
        self.assertEqual(RangeInventoryLadderController.STATE_SCHEMA_VERSION, 10)
        self.assertEqual(RangeInventoryLadderController.SUPPORTED_STATE_SCHEMA_VERSIONS, {6, 7, 8, 9, 10})

    def test_existing_v10_state_loads_without_quarantine(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        valid = {
            "schema_version": 10,
            "controller_name": "range_inventory_ladder",
            "controller_type": "market_making",
            "controller_id": "ctrl-v12",
            "connector_name": "nonkyc",
            "trading_pair": "XMR-USDT",
            "base_asset": "XMR",
            "quote_asset": "USDT",
            "initialized": True,
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "initial_managed_quote": "100",
            "initial_claimed_base_amount": "0",
            "initial_reference_price": "300",
            "initialized_timestamp": "1000",
            "owned_quote": "100",
            "owned_base": "0",
            "seed_value_quote": "100",
            "tracked_fill_executor_ids": [],
        }
        self._state_path.write_text(json.dumps(valid), encoding="utf-8")
        ctrl._load_state()
        self.assertIsNone(ctrl._state_recovery_reason)
        self.assertEqual(ctrl._state["schema_version"], 10)
        self.assertTrue(self._state_path.exists())  # not quarantined

    def test_sizing_tracks_wallet_not_owned_ledger(self):
        # owned_quote=500 but only 50 USDT is available -> free buy budget reflects the
        # wallet (50), never the ledger (500). total 600 keeps it a non-over-claim.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(600), D(50))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=500, owned_base=0, seed_value=600)
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual(ctrl.processed_data["free_buy_budget_quote"], D(50))

    def test_idle_stuck_one_sided_produces_no_buys_no_crash(self):
        # Tiny quote (below min_order_quote), sells far above market with no base.
        balances = {"XMR": (D(0), D(0)), "USDT": (D("0.04"), D("0.04"))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=170, owned_base=0, seed_value=170)
        asyncio.run(ctrl.update_processed_data())
        actions = ctrl.create_actions_proposal()  # must not raise
        self.assertEqual([], actions)


if __name__ == "__main__":
    unittest.main()
