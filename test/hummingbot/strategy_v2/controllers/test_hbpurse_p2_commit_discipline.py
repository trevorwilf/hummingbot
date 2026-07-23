"""hbpurse Phase 2 behavior-contract tests: commit-before-advance state discipline (CDX-M01),
booked_fill_progress + P1-key load validation (CLA-M03), init Decimal hygiene (F10), and the
quarantine backup-move OSError carry-along (A2).

Every test drives the REAL controller (`RangeInventoryLadderController`) with mocked balances,
prices, time() and executors_info, exactly like the v12/v13/P1 suites. IO failure is simulated by
patching the atomic writer (`_write_state_to_disk`) to raise -- never by real disk manipulation.
Every expected value is derived BY HAND from the finding/spec arithmetic (HBPURSE_FINDINGS.md
ADDENDUM A1: CDX-M01/CLA-M03, F10; A2 carry-along), never from running the implementation.

The discriminating assertions (named for the reviewer's TEST INTEGRITY AUDIT):
- Booking retry: after a FAILED save the in-memory owned_quote must still read its PRIOR value
  (100), NOT the advanced value (70). Reverting _commit_state to the old mutate-then-save order
  (assign self._state[...] then _save_state) makes owned_quote read 70 after the failed cycle ->
  test_booking_save_failure_* FAILS.
- Reseed retry: after a FAILED reseed save, last_reseed_token must be ABSENT (token not consumed).
  mutate-then-save sets it before the failing save -> test_reseed_save_failure_* FAILS.
- Degraded gate: while accounting_degraded the create proposal must be []. Deleting the
  `if self._accounting_degraded: return []` gate makes the degraded controller place its sell
  ladder -> test_accounting_degraded_suppresses_new_orders FAILS.
- CLA-M03: a non-numeric booked_fill_progress base must quarantine on load. Deleting the progress
  validation loads it clean -> test_corrupt_progress_entry_quarantines FAILS.
- F10: a NaN init balance must REFUSE init. Reverting self._d(...) to raw Decimal(...) accepts
  Decimal('NaN') without raising -> test_non_finite_init_balance_refuses FAILS.
- A2: a failed backup os.replace must emit range_ladder_state_backup_failed. Restoring the bare
  `except OSError: pass` drops the event -> test_backup_move_failure_* FAILS.
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

import range_inventory_ladder as ril  # noqa: E402
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
    """balances: {asset: (total, available)}. Mutate the dict between cycles to simulate
    fills/deposits/withdrawals; the side_effect lambdas read it live."""
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
        id="ctrl-hbpurse-p2",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("170"),
        max_fund_value_quote=Decimal("100000"),
        buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
        buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
        sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        min_order_quote=Decimal("5"),
        cooldown_time=3600,
        event_refresh_enabled=False,
        fee_rate=Decimal("0"),
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


def _filling_executor(level_id, side, price, executor_id, *, filled_base, filled_quote,
                      fees="0", active=True):
    ex = MagicMock()
    ex.id = executor_id
    ex.status = RunnableStatus.RUNNING if active else RunnableStatus.TERMINATED
    ex.is_active = active
    ex.timestamp = 0.0
    ex.close_timestamp = None
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
                    reserve_quote="0", reserve_base="0", **extra):
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
        ctrl._state.update(extra)
        ctrl._state_loaded = True

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    @staticmethod
    def _install_flaky_writer(ctrl, fail_on_calls):
        """Replace the atomic writer with one that raises OSError on the given 1-based call
        numbers and delegates to the REAL writer otherwise. Returns the call-counter dict."""
        real_write = ctrl._write_state_to_disk
        counter = {"n": 0}

        def flaky_write(state):
            counter["n"] += 1
            if counter["n"] in fail_on_calls:
                raise OSError(f"simulated disk failure on write #{counter['n']}")
            return real_write(state)

        ctrl._write_state_to_disk = flaky_write
        return counter

    @staticmethod
    def _emit_events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]

    def _persisted(self):
        with self._state_path.open() as f:
            return json.load(f)


# ==================================================== CDX-M01: booking commit discipline

class TestBookingCommitDiscipline(_Harness):

    def _buy_fill_ctrl(self):
        # owned_quote=100 (ledger, pre-book) vs wallet USDT 70 (the BUY's 30 spend already left
        # the wallet), XMR 0.1 (the 0.1 base already arrived). A huge re-anchor grace keeps the
        # transient over-claim from cutting so ONLY the booking save is under test.
        balances = {"XMR": (D("0.1"), D("0.1")), "USDT": (D(70), D(70))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=100000)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=130)
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy", filled_base="0.1", filled_quote="30",
        )]
        return ctrl, mdp, balances

    def test_booking_save_failure_leaves_inmemory_prior_then_retries_once(self):
        ctrl, mdp, _ = self._buy_fill_ctrl()
        self._install_flaky_writer(ctrl, fail_on_calls={1})

        # Cycle 1: the booking save fails. In-memory ledger MUST stay at its PRIOR value.
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(100))   # NOT advanced to 70
        self.assertEqual(D(ctrl._state["owned_base"]), D(0))
        # progress not advanced in memory (prior state never had it) and nothing on disk.
        self.assertEqual(ctrl._state.get("booked_fill_progress", {}), {})
        self.assertFalse(self._state_path.exists())
        self.assertTrue(ctrl._accounting_degraded)
        self.assertEqual(ctrl._state_io_failures, 1)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_commit_failed")))
        # The unpersisted fill must NOT drive the refresh machine (existing orders untouched):
        # the per-cycle booked-fill flags are rolled back on a failed commit.
        self.assertFalse(ctrl._booked_buy_fill_this_cycle)
        self.assertFalse(ctrl._booked_sell_fill_this_cycle)

        # Cycle 2: the writer works -> the SAME fill books exactly once (100 - 30 = 70).
        self._cycle(ctrl, mdp, 1001.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(70))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(D(self._persisted()["owned_quote"]), D(70))
        self.assertEqual(D(self._persisted()["owned_base"]), D("0.1"))
        # Recovered: degraded cleared, exactly one recovery event, failure count did not grow.
        self.assertFalse(ctrl._accounting_degraded)
        self.assertEqual(ctrl._state_io_failures, 1)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_accounting_degraded_cleared")))

    def test_failed_booking_does_not_dirty_opposite_side(self):
        # event_refresh mode: a buy fill normally dirties the SELL side (to redeploy). A fill that
        # fails to durably book must NOT dirty it -- otherwise the refresh machine would cancel the
        # existing sell orders on an unpersisted fill. Seed the refresh timers past initial
        # placement so ONLY the fill could dirty a side.
        balances = {"XMR": (D("0.1"), D("0.1")), "USDT": (D(70), D(70))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=100000,
                           event_refresh_enabled=True, executor_refresh_time=100000)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=130)
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy", filled_base="0.1", filled_quote="30",
        )]
        ctrl._refresh_timers_initialized = True   # past the initial-placement dirtying
        ctrl._last_global_refresh_ts = 1000.0
        self._install_flaky_writer(ctrl, fail_on_calls={1})

        self._cycle(ctrl, mdp, 1000.0)   # booking save fails
        self.assertTrue(ctrl._accounting_degraded)
        self.assertFalse(ctrl._sell_side_dirty)   # NOT dirtied by the unpersisted buy fill
        self.assertFalse(ctrl._buy_side_dirty)

    def test_booking_retry_books_exactly_once_not_twice(self):
        # After recovery the fill must not be booked a SECOND time on a later idle cycle: the
        # persisted progress baseline makes d_base/d_quote zero, so owned_* stay at 70/0.1.
        ctrl, mdp, _ = self._buy_fill_ctrl()
        self._install_flaky_writer(ctrl, fail_on_calls={1})
        self._cycle(ctrl, mdp, 1000.0)   # fail
        # CDX-R05 discriminator: the FAILED cycle must NOT have advanced the in-memory ledger, and
        # nothing may be on disk. Reverting _commit_state to mutate-then-save (self._state =
        # candidate BEFORE the write) leaves owned_quote at 70 here -> this test FAILS, so it can no
        # longer pass under the exact mutation it claims to catch.
        self.assertEqual(D(ctrl._state["owned_quote"]), D(100))
        self.assertEqual(D(ctrl._state["owned_base"]), D(0))
        self.assertEqual(ctrl._state.get("booked_fill_progress", {}), {})
        self.assertFalse(self._state_path.exists())
        self._cycle(ctrl, mdp, 1001.0)   # book once
        self.assertEqual(D(ctrl._state["owned_quote"]), D(70))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self._cycle(ctrl, mdp, 1002.0)   # idle: nothing new to book
        self.assertEqual(D(ctrl._state["owned_quote"]), D(70))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(D(self._persisted()["owned_quote"]), D(70))
        self.assertEqual(D(self._persisted()["owned_base"]), D("0.1"))


# ==================================================== CDX-M01: reseed commit discipline

class TestReseedCommitDiscipline(_Harness):

    def _reseed_ctrl(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, reseed_fund_from_wallet_once=True, reseed_generation=1,
                           ledger_overclaim_reanchor_seconds=100000)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=100)
        return ctrl, mdp, balances

    def test_reseed_save_failure_does_not_consume_token(self):
        ctrl, mdp, _ = self._reseed_ctrl()
        self._install_flaky_writer(ctrl, fail_on_calls={1})

        # Cycle 1: reseed save fails -> token NOT consumed, owned_* NOT rebaselined.
        self._cycle(ctrl, mdp, 1000.0)
        self.assertNotIn("last_reseed_token", ctrl._state)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(100))
        self.assertFalse(ctrl._reseed_just_applied)
        self.assertTrue(ctrl._accounting_degraded)

        # Cycle 2: writer works -> reseed applies exactly once (owned_quote -> min(200, 170)).
        self._cycle(ctrl, mdp, 1001.0)
        self.assertEqual("1:170", ctrl._state["last_reseed_token"])
        self.assertEqual(D(ctrl._state["owned_quote"]), D(170))
        self.assertEqual(D(self._persisted()["owned_quote"]), D(170))
        self.assertFalse(ctrl._accounting_degraded)


# ==================================================== CDX-M01: re-anchor commit discipline

class TestReanchorCommitDiscipline(_Harness):

    def test_reanchor_save_failure_leaves_owned_prior_then_retries(self):
        # A1 collapse trace under a failing save: owned=600, wallet 570, grace 10.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(570), D(570))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=600, owned_base=0, seed_value=600,
                         reserve_quote="400")
        self._install_flaky_writer(ctrl, fail_on_calls={1})

        self._cycle(ctrl, mdp, 1000.0)   # over-claim observed, grace arms (no save)
        self._cycle(ctrl, mdp, 1020.0)   # 20s > 10s grace -> cut attempt, save FAILS
        self.assertEqual(D(ctrl._state["owned_quote"]), D(600))   # NOT advanced to 570
        self.assertNotIn("reanchor_events", ctrl._state)          # metadata not applied either
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_ledger_reanchored"))
        self.assertTrue(ctrl._accounting_degraded)

        self._cycle(ctrl, mdp, 1021.0)   # grace already elapsed -> retry, save WORKS
        self.assertEqual(D(ctrl._state["owned_quote"]), D(570))
        self.assertEqual(1, len(ctrl._state["reanchor_events"]))
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(30))
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_ledger_reanchored")))
        self.assertFalse(ctrl._accounting_degraded)


# ==================================================== CDX-M01: degraded gates new orders

class TestAccountingDegradedGate(_Harness):

    def _sell_ladder_ctrl(self):
        # All-base wallet with sells above the ask -> a funded SELL ladder that WOULD place orders.
        balances = {"XMR": (D(1), D(1)), "USDT": (D("0.04"), D("0.04"))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base=1, seed_value=170)
        return ctrl, mdp

    def test_accounting_degraded_suppresses_new_orders(self):
        # Healthy controller: the scenario genuinely places a sell ladder.
        healthy, mdp_h = self._sell_ladder_ctrl()
        self._cycle(healthy, mdp_h, 1000.0)
        healthy_actions = healthy.create_actions_proposal()
        self.assertTrue(healthy_actions, "scenario must place orders when healthy")
        for a in healthy_actions:
            self.assertIsInstance(a, CreateExecutorAction)

        # Identical controller, but accounting-degraded: NO new orders proposed.
        degraded, mdp_d = self._sell_ladder_ctrl()
        self._cycle(degraded, mdp_d, 1000.0)
        degraded._accounting_degraded = True
        self.assertEqual([], degraded.create_actions_proposal())
        self.assertEqual(
            1, len(self._emit_events(degraded, "range_ladder_create_blocked_accounting_degraded"))
        )

    def test_degraded_clears_after_successful_save_and_orders_resume(self):
        # Drive the degrade->recover transition through the real booking commit, then confirm the
        # gate no longer blocks. owned matches wallet so booking is the only save site.
        balances = {"XMR": (D("0.1"), D("0.1")), "USDT": (D(70), D(70))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=100000)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=130)
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy", filled_base="0.1", filled_quote="30",
        )]
        self._install_flaky_writer(ctrl, fail_on_calls={1})

        self._cycle(ctrl, mdp, 1000.0)     # booking save fails -> degraded
        self.assertTrue(ctrl._accounting_degraded)
        self.assertEqual([], ctrl.create_actions_proposal())

        self._cycle(ctrl, mdp, 1001.0)     # booking save succeeds -> cleared
        self.assertFalse(ctrl._accounting_degraded)
        # Gate no longer blocks: a fresh create is NOT short-circuited by the degraded flag
        # (no new block event fires after recovery).
        ctrl._emit_structured.reset_mock()
        ctrl.create_actions_proposal()
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_create_blocked_accounting_degraded"))


# ==================================================== CDX-M01: _commit_state unit contract

class TestCommitStateHelper(_Harness):

    def _ctrl(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=50, owned_base=0, seed_value=50)
        return ctrl

    def test_commit_success_adopts_candidate_and_clears_degraded(self):
        ctrl = self._ctrl()
        ctrl._accounting_degraded = True   # pretend a prior failure
        ok = ctrl._commit_state({"owned_quote": "77"}, reason="unit")
        self.assertTrue(ok)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(77))
        self.assertEqual(D(self._persisted()["owned_quote"]), D(77))
        self.assertFalse(ctrl._accounting_degraded)

    def test_commit_failure_leaves_state_untouched(self):
        ctrl = self._ctrl()
        self._install_flaky_writer(ctrl, fail_on_calls={1})
        ok = ctrl._commit_state({"owned_quote": "999"}, reason="unit")
        self.assertFalse(ok)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(50))   # unchanged
        self.assertTrue(ctrl._accounting_degraded)
        self.assertEqual(ctrl._state_io_failures, 1)


# ==================================================== CLA-M03: load validation

class TestLoadValidation(_Harness):

    def _valid_state(self, **overrides):
        state = {
            "schema_version": 10,
            "controller_name": "range_inventory_ladder",
            "controller_type": "market_making",
            "controller_id": "ctrl-hbpurse-p2",
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
        state.update(overrides)
        return state

    def _write_state(self, state):
        with self._state_path.open("w", encoding="utf-8") as f:
            json.dump(state, f)

    def _fresh_ctrl(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        return ctrl

    def test_valid_progress_loads_untouched(self):
        self._write_state(self._valid_state(
            booked_fill_progress={"exec1": {"base": "0.5", "quote": "150", "fees": "0.1"}}))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_state_rejected"))
        self.assertTrue(ctrl._state.get("initialized"))
        # CDX-R06 discriminator: a VALID progress map must load byte-for-byte UNTOUCHED -- EVERY
        # sub-key (base AND quote AND fees), not just base. A validation pass that rewrites any
        # entry value (e.g. entry_val["fees"] = "999") is now caught by the full-mapping equality;
        # the old base-only assertion missed a corrupted quote/fee baseline, which would suppress
        # future fee deltas via the monotonic guard and over-state the fund.
        self.assertEqual(
            ctrl._state["booked_fill_progress"],
            {"exec1": {"base": "0.5", "quote": "150", "fees": "0.1"}},
        )

    def test_missing_progress_loads_and_is_treated_as_empty(self):
        # Missing/None progress must NOT quarantine and must behave as {} (the booking read
        # treats an absent key as empty). The key is left absent -- a pre-progress v10 file
        # round-trips byte-identically -- so assert the EFFECTIVE emptiness, not materialization.
        self._write_state(self._valid_state())   # no booked_fill_progress key
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_state_rejected"))
        self.assertTrue(ctrl._state.get("initialized"))
        self.assertEqual(ctrl._state.get("booked_fill_progress", {}), {})

    def test_corrupt_progress_entry_quarantines(self):
        # A non-numeric "base" would raise EVERY booking cycle (CLA-M03) -- refuse on load instead.
        self._write_state(self._valid_state(
            booked_fill_progress={"exec1": {"base": "garbage", "quote": "10", "fees": "0"}}))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)   # quarantined, not loaded
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_negative_progress_entry_quarantines(self):
        self._write_state(self._valid_state(
            booked_fill_progress={"exec1": {"base": "-0.5", "quote": "10", "fees": "0"}}))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_non_dict_progress_quarantines(self):
        self._write_state(self._valid_state(booked_fill_progress=["not", "a", "dict"]))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_pre_p1_state_without_reanchor_keys_loads(self):
        # Backward compat: a valid v10 file WITHOUT any P1 optional keys must load clean.
        state = self._valid_state()
        for k in ("reanchor_events", "reanchor_offset_quote", "reanchor_offset_base",
                  "reanchor_cut_quote_cum", "reanchor_cut_base_cum"):
            self.assertNotIn(k, state)
        self._write_state(state)
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_state_rejected"))
        self.assertTrue(ctrl._state.get("initialized"))

    def test_corrupt_reanchor_offset_quarantines(self):
        self._write_state(self._valid_state(reanchor_offset_quote="not-a-number"))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_negative_reanchor_cut_counter_quarantines(self):
        self._write_state(self._valid_state(reanchor_cut_quote_cum="-5"))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_non_list_reanchor_events_quarantines(self):
        self._write_state(self._valid_state(reanchor_events={"not": "a list"}))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    # ------ CDX-R03: malformed reanchor_events elements + future/negative offset timestamps ------

    def test_malformed_reanchor_event_element_quarantines(self):
        # CDX-R03: a reanchor_events LIST is not enough -- a malformed ELEMENT (a bare string) must
        # quarantine, else it loads clean and later crashes the reporting/journal read that iterates
        # events. The old `isinstance(..., list)`-only check accepted this.
        self._write_state(self._valid_state(reanchor_events=["not-an-event"]))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_nonfinite_reanchor_event_amount_quarantines(self):
        # A well-shaped event dict with a NON-FINITE numeric (NaN) must also quarantine.
        self._write_state(self._valid_state(reanchor_events=[
            {"ts": 1000.0, "old_owned_quote": "NaN", "new_owned_quote": "0",
             "old_owned_base": "0", "new_owned_base": "0", "overclaim_quote": "0",
             "wallet_quote_total": "0", "wallet_base_total": "0"}]))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_valid_reanchor_event_element_loads(self):
        # Regression: a well-formed event list (the shape _build_reanchor_mutations writes) must
        # still load clean.
        self._write_state(self._valid_state(reanchor_events=[
            {"ts": 1000.0, "old_owned_quote": "600", "new_owned_quote": "570",
             "old_owned_base": "0", "new_owned_base": "0", "overclaim_quote": "30",
             "wallet_quote_total": "570", "wallet_base_total": "0"}]))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_state_rejected"))
        self.assertTrue(ctrl._state.get("initialized"))

    def test_future_dated_offset_ts_quarantines(self):
        # CDX-R03: a far-future offset creation timestamp makes `now - ts` negative, so the expiry
        # test never fires and the credit becomes IMMORTAL -- it could absorb a genuine future debit
        # and OVER-STATE owned_*. Refuse it on load (harness mdp.time() == 1000).
        self._write_state(self._valid_state(
            reanchor_offset_quote="30", reanchor_offset_quote_ts="9999999999"))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_negative_offset_ts_quarantines(self):
        self._write_state(self._valid_state(
            reanchor_offset_base="5", reanchor_offset_base_ts="-1"))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_rejected")))

    def test_present_valid_offset_ts_loads(self):
        # Regression: a plausible (past, non-negative) offset ts must still load.
        self._write_state(self._valid_state(
            reanchor_offset_quote="30", reanchor_offset_quote_ts="990"))
        ctrl = self._fresh_ctrl()
        ctrl._load_state()
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_state_rejected"))
        self.assertTrue(ctrl._state.get("initialized"))


# ==================================================== F10: init Decimal hygiene

class TestInitDecimalHygiene(_Harness):

    def test_non_finite_init_balance_refuses_without_writing_state(self):
        # get_balance returns NaN for USDT -> self._d raises -> init REFUSES this cycle.
        balances = {"XMR": (D("1"), D("1")), "USDT": (float("nan"), float("nan"))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        ctrl._state = {}
        ctrl._state_loaded = True

        result = ctrl._ensure_initialized(Decimal("300"))
        self.assertFalse(result)
        self.assertNotIn("initialized", ctrl._state)     # no state constructed from garbage
        self.assertFalse(self._state_path.exists())       # nothing written
        self.assertEqual(
            1, len(self._emit_events(ctrl, "range_ladder_initialization_blocked_bad_balance"))
        )

    def test_finite_init_balance_still_initializes(self):
        # Regression guard: a normal (finite) balance initializes and writes state as before.
        balances = {"XMR": (D("1"), D("1")), "USDT": (D("200"), D("200"))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        ctrl._state = {}
        ctrl._state_loaded = True

        result = ctrl._ensure_initialized(Decimal("300"))
        self.assertTrue(result)
        self.assertTrue(ctrl._state.get("initialized"))
        self.assertTrue(self._state_path.exists())
        self.assertEqual(
            0, len(self._emit_events(ctrl, "range_ladder_initialization_blocked_bad_balance"))
        )


# ================================================ CDX-R01: first-init commit discipline

class TestInitCommitDiscipline(_Harness):

    def test_init_write_failure_refuses_then_retries(self):
        # CDX-R01: first-init CREATES every authoritative money field, so a failed initial save must
        # NOT leave initialized=True in memory with nothing on disk (which would trade from a
        # non-durable ledger and silently re-seed on restart). Instead: refuse, degrade, retry.
        balances = {"XMR": (D("1"), D("1")), "USDT": (D("200"), D("200"))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        ctrl._state = {}
        ctrl._state_loaded = True
        self._install_flaky_writer(ctrl, fail_on_calls={1})

        # Cycle 1: the initial state save fails -> init REFUSED, in-memory state NOT advanced to
        # initialized, nothing on disk, degraded raised, failure counted, commit-fail event emitted.
        # Reverting first-init to mutate-then-save (assign self._state before the write) leaves
        # initialized=True in memory here -> this test FAILS.
        self.assertFalse(ctrl._ensure_initialized(Decimal("300")))
        self.assertEqual({}, ctrl._state)
        self.assertNotIn("initialized", ctrl._state)
        self.assertFalse(self._state_path.exists())
        self.assertTrue(ctrl._accounting_degraded)
        self.assertEqual(ctrl._state_io_failures, 1)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_state_commit_failed")))

        # Cycle 2: the writer works -> init applies and persists exactly once, degraded clears.
        self.assertTrue(ctrl._ensure_initialized(Decimal("300")))
        self.assertTrue(ctrl._state.get("initialized"))
        self.assertTrue(self._state_path.exists())
        self.assertTrue(self._persisted().get("initialized"))
        self.assertFalse(ctrl._accounting_degraded)
        self.assertEqual(ctrl._state_io_failures, 1)


# =============================== CDX-R03: offset expiry defense-in-depth at consumption

class TestOffsetExpiryDefenseInDepth(_Harness):

    def test_load_reanchor_offsets_drops_future_ts(self):
        # CDX-R03 defense-in-depth: even if a future-stamped credit reaches the consumption site
        # (bypassing load validation), _load_reanchor_offsets must read it as ZERO (stale), never as
        # a live immortal credit. Without the future-age guard, `(now - ts) >= expiry` is false for a
        # future ts and the credit would survive.
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=100,
                         reanchor_offset_quote="30",
                         reanchor_offset_quote_ts=str(1000 + 10 * 86400))
        offset_quote, offset_base, cleared = ctrl._load_reanchor_offsets(1000.0)
        self.assertEqual(offset_quote, Decimal("0"))
        self.assertTrue(cleared)

    def test_load_reanchor_offsets_keeps_fresh_ts(self):
        # Regression: a fresh (recent, past) credit is preserved.
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, reanchor_offset_expiry_seconds=600)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=100,
                         reanchor_offset_quote="30", reanchor_offset_quote_ts="990")
        offset_quote, offset_base, cleared = ctrl._load_reanchor_offsets(1000.0)
        self.assertEqual(offset_quote, Decimal("30"))
        self.assertFalse(cleared)


# ==================================================== A2: quarantine backup-move failure

class TestQuarantineBackupFailure(_Harness):

    def test_backup_move_failure_emits_event_and_keeps_original(self):
        # A corrupt file triggers quarantine; the backup os.replace fails (e.g. a Windows lock).
        # The failure must be LOUDLY recorded AND the rejected bytes must survive the
        # re-initialization that FOLLOWS (which os.replace()s a fresh state onto state_path).
        rejected_bytes = "{ this is not valid json "
        with self._state_path.open("w", encoding="utf-8") as f:
            f.write(rejected_bytes)
        ctrl = self._build(_make_mdp(balances={"XMR": (D(1), D(1)), "USDT": (D(200), D(200))},
                                     mid=300, bid=299, ask=301))

        def failing_replace(src, dst):
            raise OSError("backup move blocked by a file lock")

        # Only the quarantine MOVE (os.replace) fails; the best-effort shutil.copy2 preserve and
        # the later re-init atomic write are real IO in the tmp dir.
        with patch.object(ril.os, "replace", side_effect=failing_replace):
            ctrl._load_state()

        self.assertEqual({}, ctrl._state)                 # re-init will follow
        self.assertTrue(self._state_path.exists())        # original NOT deleted/lost yet
        backup_path = ctrl._state_recovery_backup_path
        self.assertIsNotNone(backup_path)
        events = self._emit_events(ctrl, "range_ladder_state_backup_failed")
        self.assertEqual(1, len(events))
        self.assertIn("backup move blocked", events[0].kwargs["error"])
        # A2 + CDX-R04: the rejected bytes are PRESERVED at the backup path (copy fallback), so the
        # recovery init below cannot destroy the only copy.
        self.assertEqual("True", events[0].kwargs["preserved_backup"])
        self.assertEqual(Path(backup_path).read_text(encoding="utf-8"), rejected_bytes)

        # CDX-R07 discriminator: drive the recovery initialization, which os.replace()s a fresh
        # state onto state_path. The rejected ledger must STILL be recoverable at the backup path
        # afterward (dropping the copy-preserve loses it here), while state_path now holds the fresh
        # initialized state -- NOT the rejected bytes.
        self.assertTrue(ctrl._ensure_initialized(Decimal("300")))
        self.assertTrue(ctrl._state.get("initialized"))
        self.assertEqual(Path(backup_path).read_text(encoding="utf-8"), rejected_bytes)
        self.assertNotEqual(self._state_path.read_text(encoding="utf-8"), rejected_bytes)
        self.assertTrue(self._persisted().get("initialized"))

    def test_backup_move_success_still_moves_file(self):
        # Regression guard: when the move succeeds the original is relocated to the backup path.
        with self._state_path.open("w", encoding="utf-8") as f:
            f.write("{ still not valid json ")
        ctrl = self._build(_make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                                     mid=300, bid=299, ask=301))
        ctrl._load_state()
        self.assertEqual({}, ctrl._state)
        self.assertFalse(self._state_path.exists())        # moved away
        self.assertEqual([], self._emit_events(ctrl, "range_ladder_state_backup_failed"))


if __name__ == "__main__":
    unittest.main()
