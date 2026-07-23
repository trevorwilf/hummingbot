"""hbpurse Phase 4 behavior-contract tests: the append-only purse journal (contract v1) --
bootstrap (declared/undeclared basis), the scripted opening->fills->reseed->re-anchor->fills
derived-metrics arithmetic, quarantine independence, the fail-closed missing/degraded rules,
pending-delta retry, journal validation, and custom_info backward compatibility.

Every test drives the REAL controller (`RangeInventoryLadderController`) with mocked balances,
prices, time() and executors_info, exactly like the P1/P2/P3 suites. Purse IO failure is
simulated by patching PurseLedger.save on the live instance -- never by real disk manipulation.
Every expected value is derived BY HAND from the PINNED "Purse journal contract v1" formulas
(hbpurse_eng_batch_prompt.md) and the booking spec -- never from running the implementation.

The discriminating assertions (named for the reviewer's TEST INTEGRITY AUDIT):
- Derived metrics: the scripted-sequence expectations are hand-computed from the contract
  formulas (contributed=900, earned_realized=59.483, earned_total=-145.062, drift=50 at
  ref=310). Dropping the fees term from the rollup update (quote_delta -= d_quote instead of
  d_quote + d_fees on the BUY leg) shifts epoch-1 quote_delta_cum from -145.455 to -145.0 ->
  test_scripted_sequence_derived_metrics FAILS.
- Quarantine independence: test_state_quarantine_leaves_purse_untouched asserts the purse file
  still exists at purse_path with the ORIGINAL opening record byte-identical after the state
  quarantine. If _quarantine_state_file also renamed/moved the purse file, purse_path would be
  absent (or re-bootstrapped fresh) -> the prefix assertion FAILS.
- Fail-closed missing purse: test_post_bootstrap_missing_purse asserts degraded + create []
  + the file is NOT recreated. Silently re-bootstrapping (calling create() when the marker is
  set) recreates the file -> FAILS.
- Pending-delta retry: test_purse_save_failure asserts the rollup lands on disk on the NEXT
  cycle after a failed save. Dropping the in-memory retained delta (re-creating the doc or
  clearing dirty on failure) leaves the disk journal without the rollup -> FAILS.
- Bootstrap idempotency: test_marker_crash_retry asserts exactly ONE opening_epoch after a
  crashed marker commit + restart. Removing the note init_ts stamp match appends a duplicate
  epoch -> FAILS.

Adjudication additions (CDX-R01..R07, each verified by running the named mutation):
- R01 pending-gate: test_bootstrap_pending_connector_not_ready_blocks_creates FAILS if the
  purse-pending create gate reverts to only `if self._accounting_degraded or
  self._purse_degraded:` (verified: [] != [CreateExecutorAction...]).
- R02 strict reads: test_bootstrap_wallet_read_failure_defers... FAILS if any opening wallet
  read reverts to the zero-swallowing _safe_get_balance (verified on the quote-total line).
- R03 start-empty: test_empty_journal_rejected FAILS if the first-record check reverts to
  `if records and ...` (verified: PurseIntegrityError not raised).
- R04 marker retry: test_marker_transient_failure_recovers_same_process FAILS if the backstop
  guard reverts to skip on _accounting_degraded (verified: KeyError purse_initialized).
- R05 rollup strictness: TestRollupUpdateStrictness FAILS (14 subtests) under the original
  unvalidated in-place update body (verified: NaN/Infinity accepted silently).
- R06 contract seq: test_seq_gap_accepted_per_contract FAILS if contiguity is re-imposed
  (verified with seq != prev+1); the old test_seq_gap_rejected enforced contiguity BEYOND the
  pinned "1-based, strictly increasing" contract and was replaced.
- R07 value compat: the compat test compares EVERY pre-P4 key against the pre-P4 build's
  output (captured at merge-base ad5eceee for this exact scenario); breaking any single value
  (verified with reference_price -> "BROKEN") now FAILS where the old key-presence check
  stayed green.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from controllers._shared.purse_ledger import (  # noqa: E402
    PurseIntegrityError,
    PurseIOError,
    PurseLedger,
)
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
    mdp.get_connector.return_value = connector
    return mdp


def _make_config(**overrides):
    defaults = dict(
        id="ctrl-hbpurse-p4",
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


def _valid_state(controller_id, *, owned_quote="100", owned_base="0", seed_value="100",
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

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value, init_ts="1000", **extra):
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


# ==================================================== bootstrap: opening basis

class TestBootstrap(_Harness):

    def test_bootstrap_undeclared_records_current_equity_only(self):
        # Undeclared basis: contributed = CURRENT equity = owned_quote + owned_base * ref
        # = 1000 + 2 * 300 = 1600 (contract: pre-purse history must not be fabricated).
        balances = {"XMR": (D(3), D(3)), "USDT": (D(1100), D(1100))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=100000)
        self._init_state(ctrl, owned_quote=1000, owned_base=2, seed_value=1600)
        self._cycle(ctrl, mdp, 1000.0)

        doc = self._persisted_purse()
        self.assertEqual(1, doc["purse_schema_version"])
        self.assertEqual(ctrl.config.id, doc["controller_id"])
        self.assertEqual("range_inventory_ladder", doc["controller_name"])
        self.assertEqual("XMR-USDT", doc["trading_pair"])
        self.assertEqual(1, doc["sequence"])
        self.assertEqual(1, len(doc["records"]))
        rec = doc["records"][0]
        self.assertEqual("opening_epoch", rec["kind"])
        self.assertEqual(1, rec["seq"])
        self.assertEqual("epoch-1", rec["epoch_id"])
        self.assertEqual(D("1600"), D(rec["contributed_opening_quote"]))
        self.assertEqual(D("0"), D(rec["earned_opening_quote"]))
        self.assertEqual("current_equity_only", rec["opening_basis_quality"])
        self.assertIsNone(rec["predecessor"])
        self.assertTrue(rec["note"].startswith("init_ts=1000"))
        self.assertEqual(D("1000"), D(rec["owned_quote"]))
        self.assertEqual(D("2"), D(rec["owned_base"]))
        self.assertEqual(D("1600"), D(rec["seed_value_quote"]))
        self.assertEqual(D("300"), D(rec["reference_price"]))
        self.assertEqual(D("1100"), D(rec["wallet_quote_total"]))
        self.assertEqual(D("3"), D(rec["wallet_base_total"]))
        self.assertEqual(D("0"), D(rec["unavailable_quote"]))
        self.assertEqual(D("0"), D(rec["unavailable_base"]))
        # The bridge marker committed (end-of-cycle backstop; OPTIONAL v10 key, no schema bump).
        self.assertIs(True, self._persisted_state()["purse_initialized"])
        self.assertEqual(10, self._persisted_state()["schema_version"])
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_purse_bootstrapped")))

    def test_bootstrap_declared_records_reconstructed(self):
        # Declared basis (synthetic figures, deliberately NOT the A6 production numbers).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp,
            purse_opening_contributed_quote=Decimal("1234.56"),
            purse_opening_earned_quote=Decimal("-12.34"),
            purse_opening_note="unit-test opening",
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)

        rec = self._persisted_purse()["records"][0]
        self.assertEqual("reconstructed", rec["opening_basis_quality"])
        self.assertEqual(D("1234.56"), D(rec["contributed_opening_quote"]))
        self.assertEqual(D("-12.34"), D(rec["earned_opening_quote"]))
        self.assertEqual("init_ts=1000; unit-test opening", rec["note"])

    def test_bootstrap_earned_without_contributed_is_ignored(self):
        # An earned figure without the contributed anchor is not a usable basis: the bootstrap
        # falls back to current_equity_only and warns.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, purse_opening_earned_quote=Decimal("50"))
        self._init_state(ctrl, owned_quote=800, owned_base=0, seed_value=800)
        self._cycle(ctrl, mdp, 1000.0)

        rec = self._persisted_purse()["records"][0]
        self.assertEqual("current_equity_only", rec["opening_basis_quality"])
        self.assertEqual(D("800"), D(rec["contributed_opening_quote"]))   # equity, not fabricated
        self.assertEqual(D("0"), D(rec["earned_opening_quote"]))
        self.assertEqual(
            1, len(self._emit_events(ctrl, "range_ladder_purse_opening_declaration_incomplete"))
        )

    def test_declarations_after_bootstrap_are_ignored_with_warning(self):
        # Consumed at bootstrap only: an existing journal + set declarations -> warn, no change.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)
        self._cycle(ctrl, mdp, 1000.0)
        original_records = self._persisted_purse()["records"]

        # "Restart" with declarations now set: adopted journal must be unchanged.
        ctrl2 = self._build(mdp, purse_opening_contributed_quote=Decimal("999"))
        self._cycle(ctrl2, mdp, 2000.0)
        self.assertEqual(original_records, self._persisted_purse()["records"])
        self.assertEqual(
            1, len(self._emit_events(ctrl2, "range_ladder_purse_opening_declaration_ignored"))
        )


# ==================================================== the scripted contract-arithmetic test

class TestScriptedSequenceDerivedMetrics(_Harness):

    def test_scripted_sequence_derived_metrics_match_contract_arithmetic(self):
        """opening -> fills -> reseed -> re-anchor -> fills, every expected value hand-derived
        from the PINNED contract formulas (never from running the code):

        Opening (declared): contributed=900, earned_opening=50 (reconstructed).
        Epoch-1 fills:  BUY  d_base=1,   d_quote=300, fees=0.3  -> quote_delta -300.3
                        SELL d_base=0.5, d_quote=155, fees=0.155 -> quote_delta +154.845
                        epoch-1 rollup: base_delta_cum = +0.5, quote_delta_cum = -145.455,
                        fees = 0.455, fills_seen = 2; owned -> (854.545, 0.5).
        Reseed (target 800, claimed_base 0.5 @300): owned (854.545, 0.5) -> (650, 0.5),
                        seed 1000 -> 800, epoch-3 opens.
        Re-anchor: wallet quote drops to 600 -> cut 650 -> 600 (cut 50 > min_order_quote=5
                        -> classification undeclared_outflow); drift += 50.
        Epoch-3 fill:  SELL d_base=0.2, d_quote=62, fees=0.062 -> rollup base -0.2,
                        quote +61.938, fees 0.062; owned -> (661.938, 0.3).
        Metrics at ref=310 (contract formulas):
            contributed     = 900,  withdrawn = 0
            earned_realized = 50 + [(-145.455 + 0.5*310)] + [(61.938 + (-0.2)*310)]
                            = 50 + 9.545 - 0.062 = 59.483
            equity          = 661.938 + 0.3*310 = 754.938
            earned_total    = 754.938 - 900 + 0 = -145.062
            unrealized      = -145.062 - 59.483 = -204.545
            drift           = 50 (quote cut) + 0*310 = 50
        """
        balances = {"XMR": (D(2), D(2)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(
            mdp,
            total_amount_quote=Decimal("1000"),
            reseed_fund_from_wallet_once=True,
            reseed_generation=1,
            reseed_fund_target_quote=Decimal("800"),
            use_wallet_balance=True,   # the reseed base sleeve requires the wallet-claim flag
            claimed_base_amount=Decimal("0.5"),
            ledger_reconcile_threshold_quote=Decimal("10"),
            ledger_overclaim_reanchor_seconds=10,
            purse_opening_contributed_quote=Decimal("900"),
            purse_opening_earned_quote=Decimal("50"),
            purse_opening_note="scripted opening",
        )
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        # t=1000: opening epoch. An active zero-fill executor defers the reseed (flat-book gate).
        exec_a = _filling_executor("buy_300", TradeType.BUY, 300, "exec-a",
                                   filled_base="0", filled_quote="0")
        ctrl.executors_info = [exec_a]
        self._cycle(ctrl, mdp, 1000.0)
        opening = self._persisted_purse()["records"][0]
        self.assertEqual("opening_epoch", opening["kind"])
        self.assertEqual("reconstructed", opening["opening_basis_quality"])
        self.assertEqual(D("900"), D(opening["contributed_opening_quote"]))
        self.assertEqual(D("50"), D(opening["earned_opening_quote"]))

        # t=1010: BUY fill books (1 XMR @300, fees 0.3) -> owned (699.7, 1).
        exec_a.custom_info = {
            "filled_amount_base": D("1"), "filled_amount_quote": D("300"),
            "cum_fees_quote": D("0.3"),
        }
        balances["USDT"] = (D("700"), D("700"))
        balances["XMR"] = (D(2), D(2))
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(D("699.7"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("1"), D(ctrl._state["owned_base"]))

        # t=1011: SELL fill books (0.5 XMR @310-ish, quote 155, fees 0.155) -> owned (854.545, 0.5).
        exec_b = _filling_executor("sell_310", TradeType.SELL, 310, "exec-b",
                                   filled_base="0.5", filled_quote="155", fees="0.155")
        ctrl.executors_info = [exec_a, exec_b]
        balances["USDT"] = (D("854.545"), D("854.545"))
        balances["XMR"] = (D("1.5"), D("1.5"))
        self._cycle(ctrl, mdp, 1011.0)
        self.assertEqual(D("854.545"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("0.5"), D(ctrl._state["owned_base"]))
        rollup1 = self._persisted_purse()["records"][1]
        self.assertEqual("fills_rollup", rollup1["kind"])
        self.assertEqual("epoch-1", rollup1["epoch_id"])
        self.assertEqual(D("0.5"), D(rollup1["base_delta_cum"]))
        self.assertEqual(D("-145.455"), D(rollup1["quote_delta_cum"]))
        self.assertEqual(D("0.455"), D(rollup1["fees_quote_cum"]))
        self.assertEqual(2, rollup1["fills_seen"])

        # t=1012: book goes flat (reseed still deferred: within the fill-settle grace window).
        ctrl.executors_info = []
        self._cycle(ctrl, mdp, 1012.0)
        self.assertNotIn("last_reseed_token", ctrl._state)
        pre_reseed_records = self._persisted_purse()["records"]
        self.assertEqual(2, len(pre_reseed_records))

        # t=1200: grace elapsed -> reseed applies; epoch-3 opens; prior records survive
        # byte-for-byte (append-only: nothing compacted, edited or removed).
        self._cycle(ctrl, mdp, 1200.0)
        self.assertEqual("1:800", ctrl._state["last_reseed_token"])
        self.assertEqual(D("650"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("0.5"), D(ctrl._state["owned_base"]))
        doc = self._persisted_purse()
        self.assertEqual(doc["records"][:2], pre_reseed_records)
        reseed = doc["records"][2]
        self.assertEqual("reseed_epoch", reseed["kind"])
        self.assertEqual("epoch-3", reseed["epoch_id"])
        self.assertEqual("epoch-1", reseed["prev_epoch_id"])
        self.assertEqual("1:800", reseed["token"])
        self.assertEqual(D("854.545"), D(reseed["old_owned_quote"]))
        self.assertEqual(D("0.5"), D(reseed["old_owned_base"]))
        self.assertEqual(D("1000"), D(reseed["old_seed_value_quote"]))
        self.assertEqual(D("650"), D(reseed["new_owned_quote"]))
        self.assertEqual(D("0.5"), D(reseed["new_owned_base"]))
        self.assertEqual(D("800"), D(reseed["new_seed_value_quote"]))

        # t=1210/1225: wallet quote drops to 600 (undeclared outflow) -> re-anchor cuts to 600.
        balances["USDT"] = (D("600"), D("600"))
        self._cycle(ctrl, mdp, 1210.0)   # over-claim observed, grace arms
        self._cycle(ctrl, mdp, 1225.0)   # 15s > 10s grace -> cut + journal record
        self.assertEqual(D("600"), D(ctrl._state["owned_quote"]))
        reanchor = self._persisted_purse()["records"][3]
        self.assertEqual("reanchor", reanchor["kind"])
        self.assertEqual("epoch-3", reanchor["epoch_id"])
        self.assertEqual(D("650"), D(reanchor["old_owned_quote"]))
        self.assertEqual(D("600"), D(reanchor["new_owned_quote"]))
        self.assertEqual(D("0.5"), D(reanchor["old_owned_base"]))
        self.assertEqual(D("0.5"), D(reanchor["new_owned_base"]))
        self.assertEqual(D("50"), D(reanchor["overclaim_quote"]))
        self.assertEqual("undeclared_outflow", reanchor["classification"])
        self.assertEqual(D("600"), D(reanchor["wallet_quote_total"]))

        # t=1230: SELL fill in epoch-3 (0.2 XMR, quote 62, fees 0.062) -> owned (661.938, 0.3).
        exec_c = _filling_executor("sell_311", TradeType.SELL, 311, "exec-c",
                                   filled_base="0.2", filled_quote="62", fees="0.062")
        ctrl.executors_info = [exec_c]
        balances["USDT"] = (D("662"), D("662"))
        balances["XMR"] = (D("1.3"), D("1.3"))
        self._cycle(ctrl, mdp, 1230.0)
        self.assertEqual(D("661.938"), D(ctrl._state["owned_quote"]))
        self.assertEqual(D("0.3"), D(ctrl._state["owned_base"]))
        doc = self._persisted_purse()
        self.assertEqual(5, doc["sequence"])
        self.assertEqual(
            ["opening_epoch", "fills_rollup", "reseed_epoch", "reanchor", "fills_rollup"],
            [r["kind"] for r in doc["records"]],
        )
        rollup3 = doc["records"][4]
        self.assertEqual("epoch-3", rollup3["epoch_id"])
        self.assertEqual(D("-0.2"), D(rollup3["base_delta_cum"]))
        self.assertEqual(D("61.938"), D(rollup3["quote_delta_cum"]))
        self.assertEqual(D("0.062"), D(rollup3["fees_quote_cum"]))
        self.assertEqual(1, rollup3["fills_seen"])

        # Derived metrics at ref=310 -- pure contract arithmetic (see docstring).
        metrics = ctrl._purse.derived_metrics(
            reference_price=D("310"), owned_quote=D("661.938"), owned_base=D("0.3"),
        )
        self.assertEqual(D("900"), metrics["contributed"])
        self.assertEqual(D("0"), metrics["withdrawn"])
        self.assertEqual(D("59.483"), metrics["earned_realized"])
        self.assertEqual(D("754.938"), metrics["equity_quote"])
        self.assertEqual(D("-145.062"), metrics["earned_total"])
        self.assertEqual(D("-204.545"), metrics["unrealized"])
        self.assertEqual(D("50"), metrics["drift"])


# ==================================================== re-anchor classification: dust -> drift

class TestReanchorClassification(_Harness):

    def test_small_cut_classifies_as_drift(self):
        # cut value 3 <= min_order_quote 5 -> "drift" (not an operator-classifiable outflow).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(600), D(600))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_reconcile_threshold_quote=Decimal("1"),
                           ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=603, owned_base=0, seed_value=603)
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1015.0)
        self.assertEqual(D("600"), D(ctrl._state["owned_quote"]))
        records = self._persisted_purse()["records"]
        self.assertEqual("reanchor", records[1]["kind"])
        self.assertEqual("drift", records[1]["classification"])
        self.assertEqual(D("3"), D(records[1]["old_owned_quote"]) - D(records[1]["new_owned_quote"]))


# ==================================================== quarantine independence (F2/F3/F16)

class TestQuarantineIndependence(_Harness):

    def test_state_quarantine_leaves_purse_untouched_and_reinit_carries_predecessor(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        with self._state_path.open("w", encoding="utf-8") as f:
            json.dump(_valid_state("ctrl-hbpurse-p4", owned_quote="400", owned_base="0",
                                   seed_value="400", init_ts="1000"), f)
        ctrl1 = self._build(mdp)
        self._cycle(ctrl1, mdp, 1000.0)
        snapshot = self._persisted_purse()["records"]
        self.assertEqual(1, len(snapshot))
        self.assertEqual(D("400"), D(snapshot[0]["contributed_opening_quote"]))

        # Corrupt the STATE file; a fresh controller quarantines it and re-initializes.
        with self._state_path.open("w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        ctrl2 = self._build(mdp)
        self._cycle(ctrl2, mdp, 2000.0)

        # The purse journal file was NOT touched by the quarantine rename: still at purse_path,
        # the original opening record byte-identical, and history ACCUMULATED (a re-init epoch
        # was appended with the recovery reason as predecessor -- F16), never reset.
        self.assertTrue(self._purse_path.exists())
        doc = self._persisted_purse()
        self.assertEqual(2, len(doc["records"]))
        self.assertEqual(snapshot[0], doc["records"][0])
        reinit = doc["records"][1]
        self.assertEqual("opening_epoch", reinit["kind"])
        self.assertEqual("corrupt_state", reinit["predecessor"])
        self.assertEqual("current_equity_only", reinit["opening_basis_quality"])
        # A re-init epoch declares NO new basis (contributed would double-count inception).
        self.assertEqual(D("0"), D(reinit["contributed_opening_quote"]))
        self.assertEqual(D("0"), D(reinit["earned_opening_quote"]))
        self.assertEqual(1, len(self._emit_events(ctrl2, "range_ladder_purse_epoch_appended")))
        # The fresh state carries the bridge marker again.
        self.assertIs(True, self._persisted_state()["purse_initialized"])


# ==================================================== fail-closed: missing / degraded purse

class TestFailClosed(_Harness):

    def _sell_ladder_balances(self):
        # All-base wallet with sells above the ask -> a funded SELL ladder that WOULD place.
        return {"XMR": (D(1), D(1)), "USDT": (D("0.04"), D("0.04"))}

    def test_post_bootstrap_missing_purse_degrades_blocks_and_never_rebootstraps(self):
        balances = self._sell_ladder_balances()
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl1 = self._build(mdp)
        self._init_state(ctrl1, owned_quote=0, owned_base=1, seed_value=170)
        self._cycle(ctrl1, mdp, 1000.0)
        self.assertTrue(ctrl1.create_actions_proposal(), "scenario must place orders when healthy")
        self.assertIs(True, self._persisted_state()["purse_initialized"])

        # The journal disappears (share glitch, manual deletion). A restarted controller must
        # fail CLOSED: degraded, no new orders, and NO silent re-bootstrap.
        os.remove(self._purse_path)
        ctrl2 = self._build(mdp)
        self._cycle(ctrl2, mdp, 2000.0)
        self.assertTrue(ctrl2._purse_degraded)
        self.assertEqual([], ctrl2.create_actions_proposal())
        self.assertEqual(
            1, len(self._emit_events(ctrl2, "range_ladder_create_blocked_accounting_degraded"))
        )
        degraded_events = self._emit_events(ctrl2, "range_ladder_purse_degraded")
        self.assertEqual(1, len(degraded_events))
        self.assertEqual("purse_missing_post_bootstrap", degraded_events[0].kwargs["reason"])
        # Another cycle: STILL no file fabricated.
        self._cycle(ctrl2, mdp, 2001.0)
        self.assertFalse(self._purse_path.exists())
        self.assertTrue(ctrl2._purse_degraded)

    def test_bootstrap_pending_connector_not_ready_blocks_creates(self):
        # CDX-R01: an initialized, FUNDED controller whose purse has not bootstrapped yet
        # (connector explicitly ready=False) must not propose orders from cached balances.
        # The deferral is fail-closed: not degraded, just not yet journaled -- and un-journaled
        # means no new orders.
        balances = self._sell_ladder_balances()
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        mdp.get_connector.return_value.ready = False   # the literal False: EXPLICITLY not ready
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base=1, seed_value=170)
        self._cycle(ctrl, mdp, 1000.0)

        self.assertFalse(self._purse_path.exists(), "bootstrap must defer, not journal blind")
        self.assertFalse(ctrl._purse_degraded)         # deferral, not degradation
        self.assertEqual([], ctrl.create_actions_proposal())
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_create_blocked_purse_pending")))

        # Connector comes up -> the one-shot bootstrap lands and creates open again.
        mdp.get_connector.return_value.ready = True
        self._cycle(ctrl, mdp, 1001.0)
        self.assertTrue(self._purse_path.exists())
        self.assertTrue(ctrl.create_actions_proposal(), "creates must resume once journaled")

    def test_bootstrap_wallet_read_failure_defers_and_never_journals_zeros(self):
        # CDX-R02: a failed wallet read during the one-shot bootstrap must DEFER the opening
        # epoch -- never let the _safe_* zero-fallback fabricate a permanent observation.
        # Each of the four opening wallet reads is exercised independently.
        cases = [("get_balance", "USDT"), ("get_balance", "XMR"),
                 ("get_available_balance", "USDT"), ("get_available_balance", "XMR")]
        for method, broken_asset in cases:
            with self.subTest(method=method, asset=broken_asset):
                balances = {"XMR": (D(1), D(1)), "USDT": (D(500), D(500))}
                mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
                idx = 1 if method == "get_available_balance" else 0

                def broken_reader(c, a, *, _broken=broken_asset, _idx=idx):
                    if a == _broken:
                        raise OSError("simulated wallet read failure")
                    return balances[a][_idx]

                getattr(mdp, method).side_effect = broken_reader
                ctrl = self._build(mdp)
                self._init_state(ctrl, owned_quote=400, owned_base=0, seed_value=400)
                self._cycle(ctrl, mdp, 1000.0)

                self.assertFalse(self._purse_path.exists(),
                                 "a failed read must never bootstrap a zeroed opening epoch")
                self.assertFalse(ctrl._purse_degraded)
                self.assertEqual([], ctrl.create_actions_proposal())
                self.assertEqual(
                    1, len(self._emit_events(ctrl, "range_ladder_purse_wallet_unreadable"))
                )

        # Reads recover -> the bootstrap lands with the REAL wallet observations, and the
        # deferral was zero-residue (no half-created journal to collide with).
        balances = {"XMR": (D(1), D(1)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=400, owned_base=0, seed_value=400)
        self._cycle(ctrl, mdp, 2000.0)
        rec = self._persisted_purse()["records"][0]
        self.assertEqual(D("500"), D(rec["wallet_quote_total"]))
        self.assertEqual(D("1"), D(rec["wallet_base_total"]))

    def test_corrupt_purse_degrades_and_defers_reseed_until_recovery(self):
        with self._purse_path.open("w", encoding="utf-8") as f:
            f.write("{ corrupt purse bytes ")
        balances = {"XMR": (D(0), D(0)), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, reseed_fund_from_wallet_once=True, reseed_generation=1)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=100)

        self._cycle(ctrl, mdp, 1000.0)
        self.assertTrue(ctrl._purse_degraded)
        self.assertEqual("purse_load_failed", ctrl._purse_degraded_reason)
        # The reseed epoch could not be journaled -> deferred, token NOT consumed.
        self.assertNotIn("last_reseed_token", ctrl._state)
        self.assertEqual(D("100"), D(ctrl._state["owned_quote"]))
        self.assertEqual(
            1, len(self._emit_events(ctrl, "range_ladder_reseed_deferred_purse_unavailable"))
        )
        # The corrupt journal was NOT overwritten or moved (it is the permanent record).
        self.assertEqual("{ corrupt purse bytes ",
                         self._purse_path.read_text(encoding="utf-8"))

        # Operator resolves (removes the corrupt file; no marker was ever set) -> the
        # bootstrap runs, the journal recovers, and the reseed applies with its epoch record.
        os.remove(self._purse_path)
        self._cycle(ctrl, mdp, 1001.0)
        self.assertFalse(ctrl._purse_degraded)
        self.assertEqual("1:170", ctrl._state["last_reseed_token"])
        self.assertEqual(D("170"), D(ctrl._state["owned_quote"]))
        kinds = [r["kind"] for r in self._persisted_purse()["records"]]
        self.assertEqual(["opening_epoch", "reseed_epoch"], kinds)
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_purse_recovered")))


# ==================================================== purse IO failure: pending-delta retry

class TestPurseSaveFailureRetry(_Harness):

    def test_purse_save_failure_degrades_and_pending_rollup_retries_to_disk(self):
        balances = {"XMR": (D("0.1"), D("0.1")), "USDT": (D(70), D(70))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=100000)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=130)
        self._cycle(ctrl, mdp, 1000.0)   # bootstrap; journal on disk with 1 record

        # The NEXT purse save fails once (simulated on the live instance; real disk untouched).
        real_save = ctrl._purse.save
        calls = {"n": 0}

        def flaky_save():
            calls["n"] += 1
            if calls["n"] == 1:
                raise PurseIOError("simulated purse disk failure")
            return real_save()

        ctrl._purse.save = flaky_save

        # A BUY fill books: the STATE commits first (owned advances, contract save ordering),
        # then the purse save fails -> degraded, rollup retained in memory, nothing on disk yet.
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy",
            filled_base="0.1", filled_quote="30", fees="0.03",
        )]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(D("69.97"), D(ctrl._state["owned_quote"]))   # state advanced (100 - 30.03)
        self.assertEqual(D("0.1"), D(ctrl._state["owned_base"]))
        self.assertTrue(ctrl._purse_degraded)
        self.assertEqual("purse_save_failed", ctrl._purse_degraded_reason)
        self.assertEqual([], ctrl.create_actions_proposal())
        self.assertEqual(1, len(self._persisted_purse()["records"]))   # rollup NOT on disk yet

        # Next cycle: the retained delta retries and lands; degraded clears; orders resume.
        self._cycle(ctrl, mdp, 1011.0)
        self.assertFalse(ctrl._purse_degraded)
        doc = self._persisted_purse()
        self.assertEqual(2, len(doc["records"]))
        rollup = doc["records"][1]
        self.assertEqual("fills_rollup", rollup["kind"])
        self.assertEqual("epoch-1", rollup["epoch_id"])
        self.assertEqual(D("0.1"), D(rollup["base_delta_cum"]))
        self.assertEqual(D("-30.03"), D(rollup["quote_delta_cum"]))   # -(30 + 0.03), fees netted
        self.assertEqual(D("0.03"), D(rollup["fees_quote_cum"]))
        self.assertEqual(1, rollup["fills_seen"])
        self.assertEqual(1, len(self._emit_events(ctrl, "range_ladder_purse_recovered")))


# ==================================================== bootstrap idempotency (marker crash)

class TestMarkerCrashRetry(_Harness):

    def test_marker_crash_retry_does_not_duplicate_opening_epoch(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        with self._state_path.open("w", encoding="utf-8") as f:
            json.dump(_valid_state("ctrl-hbpurse-p4", owned_quote="400", owned_base="0",
                                   seed_value="400", init_ts="1000"), f)

        # Session 1: the purse bootstraps and saves, but EVERY state write fails -- the bridge
        # marker never lands (the crash-between-purse-save-and-marker-commit window).
        ctrl1 = self._build(mdp)
        ctrl1._write_state_to_disk = lambda state: (_ for _ in ()).throw(OSError("simulated"))
        self._cycle(ctrl1, mdp, 1000.0)
        self.assertTrue(self._purse_path.exists())
        self.assertEqual(1, len(self._persisted_purse()["records"]))
        self.assertNotIn("purse_initialized", self._persisted_state())

        # Session 2 (restart, healthy writer): the opening-epoch note's init_ts stamp matches
        # the state's initialized_timestamp -> SAME incarnation -> NO duplicate epoch; the
        # marker simply commits. Removing the stamp match would append a second opening epoch
        # here -> this assertion fails.
        ctrl2 = self._build(mdp)
        self._cycle(ctrl2, mdp, 2000.0)
        records = self._persisted_purse()["records"]
        self.assertEqual(1, len([r for r in records if r["kind"] == "opening_epoch"]))
        self.assertEqual(1, len(records))
        self.assertIs(True, self._persisted_state()["purse_initialized"])
        self.assertEqual([], self._emit_events(ctrl2, "range_ladder_purse_epoch_appended"))

    def test_marker_transient_failure_recovers_same_process(self):
        # CDX-R04: the purse bootstrap saves, but the FIRST marker state-commit fails. On a
        # QUIET controller that failed marker write is the only commit there is, so the
        # end-of-cycle backstop must keep retrying it WHILE accounting_degraded is set --
        # a guard that skips on the very flag the failure raised would deadlock the
        # degradation until restart.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        with self._state_path.open("w", encoding="utf-8") as f:
            json.dump(_valid_state("ctrl-hbpurse-p4", owned_quote="400", owned_base="0",
                                   seed_value="400", init_ts="1000"), f)
        ctrl = self._build(mdp)
        ctrl._write_state_to_disk = lambda state: (_ for _ in ()).throw(OSError("simulated"))
        self._cycle(ctrl, mdp, 1000.0)
        self.assertTrue(self._purse_path.exists())     # the journal itself is durable
        self.assertNotIn("purse_initialized", self._persisted_state())
        self.assertTrue(ctrl._accounting_degraded)     # the marker commit failed
        self.assertEqual([], ctrl.create_actions_proposal())

        # The writer recovers -- same process, no restart, and NO money mutation for the
        # marker to piggyback on. The backstop retry alone must land it and clear the flag.
        del ctrl._write_state_to_disk
        self._cycle(ctrl, mdp, 1001.0)
        self.assertIs(True, self._persisted_state()["purse_initialized"])
        self.assertFalse(ctrl._accounting_degraded)
        self.assertEqual(
            1, len(self._emit_events(ctrl, "range_ladder_accounting_degraded_cleared"))
        )
        self.assertEqual(1, len([r for r in self._persisted_purse()["records"]
                                 if r["kind"] == "opening_epoch"]))


# ==================================================== PurseLedger validation unit tests

class _LedgerUnitHarness(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "unit.purse.json"

    def _ledger(self):
        return PurseLedger(self._path, controller_id="cid", controller_name="range_inventory_ladder",
                           trading_pair="XMR-USDT")

    @staticmethod
    def _opening(seq=1, epoch="epoch-1"):
        return {
            "seq": seq, "ts": 1000.0, "kind": "opening_epoch", "epoch_id": epoch,
            "owned_quote": "100", "owned_base": "0", "seed_value_quote": "100",
            "reference_price": "300", "wallet_quote_total": "100", "wallet_base_total": "0",
            "unavailable_quote": "0", "unavailable_base": "0",
            "contributed_opening_quote": "100", "earned_opening_quote": "0",
            "opening_basis_quality": "current_equity_only", "predecessor": None,
            "note": "init_ts=1000",
        }

    def _doc(self, **overrides):
        doc = {
            "purse_schema_version": 1,
            "controller_id": "cid",
            "controller_name": "range_inventory_ladder",
            "trading_pair": "XMR-USDT",
            "sequence": 1,
            "records": [self._opening()],
        }
        doc.update(overrides)
        return doc

    def _write(self, doc):
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(doc, f)

    @staticmethod
    def _reseed(seq=2, epoch="epoch-2"):
        return {
            "seq": seq, "ts": 1001.0, "kind": "reseed_epoch", "epoch_id": epoch,
            "prev_epoch_id": "epoch-1", "token": "1:800",
            "old_owned_quote": "100", "old_owned_base": "0", "old_seed_value_quote": "100",
            "new_owned_quote": "80", "new_owned_base": "0", "new_seed_value_quote": "80",
            "reference_price": "300",
        }


class TestPurseLedgerValidation(_LedgerUnitHarness):

    def test_valid_document_loads(self):
        self._write(self._doc())
        ledger = self._ledger()
        ledger.load()
        self.assertTrue(ledger.loaded)
        self.assertEqual("epoch-1", ledger.current_epoch_id())

    def test_wrong_controller_id_rejected(self):
        self._write(self._doc(controller_id="someone-else"))
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_unknown_kind_rejected(self):
        bad = self._doc()
        bad["records"].append({"seq": 2, "ts": 1001.0, "kind": "banana"})
        bad["sequence"] = 2
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_non_monotonic_seq_rejected(self):
        bad = self._doc()
        bad["records"].append(self._opening(seq=1, epoch="epoch-9"))   # repeated seq 1
        bad["sequence"] = 2
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_seq_gap_accepted_per_contract(self):
        # CDX-R06: the PINNED contract says seq is "1-based, strictly increasing" and the
        # top-level sequence is "highest record seq" -- NOT contiguous. A 1,3 journal is
        # contract-valid and MUST load (the sibling API validates against the same text; the
        # engine must not accept a narrower format than the contract defines).
        doc = self._doc()
        doc["records"].append(self._opening(seq=3, epoch="epoch-3"))
        doc["sequence"] = 3
        self._write(doc)
        ledger = self._ledger()
        ledger.load()
        self.assertTrue(ledger.loaded)
        self.assertEqual("epoch-3", ledger.current_epoch_id())
        # Appends continue from the highest seq, not the record count.
        self.assertEqual("epoch-4", ledger.next_epoch_id())

    def test_first_seq_must_be_one_rejected(self):
        # "1-based" pins the FIRST record to seq 1; starting higher means the opening was lost.
        bad = self._doc(records=[self._opening(seq=2)], sequence=2)
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_seq_decreasing_rejected(self):
        bad = self._doc()
        bad["records"].append(self._opening(seq=3, epoch="epoch-3"))
        bad["records"].append(self._opening(seq=2, epoch="epoch-2"))
        bad["sequence"] = 3
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_sequence_must_match_highest_seq_rejected(self):
        bad = self._doc(sequence=2)   # records' highest seq is 1
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_empty_journal_rejected(self):
        # CDX-R03: an on-disk journal with records: [] is the forbidden start-empty fallback.
        # Only create() may hold a transiently empty document, and only in memory.
        self._write(self._doc(records=[], sequence=0))
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_opening_missing_predecessor_key_rejected(self):
        # CDX-R03: contract-required fields must be PRESENT; predecessor may be null but
        # never absent.
        opening = self._opening()
        del opening["predecessor"]
        self._write(self._doc(records=[opening]))
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_opening_missing_note_key_rejected(self):
        opening = self._opening()
        del opening["note"]
        self._write(self._doc(records=[opening]))
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_opening_null_note_accepted(self):
        opening = self._opening()
        opening["note"] = None
        self._write(self._doc(records=[opening]))
        ledger = self._ledger()
        ledger.load()
        self.assertTrue(ledger.loaded)

    def test_reseed_missing_prev_epoch_id_key_rejected(self):
        reseed = self._reseed()
        del reseed["prev_epoch_id"]
        doc = self._doc()
        doc["records"].append(reseed)
        doc["sequence"] = 2
        self._write(doc)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_valid_reseed_record_accepted(self):
        doc = self._doc()
        doc["records"].append(self._reseed())
        doc["sequence"] = 2
        self._write(doc)
        ledger = self._ledger()
        ledger.load()
        self.assertEqual("epoch-2", ledger.current_epoch_id())

    def test_unsupported_schema_version_rejected(self):
        self._write(self._doc(purse_schema_version=2))
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_nonfinite_money_rejected(self):
        bad = self._doc()
        bad["records"][0]["owned_quote"] = "NaN"
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()

    def test_rollup_for_unknown_epoch_rejected(self):
        bad = self._doc()
        bad["records"].append({
            "seq": 2, "ts": 1001.0, "kind": "fills_rollup", "epoch_id": "epoch-99",
            "base_delta_cum": "0", "quote_delta_cum": "0", "fees_quote_cum": "0",
            "fills_seen": 0, "last_update_ts": 1001.0,
        })
        bad["sequence"] = 2
        self._write(bad)
        with self.assertRaises(PurseIntegrityError):
            self._ledger().load()


# ==================================================== in-place rollup strictness (CDX-R05)

class TestRollupUpdateStrictness(_LedgerUnitHarness):
    """CDX-R05: update_fills_rollup mutates the permanent journal in place (the one
    append-only exception), so every incoming delta must be finite-checked BEFORE any
    mutation -- a NaN/Infinity delta must raise immediately and leave the document
    untouched, never serialize into the file and poison the next load."""

    def test_rollup_first_use_rejects_nonfinite(self):
        self._write(self._doc())
        ledger = self._ledger()
        ledger.load()
        with self.assertRaises(PurseIntegrityError):
            ledger.update_fills_rollup(epoch_id="epoch-1", d_base=D("0"),
                                       d_quote=Decimal("NaN"), d_fees=D("0"),
                                       fills=1, ts=1001.0)
        self.assertEqual(1, len(ledger.records()))   # no rollup record was created

    def test_rollup_inplace_update_rejects_bad_inputs_and_leaves_doc_unchanged(self):
        self._write(self._doc())
        ledger = self._ledger()
        ledger.load()
        ledger.update_fills_rollup(epoch_id="epoch-1", d_base=D("1"), d_quote=D("-300.3"),
                                   d_fees=D("0.3"), fills=1, ts=1001.0)
        snapshot = ledger.records()

        bad_deltas = [("d_base", Decimal("NaN")), ("d_base", Decimal("Infinity")),
                      ("d_base", Decimal("-Infinity")),
                      ("d_quote", Decimal("NaN")), ("d_quote", Decimal("Infinity")),
                      ("d_quote", Decimal("-Infinity")),
                      ("d_fees", Decimal("NaN")), ("d_fees", Decimal("Infinity")),
                      ("d_fees", Decimal("-Infinity")), ("d_fees", Decimal("-1"))]
        for field, bad in bad_deltas:
            with self.subTest(field=field, bad=str(bad)):
                kwargs = {"d_base": D("0.1"), "d_quote": D("-31"), "d_fees": D("0.01")}
                kwargs[field] = bad
                with self.assertRaises(PurseIntegrityError):
                    ledger.update_fills_rollup(epoch_id="epoch-1", fills=1, ts=1002.0,
                                               **kwargs)
                self.assertEqual(snapshot, ledger.records(),
                                 "a rejected update must leave the document untouched")
        for bad_fills in (-1, True):
            with self.subTest(bad_fills=bad_fills):
                with self.assertRaises(PurseIntegrityError):
                    ledger.update_fills_rollup(epoch_id="epoch-1", d_base=D("0"),
                                               d_quote=D("0"), d_fees=D("0"),
                                               fills=bad_fills, ts=1002.0)
                self.assertEqual(snapshot, ledger.records())
        with self.subTest(bad_ts="nan"):
            with self.assertRaises(PurseIntegrityError):
                ledger.update_fills_rollup(epoch_id="epoch-1", d_base=D("0"), d_quote=D("0"),
                                           d_fees=D("0"), fills=0, ts=float("nan"))
            self.assertEqual(snapshot, ledger.records())

        # The untouched document still saves and reloads cleanly -- nothing leaked to disk.
        ledger.save()
        fresh = self._ledger()
        fresh.load()
        self.assertEqual(snapshot, fresh.records())


# ==================================================== custom_info backward compatibility

class TestCustomInfoCompat(_Harness):

    # CDX-R07: the FULL pre-P4 custom_info surface for THIS scenario, value by value. The
    # oracle is the PRE-PHASE build (F9's byte-compat contract IS "what pre-P4 emitted"):
    # captured by running this exact scenario -- balances XMR (2,2) / USDT (1100,1100),
    # mid 300 bid 299 ask 301, owned_quote 1000, owned_base 2, seed 1600, default config,
    # one cycle at t=1000 -- against the merge-base nonkyc (ad5eceee, pre-P4), then
    # hand-checked against the field semantics. Any pre-existing key whose value drifts from
    # this dict is a dashboard-breaking regression. Two keys are per-run and asserted with
    # predicates instead: state_file (tmp path) and diagnostic_log_path (session stamp).
    PRE_P4_BASELINE = {
        "accounting_degraded": "False",
        "active_order_executors": 0,
        "available_base_balance": "2",
        "available_quote_balance": "1100",
        "blocked_level_ids": [],
        "buy_cooldown_armed": "False",
        "buy_cooldown_remaining_s": "None",
        "buy_cooldown_time": "3600",
        "buy_dirty_reason": "",
        "buy_fee_headroom_quote": "0",
        "buy_prices": ["321", "318", "315"],
        "buy_side_dirty": "False",
        "cancel_disabled_side_orders": "True",
        "cap_factor": "1",
        "config_rebuild_pending": "False",
        "connector": "nonkyc",
        "deploy_ceiling_quote": "1600",
        "deploy_headroom_quote": "1600",
        "deployable_base_total": "2",
        "deployable_quote_total": "1000",
        "diagnostic_heartbeat_interval_seconds": "300",
        "diagnostic_log_enabled": "True",
        "enable_buys": "True",
        "enable_sells": "True",
        "event_refresh_enabled": "False",
        "free_buy_budget_quote": "1000",
        "free_sell_budget_base": "2",
        "fund_growth_quote": "0",
        "global_refresh_remaining_s": "0.0",
        "initial_fund_value_quote": "1600",
        "initialization_blocked_reason": "",
        "initialization_ready": "True",
        "inventory_abs_notional_quote": "0",
        "inventory_cum_fees_quote": "0",
        "inventory_global_pnl_quote": "0",
        "inventory_net_base_amount": "0",
        "inventory_realized_pnl_quote": "0",
        "inventory_unrealized_pnl_quote": "0",
        "last_global_refresh_ts": "0.0",
        "ledger_funded_budgets": "True",
        "ledger_surplus_quote": "100",
        "managed_base_total": "2",
        "managed_fund_value_quote": "1600",
        "managed_quote_total": "1000",
        "market_data_error": "",
        "market_data_hard_pause": "False",
        "market_data_ready": "True",
        "market_data_unavailable_duration_s": "0",
        "max_fund_value_quote": "100000",
        "out_of_range_action": "dormant",
        "owned_base_free": "2",
        "owned_quote_free": "1000",
        "price_regime": "below_buy_range",
        "reconciliation_gap_quote": "0",
        "reference_price": "300",
        "refresh_wave_release_buy_quote": "0",
        "refresh_wave_release_sell_base": "0",
        "reservation_sources_buy": {},
        "reservation_sources_sell": {},
        "reserve_base_balance": "0",
        "reserve_quote_balance": "0",
        "seed_value_quote": "1600",
        "sell_cooldown_armed": "False",
        "sell_cooldown_remaining_s": "None",
        "sell_cooldown_time": "3600",
        "sell_dirty_reason": "",
        "sell_prices": ["350", "355", "360"],
        "sell_side_dirty": "False",
        "session_elapsed_s": "0.0",
        "session_expired": "False",
        "session_expired_reason": "",
        "state_io_failures": 0,
        "state_migrated_from_version": "",
        "state_recovery_reason": "",
        "state_schema_version": "10",
        "strategy": "range_inventory_ladder",
        "timestamp_ms": 1000000,
        "total_base_balance": "2",
        "total_quote_balance": "1100",
        "tracked_positions": 0,
        "trading_pair": "XMR-USDT",
    }

    PURSE_BLOCK_KEYS = [
        "purse_ready", "purse_initialized", "purse_path", "accounting_degraded",
        "purse_degraded", "purse_degraded_reason", "purse_io_failures",
        "purse_pending_writes", "epoch_id", "opening_basis_quality", "reanchor_count",
        "last_reseed_token", "reseed_generation", "contributed", "withdrawn",
        "earned_realized", "earned_total", "unrealized", "drift", "equity_quote",
    ]

    def test_custom_info_keeps_old_keys_and_adds_purse_block(self):
        balances = {"XMR": (D(2), D(2)), "USDT": (D(1100), D(1100))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=1000, owned_base=2, seed_value=1600)
        self._cycle(ctrl, mdp, 1000.0)
        info = ctrl.get_custom_info()

        # CDX-R07: EVERY pre-P4 key must be present with the pre-P4 VALUE (byte-compatible),
        # not merely present -- a broken reference_price/budget/PnL value with the key intact
        # is exactly the dashboard regression F9 forbids.
        mismatches = {
            key: (expected, info.get(key, "<MISSING>"))
            for key, expected in self.PRE_P4_BASELINE.items()
            if info.get(key, "<MISSING>") != expected
        }
        self.assertEqual({}, mismatches,
                         f"pre-P4 custom_info values drifted from the pre-P4 build: {mismatches}")
        # The two per-run keys, pinned by predicate instead of literal value.
        self.assertEqual(str(self._state_path), info["state_file"])
        self.assertRegex(
            str(info["diagnostic_log_path"]),
            r"^data[\\/]range_inventory_ladder_ctrl-hbpurse-p4\.diagnostic_\d{8}-\d{6}\.jsonl$",
        )
        # F9: the clarified alias mirrors the old key byte-for-byte.
        self.assertEqual(info["fund_growth_quote"], info["fund_growth_since_epoch_mtm"])

        purse = info["purse"]
        missing_purse = [key for key in self.PURSE_BLOCK_KEYS if key not in purse]
        self.assertEqual([], missing_purse, f"purse block keys missing: {missing_purse}")
        self.assertIs(True, purse["purse_ready"])
        self.assertEqual("epoch-1", purse["epoch_id"])
        self.assertEqual("current_equity_only", purse["opening_basis_quality"])
        # Undeclared bootstrap: contributed = equity = 1000 + 2*300 = 1600; earned_total = 0.
        self.assertEqual(D("1600"), D(purse["contributed"]))
        self.assertEqual(D("0"), D(purse["withdrawn"]))
        self.assertEqual(D("0"), D(purse["earned_total"]))
        self.assertEqual(D("1600"), D(purse["equity_quote"]))
        self.assertEqual(0, purse["reanchor_count"])

    def test_status_text_gains_purse_section_and_epoch_label(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=400, owned_base=0, seed_value=400)
        self._cycle(ctrl, mdp, 1000.0)
        status = "\n".join(ctrl.to_format_status())
        # F9: the epoch-relative MTM figure no longer masquerades as inception growth.
        self.assertIn("Fund growth since epoch (MTM):", status)
        self.assertNotIn("Fund growth since init:", status)
        self.assertIn("Purse (inception): contributed 400.000000", status)
        self.assertIn("Purse epoch: epoch-1 (current_equity_only)", status)


if __name__ == "__main__":
    unittest.main()
