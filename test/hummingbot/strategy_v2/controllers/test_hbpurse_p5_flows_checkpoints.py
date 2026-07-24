"""hbpurse Phase 5 behavior-contract tests: declared flows (generation-token, quiet-window
confirmation, drift on contradiction), wallet-observation checkpoints (interval + after every
flow/reseed/re-anchor event), the stop-path final booking pass, and carried-prune drift
surfacing.

Every test drives the REAL controller (`RangeInventoryLadderController`) with mocked balances,
prices, time() and executors_info, exactly like the P1-P4 suites. Purse IO is real (a temp dir);
no record's expected value is captured by RUNNING the code -- each is hand-derived from the PINNED
"Purse journal contract v1" (hbpurse_eng_batch_prompt.md) and the Phase-5 spec.

Discriminating assertions (named for the reviewer's TEST INTEGRITY AUDIT):
- Quiet window: test_declared_deposit_confirms_only_after_quiet_window asserts NO flow record
  exists after the arm cycle and the first in-window cycle, and exactly one after the quiet count
  is reached. Deleting the quiet-window gate (the `if self._flow_quiet_cycles < max(1, ...):
  return` in _maybe_confirm_declared_flow) confirms one cycle early -> the "no record yet" assert
  FAILS.
- Drift on contradiction: test_contradicting_wallet_move_records_drift asserts confirmation=
  "drift" when the wallet moved OPPOSITE the declared direction. Forcing
  confirmation="wallet_delta_matched" (matched = True) -> FAILS.
- owned_* invariance: test_flow_never_resizes_owned asserts owned_quote/owned_base are byte-equal
  across the flow confirmation. A flow path that added quote_valuation to owned_quote -> FAILS.
- Idempotency: test_same_token_records_once asserts exactly ONE flow record after re-running the
  same token many cycles. Dropping the last_flow_token guard -> a second record -> FAILS.
- Checkpoint after event: test_checkpoint_appended_after_flow_event asserts a checkpoint record
  follows the flow record in the SAME cycle. Removing the `_purse_checkpoint_due = True` arming in
  _purse_append_record for kind "flow" -> no checkpoint that cycle -> FAILS.
- Checkpoint on interval: test_checkpoint_appended_on_interval asserts a checkpoint appears once
  the interval elapses with no events. Disabling the interval branch (interval_due) -> FAILS.
- Carried-prune drift: test_carried_prune_produces_drift_record asserts a drift reanchor record
  carrying the pruned entry's cumulative base/quote. Reverting to today's silent delete (removing
  the _journal_carried_prune_drift call) -> no such record -> FAILS.
- Routine prune silent: test_same_session_completed_prune_is_silent asserts NO drift record and NO
  carried-prune event when a live-observed executor's entry is pruned. Misclassifying it as
  carried (dropping the `eid not in self._observed_progress_ids` guard) -> a spurious drift record
  -> FAILS.
- Stop-path booking: test_stop_path_books_trailing_fill asserts owned_* advances and a fills_rollup
  lands from a fill that arrived AFTER the last cycle, driven only by on_stop(). Making on_stop a
  no-op (skipping the stop-path booking pass) -> owned_* stale -> FAILS.
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

from controllers._shared.purse_ledger import PurseIOError  # noqa: E402
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
    """balances: {asset: (total, available)}. Mutate the dict between cycles; the side_effect
    lambdas read it live."""
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
    # NonKYC-style: no balance-settling flag and no external-order-holds getter -> both degrade
    # to the fail-closed defaults the controller already handles.
    connector.is_balance_settling = False
    connector.external_order_holds = None
    mdp.get_connector.return_value = connector
    return mdp


def _make_config(**overrides):
    defaults = dict(
        id="ctrl-hbpurse-p5",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("1000"),
        max_fund_value_quote=Decimal("100000"),
        buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
        buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
        sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        min_order_quote=Decimal("5"),
        cooldown_time=3600,
        event_refresh_enabled=False,
        fee_rate=Decimal("0"),
        # A very long over-claim grace so an incidental wallet dip never fires a re-anchor and
        # muddies the record stream (tests that want a re-anchor set it short explicitly).
        ledger_overclaim_reanchor_seconds=100000,
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


def _valid_state(controller_id, *, owned_quote="1000", owned_base="0", seed_value="1000",
                 init_ts="1000", **overrides):
    state = {
        "schema_version": 10,
        "controller_name": "range_inventory_ladder",
        "controller_type": "market_making",
        "controller_id": controller_id,
        "connector_name": "nonkyc",
        "trading_pair": "XMR-USDT",
        "base_asset": "XMR",
        "quote_asset": "USDT",
        "initialized": True,
        "reserve_quote_balance": "0",
        "reserve_base_balance": "0",
        "initial_managed_quote": str(owned_quote),
        "initial_claimed_base_amount": str(owned_base),
        "initial_reference_price": "300",
        "initialized_timestamp": init_ts,
        "owned_quote": str(owned_quote),
        "owned_base": str(owned_base),
        "seed_value_quote": str(seed_value),
        "tracked_fill_executor_ids": [],
    }
    state.update(overrides)
    return state


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"
        self._purse_path = Path(self._tmp.name) / "state.purse.json"

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

    def _init_state(self, ctrl, *, owned_quote="1000", owned_base="0", seed_value="1000",
                    init_ts="1000", **extra):
        ctrl._state = _valid_state(
            ctrl.config.id, owned_quote=str(owned_quote), owned_base=str(owned_base),
            seed_value=str(seed_value), init_ts=init_ts, **extra,
        )
        ctrl._state_loaded = True

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    @staticmethod
    def _emit_events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]

    def _persisted_state(self):
        with self._state_path.open() as f:
            return json.load(f)

    def _persisted_purse(self):
        with self._purse_path.open() as f:
            return json.load(f)

    def _records_of_kind(self, kind):
        return [r for r in self._persisted_purse()["records"] if r["kind"] == kind]


# ==================================================== declared flows

class TestDeclaredFlows(_Harness):

    def test_declared_deposit_confirms_only_after_quiet_window(self):
        # Declared quote deposit of 100, quiet window = 2 cycles. Arm cycle first, then the wallet
        # gains 100; the record appears only AFTER the quiet count is met -- not before.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=2,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        # Cycle 1 (t=1000): bootstrap + ARM (baseline wallet quote=1000). No flow record yet.
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual("1:deposit:quote:100", ctrl._state["flow_arm_token"])
        self.assertEqual(D("1000"), D(ctrl._state["flow_arm_wallet_quote"]))
        self.assertEqual([], self._records_of_kind("flow"))
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_flow_armed")))

        # Deposit lands after arm.
        balances["USDT"] = (D(1100), D(1100))

        # Cycle 2 (t=1001): first in-window quiet cycle -> still NO record (quiet=1 < 2).
        self._cycle(ctrl, mdp, 1001.0)
        self.assertEqual([], self._records_of_kind("flow"))

        # Cycle 3 (t=1002): quiet=2 -> confirm. Quote flow -> quote_valuation == native_amount.
        self._cycle(ctrl, mdp, 1002.0)
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))
        rec = flows[0]
        self.assertEqual("1:deposit:quote:100", rec["token"])
        self.assertEqual("deposit", rec["flow_kind"])
        self.assertEqual("USDT", rec["asset"])
        self.assertEqual(D("100"), D(rec["native_amount"]))
        self.assertEqual(D("100"), D(rec["quote_valuation"]))
        self.assertEqual(D("300"), D(rec["valuation_price"]))
        self.assertEqual("wallet_delta_matched", rec["confirmation"])
        self.assertEqual("1:deposit:quote:100", ctrl._state["last_flow_token"])
        self.assertIsNone(ctrl._state["flow_arm_token"])
        # Derived contributed: bootstrap current_equity_only (equity 1000) + deposit 100 = 1100.
        metrics = ctrl._purse.derived_metrics(
            reference_price=D(300), owned_quote=D(1000), owned_base=D(0)
        )
        self.assertEqual(D("1100"), metrics["contributed"])

    def test_base_flow_valued_at_reference_price(self):
        # Declared BASE deposit of 2 XMR at ref 300 -> quote_valuation = 2 * 300 = 600.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="base",
            flow_amount=Decimal("2"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        self._cycle(ctrl, mdp, 1000.0)          # arm (baseline XMR=0)
        balances["XMR"] = (D(2), D(2))          # deposit of 2 XMR lands
        self._cycle(ctrl, mdp, 1001.0)          # quiet=1 -> confirm
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))
        rec = flows[0]
        self.assertEqual("XMR", rec["asset"])
        self.assertEqual(D("2"), D(rec["native_amount"]))
        self.assertEqual(D("600"), D(rec["quote_valuation"]))
        self.assertEqual(D("300"), D(rec["valuation_price"]))
        self.assertEqual("wallet_delta_matched", rec["confirmation"])

    def test_same_token_records_once(self):
        # Idempotency: the same declaration left set for many cycles records exactly ONE flow.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)          # arm
        balances["USDT"] = (D(1100), D(1100))
        for i in range(1, 8):                   # confirm, then re-run the same token repeatedly
            self._cycle(ctrl, mdp, 1000.0 + i)
        self.assertEqual(1, len(self._records_of_kind("flow")))

    def test_contradicting_wallet_move_records_drift(self):
        # Declared a DEPOSIT, but the wallet DROPPED -> confirmation="drift", never force-matched.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)          # arm (baseline 1000)
        balances["USDT"] = (D(900), D(900))     # wallet went the WRONG way for a deposit
        self._cycle(ctrl, mdp, 1001.0)          # confirm -> drift
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))
        self.assertEqual("drift", flows[0]["confirmation"])
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_flow_recorded")))

    def test_flow_never_resizes_owned(self):
        # Safety rule 6: a flow record NEVER touches owned_* (deposit-exclusion invariant).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("250"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=5, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)
        owned_q_before = ctrl._state["owned_quote"]
        owned_b_before = ctrl._state["owned_base"]
        balances["USDT"] = (D(1250), D(1250))
        self._cycle(ctrl, mdp, 1001.0)
        self.assertEqual(1, len(self._records_of_kind("flow")))
        self.assertEqual(owned_q_before, ctrl._state["owned_quote"])
        self.assertEqual(owned_b_before, ctrl._state["owned_base"])
        # No reseed / re-anchor record was produced by the flow path.
        self.assertEqual([], self._records_of_kind("reseed_epoch"))
        self.assertEqual([], self._records_of_kind("reanchor"))

    def test_busy_ladder_defers_flow_and_warns(self):
        # A ladder that keeps booking fills never opens a quiet window: the flow stays armed and
        # the deferral is surfaced (a starving flow must be observable, not silently stuck).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=2,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)          # arm
        balances["USDT"] = (D(1100), D(1100))
        # Every cycle books a fresh BUY fill -> booked_this_cycle True -> quiet counter resets.
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "exec-live",
                               filled_base="0", filled_quote="0")
        ctrl.executors_info = [ex]
        for i in range(1, 5):
            ex.custom_info = {
                "filled_amount_base": D(str(i)), "filled_amount_quote": D(str(i)),
                "cum_fees_quote": D("0"),
            }
            self._cycle(ctrl, mdp, 1000.0 + i)
        self.assertEqual([], self._records_of_kind("flow"))
        self.assertEqual("1:deposit:quote:100", ctrl._state["flow_arm_token"])
        self.assertGreaterEqual(len(self._emit_events(ctrl, "range_ladder_flow_confirm_deferred")), 1)


# ==================================================== wallet checkpoints

class TestCheckpoints(_Harness):

    def test_checkpoint_appended_on_interval(self):
        # No events; a checkpoint appears once purse_checkpoint_interval_seconds elapses.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, purse_checkpoint_interval_seconds=100)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        self._cycle(ctrl, mdp, 1000.0)                    # bootstrap; clock starts, no checkpoint
        self.assertEqual([], self._records_of_kind("checkpoint"))
        self._cycle(ctrl, mdp, 1050.0)                    # 50s < 100s -> still none
        self.assertEqual([], self._records_of_kind("checkpoint"))
        self._cycle(ctrl, mdp, 1101.0)                    # 101s >= 100s -> checkpoint
        cps = self._records_of_kind("checkpoint")
        self.assertEqual(1, len(cps))
        cp = cps[0]
        self.assertEqual("epoch-1", cp["epoch_id"])
        self.assertEqual(D("1000"), D(cp["owned_quote"]))
        self.assertEqual(D("1000"), D(cp["wallet_quote_total"]))
        self.assertEqual(D("1000"), D(cp["equity_quote"]))   # 1000 + 0*300
        self.assertEqual(D("0"), D(cp["external_holds_quote"]))

    def test_checkpoint_appended_after_flow_event(self):
        # Contract: one checkpoint immediately after every flow/reseed/re-anchor append. Interval
        # disabled (0) so ONLY the event drives it.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, purse_checkpoint_interval_seconds=0, flow_generation=1, flow_kind="deposit",
            flow_asset="quote", flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)                    # arm; no flow, no checkpoint
        self.assertEqual([], self._records_of_kind("checkpoint"))
        balances["USDT"] = (D(1100), D(1100))
        self._cycle(ctrl, mdp, 1001.0)                    # flow confirms -> checkpoint follows
        self.assertEqual(1, len(self._records_of_kind("flow")))
        cps = self._records_of_kind("checkpoint")
        self.assertEqual(1, len(cps))
        # The checkpoint is appended AFTER the flow record (higher seq).
        flow_seq = self._records_of_kind("flow")[0]["seq"]
        self.assertGreater(cps[0]["seq"], flow_seq)
        self.assertEqual(D("1100"), D(cps[0]["wallet_quote_total"]))

    def test_checkpoint_follows_reanchor_event(self):
        # A re-anchor (owned > wallet) appends a reanchor record; a checkpoint must follow it.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(600), D(600))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, purse_checkpoint_interval_seconds=0,
            ledger_reconcile_threshold_quote=Decimal("10"),
            ledger_overclaim_reanchor_seconds=10,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)     # bootstrap; owned 1000 > wallet 600 arms over-claim
        self._cycle(ctrl, mdp, 1015.0)     # 15s > 10s grace -> re-anchor + checkpoint
        self.assertEqual(1, len(self._records_of_kind("reanchor")))
        self.assertEqual(1, len(self._records_of_kind("checkpoint")))


# ==================================================== carried-prune drift

class TestCarriedPruneDrift(_Harness):

    def test_carried_prune_produces_drift_record(self):
        # A booked_fill_progress entry loaded from disk, never seen live this session, is pruned on
        # the first post-resume booking cycle -> ONE drift reanchor record with its values.
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(
            ctrl, owned_quote=1000, owned_base="0.5", seed_value=1000,
            booked_fill_progress={"old-exec-1": {"base": "0.4", "quote": "120", "fees": "0.12"}},
        )
        ctrl.executors_info = []               # post-resume: prior executors are NOT re-attached
        self._cycle(ctrl, mdp, 1000.0)

        drifts = [r for r in self._records_of_kind("reanchor") if r["classification"] == "drift"]
        self.assertEqual(1, len(drifts))
        rec = drifts[0]
        self.assertEqual(D("120"), D(rec["old_owned_quote"]))   # pruned entry's cumulative quote
        self.assertEqual(D("0.4"), D(rec["old_owned_base"]))    # pruned entry's cumulative base
        self.assertEqual(D("0"), D(rec["new_owned_quote"]))
        self.assertEqual(D("0"), D(rec["new_owned_base"]))
        # drift magnitude = quote + base * ref = 120 + 0.4 * 300 = 240.
        self.assertEqual(D("240"), D(rec["overclaim_quote"]))
        # The progress entry is gone from state (pruned) but owned_* is UNCHANGED (never resized).
        self.assertNotIn("old-exec-1", ctrl._state.get("booked_fill_progress", {}))
        self.assertEqual(D("1000"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("0.5"), D(ctrl._state["owned_base"]))
        evts = self._emit_events(ctrl, "range_ladder_carried_prune_drift")
        self.assertEqual(1, len(evts))
        self.assertEqual(["old-exec-1"], evts[0].kwargs["pruned_ids"])
        # Derived drift reflects the surfaced loss.
        metrics = ctrl._purse.derived_metrics(
            reference_price=D(300), owned_quote=D(1000), owned_base=D("0.5")
        )
        self.assertEqual(D("240"), metrics["drift"])

    def test_same_session_completed_prune_is_silent(self):
        # An executor OBSERVED live this session, whose entry is pruned after it completes, is a
        # routine prune: no drift record, no carried-prune event (today's behavior preserved).
        balances = {"XMR": (D(2), D(2)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        # Cycle 1: a live BUY executor books a fill -> creates its own progress entry (observed).
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "new-exec-1",
                               filled_base="1", filled_quote="300", fees="0")
        ctrl.executors_info = [ex]
        self._cycle(ctrl, mdp, 1000.0)
        self.assertIn("new-exec-1", ctrl._state.get("booked_fill_progress", {}))

        # Cycle 2: the executor is gone -> its entry is pruned, but it was observed -> silent.
        ctrl.executors_info = []
        self._cycle(ctrl, mdp, 1001.0)
        self.assertNotIn("new-exec-1", ctrl._state.get("booked_fill_progress", {}))
        self.assertEqual([], [r for r in self._records_of_kind("reanchor")
                              if r["classification"] == "drift"])
        self.assertEqual(0, len(self._emit_events(ctrl, "range_ladder_carried_prune_drift")))

    def test_carried_entry_reobserved_live_then_pruned_is_silent(self):
        # A carried entry (LOADED from disk) that IS re-attached into executors_info this session
        # is no longer an orphan: its later prune is ROUTINE (silent). This isolates the
        # "never observed" half of the carried test -- misclassifying a re-observed carried entry
        # as drift (dropping the `eid not in self._observed_progress_ids` guard) would journal a
        # spurious drift reanchor here.
        balances = {"XMR": (D(1), D(1)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(
            ctrl, owned_quote=1000, owned_base=1, seed_value=1000,
            booked_fill_progress={"reattached-exec": {"base": "1", "quote": "300", "fees": "0"}},
        )
        # Cycle 1: the framework DID re-attach this executor (cumulative matches the carried entry,
        # so nothing new books) -> it is observed live this session.
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "reattached-exec",
                               filled_base="1", filled_quote="300", fees="0")
        ctrl.executors_info = [ex]
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual([], [r for r in self._records_of_kind("reanchor")
                              if r["classification"] == "drift"])

        # Cycle 2: the executor completes and leaves -> its entry is pruned, but it was observed
        # this session -> routine prune, no drift.
        ctrl.executors_info = []
        self._cycle(ctrl, mdp, 1001.0)
        self.assertNotIn("reattached-exec", ctrl._state.get("booked_fill_progress", {}))
        self.assertEqual([], [r for r in self._records_of_kind("reanchor")
                              if r["classification"] == "drift"])
        self.assertEqual(0, len(self._emit_events(ctrl, "range_ladder_carried_prune_drift")))


# ==================================================== CDX-R02: within-session uncommitted fill

class TestUncommittedFillDrift(_Harness):

    def test_within_session_uncommitted_fill_vanish_surfaces_drift(self):
        # CDX-R02 (deferred P2 -> P5): a fill whose booking commit FAILED, whose executor then
        # LEFT executors_info before a successful recompute-based retry, loses its exact delta.
        # The lost delta must be surfaced as drift, never dropped. (Mutation: skip the
        # _journal_uncommitted_fill_drift call, or drop `uncommitted_lost` from the commit
        # trigger -> no drift record on the vanish cycle -> FAILS.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)  # default long re-anchor grace -> no real re-anchor interferes
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        # Cycle 1: bootstrap; a live BUY executor, not yet filled.
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "exec-fail",
                               filled_base="0", filled_quote="0")
        ctrl.executors_info = [ex]
        self._cycle(ctrl, mdp, 1000.0)

        # A state-writer that fails while the gate is set; the BUY then fills. The booking commit
        # fails -> owned_* stays at 1000, the fill delta is captured as uncommitted.
        real_write = ctrl._write_state_to_disk
        gate = {"fail": True}

        def flaky(state):
            if gate["fail"]:
                raise OSError("simulated booking commit failure")
            return real_write(state)

        ctrl._write_state_to_disk = flaky
        ex.custom_info = {"filled_amount_base": D("1"), "filled_amount_quote": D("300"),
                          "cum_fees_quote": D("0")}
        balances["USDT"] = (D(700), D(700))
        self._cycle(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._accounting_degraded)
        self.assertEqual(D("1000"), D(ctrl._state["owned_quote"]))     # fill did NOT book
        self.assertIn("exec-fail", ctrl._uncommitted_fill_actuals)

        # Cycle 3: writer recovers, but the executor has VANISHED before the retry -> its lost
        # delta (base 1, quote 300; valued 300 + 1*300 = 600 at ref 300) is surfaced as drift.
        gate["fail"] = False
        ctrl.executors_info = []
        self._cycle(ctrl, mdp, 1020.0)
        self.assertFalse(ctrl._accounting_degraded)
        drifts = [r for r in self._records_of_kind("reanchor") if r["classification"] == "drift"]
        self.assertEqual(1, len(drifts))
        self.assertEqual(D("300"), D(drifts[0]["old_owned_quote"]))
        self.assertEqual(D("1"), D(drifts[0]["old_owned_base"]))
        self.assertEqual(D("0"), D(drifts[0]["new_owned_quote"]))
        self.assertEqual(D("600"), D(drifts[0]["overclaim_quote"]))
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_uncommitted_fill_drift")))
        self.assertEqual({}, ctrl._uncommitted_fill_actuals)
        self.assertEqual(D("1000"), D(ctrl._state["owned_quote"]))     # owned_* NOT resized

    def test_recovered_commit_before_vanish_books_normally_no_drift(self):
        # Control: if the executor is STILL present when the writer recovers, the recompute-based
        # retry books the delta normally and NO drift record is produced.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "exec-retry",
                               filled_base="0", filled_quote="0")
        ctrl.executors_info = [ex]
        self._cycle(ctrl, mdp, 1000.0)

        real_write = ctrl._write_state_to_disk
        gate = {"fail": True}

        def flaky(state):
            if gate["fail"]:
                raise OSError("simulated booking commit failure")
            return real_write(state)

        ctrl._write_state_to_disk = flaky
        ex.custom_info = {"filled_amount_base": D("1"), "filled_amount_quote": D("300"),
                          "cum_fees_quote": D("0")}
        balances["USDT"] = (D(700), D(700))
        self._cycle(ctrl, mdp, 1010.0)                    # commit fails -> degraded
        self.assertTrue(ctrl._accounting_degraded)

        gate["fail"] = False                              # writer recovers; executor STILL present
        self._cycle(ctrl, mdp, 1011.0)                    # recompute-based retry books it
        self.assertFalse(ctrl._accounting_degraded)
        self.assertEqual(D("700"), D(ctrl._state["owned_quote"]))   # 1000 - 300 booked
        self.assertEqual(D("1"), D(ctrl._state["owned_base"]))
        self.assertEqual([], [r for r in self._records_of_kind("reanchor")
                              if r["classification"] == "drift"])
        self.assertEqual({}, ctrl._uncommitted_fill_actuals)


# ==================================================== stop-path booking

class TestStopPathBooking(_Harness):

    def test_stop_path_books_trailing_fill(self):
        # A fill that arrives AFTER the last control cycle books on the stop path (on_stop) --
        # owned_* advances and a fills_rollup lands, driven only by on_stop().
        balances = {"XMR": (D(2), D(2)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        # Cycle 1: executor present but not yet filled -> nothing books.
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "exec-trail",
                               filled_base="0", filled_quote="0")
        ctrl.executors_info = [ex]
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D("1000"), D(ctrl._state["owned_quote"]))
        self.assertEqual([], self._records_of_kind("fills_rollup"))

        # A BUY fills 1 XMR @300 (fees 0) AFTER the last cycle; the loop has stopped.
        ex.custom_info = {
            "filled_amount_base": D("1"), "filled_amount_quote": D("300"), "cum_fees_quote": D("0"),
        }
        mdp.time.return_value = 1005.0
        ctrl.on_stop()

        # BUY books base +1, quote -300 -> owned (700, 1); the rollup records the same deltas.
        self.assertEqual(D("700"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("1"), D(ctrl._state["owned_base"]))
        rollups = self._records_of_kind("fills_rollup")
        self.assertEqual(1, len(rollups))
        self.assertEqual(D("1"), D(rollups[0]["base_delta_cum"]))
        self.assertEqual(D("-300"), D(rollups[0]["quote_delta_cum"]))
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_stop_book_pass_complete")))

    def test_stop_path_is_safe_without_fills(self):
        # on_stop with nothing new to book must not crash and must not fabricate records.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)
        ctrl.on_stop()
        self.assertEqual(D("1000"), D(ctrl._state["owned_quote"]))
        self.assertEqual([], self._records_of_kind("fills_rollup"))


# ==================================================== status / custom_info exposure

class TestPurseBlockExposure(_Harness):

    def test_custom_info_exposes_flow_and_checkpoint_markers(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, purse_checkpoint_interval_seconds=100, flow_generation=1, flow_kind="deposit",
            flow_asset="quote", flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)                    # arm
        balances["USDT"] = (D(1100), D(1100))
        self._cycle(ctrl, mdp, 1001.0)                    # confirm
        info = ctrl.get_custom_info()
        purse = info["purse"]
        self.assertEqual("1:deposit:quote:100", purse["last_flow_token"])
        self.assertEqual("", purse["armed_flow_token"])
        self.assertEqual(1, purse["flow_generation"])
        self.assertIn("checkpoint_age_s", purse)
        self.assertIn("carried_prune_drift_quote", purse)


# ============================ CDX-R09: declared WITHDRAWALS (matched / contradicted / base)

class TestDeclaredWithdrawals(_Harness):

    def test_quote_withdrawal_confirms_matched(self):
        # Declared quote withdrawal of 100: the wallet DROPS by 100 in the declared direction ->
        # confirmation="wallet_delta_matched", withdrawn metric = 100, owned_* untouched.
        # (Mutation CDX-R09a: reverse the withdrawal comparison at _maybe_confirm_declared_flow's
        #  `matched = observed_delta <= -min_move` -> observed -100 no longer matches -> the
        #  confirmation flips to "drift" -> this assert FAILS.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="withdrawal", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # arm (baseline USDT=1000)
        balances["USDT"] = (D(900), D(900))         # withdrawal of 100 lands
        self._cycle(ctrl, mdp, 1001.0)              # confirm
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))
        self.assertEqual("withdrawal", flows[0]["flow_kind"])
        self.assertEqual("USDT", flows[0]["asset"])
        self.assertEqual(D("100"), D(flows[0]["quote_valuation"]))
        self.assertEqual("wallet_delta_matched", flows[0]["confirmation"])
        # owned_* is never resized by a flow (deposit/withdrawal-exclusion, safety rule 6).
        self.assertEqual(D("1000"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("0"), D(ctrl._state["owned_base"]))
        metrics = ctrl._purse.derived_metrics(
            reference_price=D(300), owned_quote=D(1000), owned_base=D(0)
        )
        self.assertEqual(D("100"), metrics["withdrawn"])

    def test_quote_withdrawal_contradiction_records_drift(self):
        # Declared a withdrawal but the wallet ROSE -> confirmation="drift", never force-matched.
        # (Also independently fails under the CDX-R09a reversed-comparison mutation.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="withdrawal", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # arm (baseline 1000)
        balances["USDT"] = (D(1100), D(1100))       # wallet went the WRONG way for a withdrawal
        self._cycle(ctrl, mdp, 1001.0)              # confirm -> drift
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))
        self.assertEqual("drift", flows[0]["confirmation"])

    def test_base_withdrawal_valued_at_reference_price(self):
        # Declared BASE withdrawal of 2 XMR at ref 300 -> quote_valuation = 2*300 = 600, matched
        # when the base wallet drops by 2.
        balances = {"XMR": (D(2), D(2)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="withdrawal", flow_asset="base",
            flow_amount=Decimal("2"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=2, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # arm (baseline XMR=2)
        balances["XMR"] = (D(0), D(0))              # withdrawal of 2 XMR lands
        self._cycle(ctrl, mdp, 1001.0)              # confirm
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))
        self.assertEqual("XMR", flows[0]["asset"])
        self.assertEqual(D("2"), D(flows[0]["native_amount"]))
        self.assertEqual(D("600"), D(flows[0]["quote_valuation"]))
        self.assertEqual("wallet_delta_matched", flows[0]["confirmation"])
        metrics = ctrl._purse.derived_metrics(
            reference_price=D(300), owned_quote=D(1000), owned_base=D(2)
        )
        self.assertEqual(D("600"), metrics["withdrawn"])


# ==================== CDX-R09/R01: restart idempotency & crash-boundary flow durability

class TestFlowRestartDurability(_Harness):

    def test_same_token_does_not_record_twice_across_restart(self):
        # A confirmed flow's terminal marker (last_flow_token) is persisted; a RESTARTED controller
        # loads it and does NOT re-arm or re-record the same declaration. (Mutation CDX-R09b: on
        # load, `token_val = validated.pop(token_key, None)` drops the token -> the fresh controller
        # forgets it -> re-arms and records a 2nd flow -> the "exactly one" assert FAILS.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # arm
        balances["USDT"] = (D(1100), D(1100))
        self._cycle(ctrl, mdp, 1001.0)              # confirm -> 1 record, last_flow_token set
        self.assertEqual(1, len(self._records_of_kind("flow")))
        self.assertEqual("1:deposit:quote:100", self._persisted_state()["last_flow_token"])

        # RESTART: a fresh controller loads state + purse from disk (no _init_state). The persisted
        # last_flow_token must make it a no-op -- it must NOT re-arm the already-recorded flow.
        ctrl2 = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._cycle(ctrl2, mdp, 1002.0)             # would re-arm under the mutation
        self._cycle(ctrl2, mdp, 1003.0)
        self.assertEqual(1, len(self._records_of_kind("flow")))  # still exactly one
        self.assertEqual("", ctrl2._state.get("flow_arm_token") or "")
        # The restored token suppresses re-arming entirely. Under the CDX-R09b mutation the fresh
        # controller forgets last_flow_token and re-arms the declaration -> a flow_armed event fires
        # -> this assert FAILS (the purse-level has_flow_token guard still blocks the duplicate
        # RECORD, so the re-arm event is the observable proof the token was lost on load).
        self.assertEqual(0, len(self._emit_events(ctrl2, "range_ladder_flow_armed")))

    def test_flow_survives_crash_between_state_commit_and_purse_save(self):
        # CDX-R01: the terminal token must not outlive its purse record. Confirm a flow while the
        # PURSE save fails -> the flow record is NOT durable and last_flow_token is NOT consumed
        # (pending_flow_record persists). A restart REPLAYS the record from state -> the operator-
        # declared flow is recovered, never dropped. (Old behavior consumed the token + buffered
        # the record in memory -> the crash lost the flow while suppressing all future retries.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=7, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # arm + bootstrap purse (opening on disk)
        balances["USDT"] = (D(1100), D(1100))
        # Make the purse save fail during the confirm cycle (the state save still succeeds).
        ctrl._purse.save = MagicMock(side_effect=PurseIOError("simulated purse save failure"))
        self._cycle(ctrl, mdp, 1001.0)              # confirm -> pending persisted, purse degraded
        token = "7:deposit:quote:100"
        self.assertEqual(token, self._persisted_state()["pending_flow_record"]["token"])
        self.assertNotEqual(token, self._persisted_state().get("last_flow_token"))
        self.assertEqual([], self._records_of_kind("flow"))   # never reached disk
        self.assertTrue(ctrl._purse_degraded)

        # RESTART with a healthy purse: reconcile replays the pending record and finalizes it.
        ctrl2 = self._build(
            mdp, flow_generation=7, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._cycle(ctrl2, mdp, 1002.0)
        flows = self._records_of_kind("flow")
        self.assertEqual(1, len(flows))                       # RECOVERED, not lost
        self.assertEqual(token, flows[0]["token"])
        self.assertEqual(token, self._persisted_state()["last_flow_token"])
        self.assertIsNone(self._persisted_state().get("pending_flow_record"))


# ==================================== CDX-R03: fail-closed flow gate while ledger is degraded

class TestFlowDegradedGate(_Harness):

    def test_flow_does_not_clear_degraded_while_uncommitted_fill_pending(self):
        # A booking commit fails (accounting degraded + an uncommitted fill pending). In the SAME
        # cycle the armed flow's confirmation must NOT commit and clear the degradation -- doing so
        # would re-open new-order proposals with a stale ledger. (Mutation CDX-R03: drop the
        # `if self._accounting_degraded or self._uncommitted_fill_actuals: return` gate -> the flow
        # commit succeeds on the second write and clears _accounting_degraded -> assertTrue FAILS.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp, flow_generation=1, flow_kind="deposit", flow_asset="quote",
            flow_amount=Decimal("100"), flow_confirm_quiet_cycles=1,
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # arm (no executors -> clean commit)

        # A flaky writer that fails the FIRST write of the next cycle (the booking commit) and
        # succeeds afterwards (the flow commit the mutation would let through).
        real_write = ctrl._write_state_to_disk
        calls = {"n": 0}

        def flaky(state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated booking commit failure")
            return real_write(state)

        ctrl._write_state_to_disk = flaky
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "exec-fail",
                               filled_base="1", filled_quote="300", fees="0")
        ctrl.executors_info = [ex]
        balances["USDT"] = (D(700), D(700))         # BUY spent 300
        self._cycle(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._accounting_degraded)              # booking failure stands
        self.assertIn("exec-fail", ctrl._uncommitted_fill_actuals)
        self.assertEqual([], self._records_of_kind("flow"))     # flow gated -> not recorded


# ==================================== CDX-R04: strict wallet reads for permanent records

class TestStrictWalletReads(_Harness):

    def test_checkpoint_defers_on_unreadable_wallet_no_fabricated_zero(self):
        # An interval checkpoint is due but the wallet read FAILS -> the checkpoint DEFERS (no
        # record, a structured deferral event) rather than journaling a fabricated wallet_total=0,
        # which would corrupt the ledger-reconciliation drift. (Mutation CDX-R04: read via
        # _safe_get_balance -> a checkpoint with wallet_quote_total="0" is written -> the "no
        # checkpoint" assert FAILS.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, purse_checkpoint_interval_seconds=100)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)              # bootstrap; checkpoint clock starts

        def _raise(_conn, _asset):
            raise RuntimeError("wallet momentarily unreadable")

        mdp.get_balance.side_effect = _raise
        self._cycle(ctrl, mdp, 1101.0)              # interval due, but wallet unreadable -> defer
        self.assertEqual([], self._records_of_kind("checkpoint"))   # no fabricated-zero checkpoint
        self.assertGreaterEqual(
            len(self._emit_events(ctrl, "range_ladder_checkpoint_wallet_unreadable")), 1)


# ==================================== CDX-R06: fee-only uncommitted loss surfaces as drift

class TestFeeOnlyUncommittedDrift(_Harness):

    def test_fee_only_uncommitted_loss_surfaces_drift(self):
        # A durable fill (base 1, quote 300, fees 0), then a FEE-ONLY update (fees +1, base/quote
        # flat) whose commit FAILS; the executor then vanishes. The unbooked fee is a quote debit
        # and must be surfaced as drift, not dropped. (Mutation CDX-R06: retention condition
        # `lost_base>0 or lost_quote>0` (no lost_fees term) -> the fee-only loss is discarded ->
        # no drift record -> FAILS.)
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        ex = _filling_executor("buy_315", TradeType.BUY, 315, "exec-fee",
                               filled_base="1", filled_quote="300", fees="0")
        ctrl.executors_info = [ex]
        balances["USDT"] = (D(700), D(700))
        self._cycle(ctrl, mdp, 1000.0)              # books base 1, quote 300, fees 0 (durable)
        self.assertEqual(D("700"), D(ctrl._state["owned_quote"]))

        # A fee-only update whose booking commit fails.
        real_write = ctrl._write_state_to_disk
        gate = {"fail": True}

        def flaky(state):
            if gate["fail"]:
                raise OSError("simulated fee booking commit failure")
            return real_write(state)

        ctrl._write_state_to_disk = flaky
        ex.custom_info = {"filled_amount_base": D("1"), "filled_amount_quote": D("300"),
                          "cum_fees_quote": D("1")}
        self._cycle(ctrl, mdp, 1010.0)
        self.assertTrue(ctrl._accounting_degraded)
        self.assertIn("exec-fee", ctrl._uncommitted_fill_actuals)

        # Recover the writer, executor vanishes -> the lost fee (1 quote) surfaces as drift.
        gate["fail"] = False
        ctrl.executors_info = []
        self._cycle(ctrl, mdp, 1020.0)
        drifts = [r for r in self._records_of_kind("reanchor") if r["classification"] == "drift"]
        self.assertEqual(1, len(drifts))
        self.assertEqual(D("1"), D(drifts[0]["old_owned_quote"]))   # the unbooked fee (folded)
        self.assertEqual(D("0"), D(drifts[0]["old_owned_base"]))
        self.assertEqual(D("1"), D(drifts[0]["overclaim_quote"]))   # 1 quote at ref
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_uncommitted_fill_drift")))


# ==================================== CDX-R07/R10: restart-accurate reporting markers

class TestRestartMarkers(_Harness):

    def test_checkpoint_age_and_carried_prune_drift_exact_and_survive_restart(self):
        # checkpoint_age_s reflects the ACTUAL last checkpoint ts and carried_prune_drift_quote the
        # ACTUAL surfaced total -- both exact, and both survive a restart. (Mutations CDX-R10:
        # force checkpoint_age to None / carried total to Decimal("0") -> the exact asserts FAIL.
        # Mutation CDX-R07: reset the markers from constants on restart -> the post-restart asserts
        # FAIL.)
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(
            ctrl, owned_quote=1000, owned_base="0.5", seed_value=1000,
            booked_fill_progress={"old-exec-1": {"base": "0.4", "quote": "120", "fees": "0.12"}},
        )
        ctrl.executors_info = []
        self._cycle(ctrl, mdp, 1000.0)              # carried prune -> drift 240 + a checkpoint @1000
        self.assertEqual(1, len(self._records_of_kind("checkpoint")))

        # Exact markers at a later time in the SAME session (the purse block rides processed_data,
        # so re-run a cycle at t=1500 to recompute it -- no new checkpoint: interval 3600 not due).
        self._cycle(ctrl, mdp, 1500.0)
        self.assertEqual(1, len(self._records_of_kind("checkpoint")))   # still just the event one
        purse = ctrl.get_custom_info()["purse"]
        self.assertEqual(500.0, purse["checkpoint_age_s"])          # 1500 - 1000
        self.assertEqual(D("240"), D(str(purse["carried_prune_drift_quote"])))
        self.assertEqual(D("240"),
                         D(self._persisted_state()["carried_prune_drift_quote_cum"]))

        # RESTART: a fresh controller loads the persisted state + purse; the markers are seeded
        # from history, not reset to constants.
        ctrl2 = self._build(mdp)
        self._cycle(ctrl2, mdp, 2000.0)             # adopt purse (seed checkpoint ts + carried cum)
        purse2 = ctrl2.get_custom_info()["purse"]
        self.assertEqual(1000.0, purse2["checkpoint_age_s"])       # 2000 - persisted 1000
        self.assertEqual(D("240"), D(str(purse2["carried_prune_drift_quote"])))


if __name__ == "__main__":
    unittest.main()
