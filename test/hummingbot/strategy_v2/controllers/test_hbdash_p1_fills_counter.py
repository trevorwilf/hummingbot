"""hbdash Phase 1 tests: the additive, read-only LIVE ACTIVITY read-out on custom_info.purse
(HBDASH_FINDINGS CLA-2A-01 / CDX-007, design "Phase C"). This surfaces the EXISTING per-epoch
`fills_seen` booking-event counter -- honestly labeled "booked fill events, not exchange trades"
-- via two ~10-line PurseLedger accessors (siblings of reanchor_count()) and three additive keys
on the `_purse_status_block`. It changes NO booking / re-anchor / reseed / persistence logic; it
only SUMS a field already written and SURFACES it for the panel/API.

Every expected value is SPEC-DERIVED: journals are constructed with KNOWN records (never captured
from running the code), and the arithmetic (2+5+1=8, max(1800,1500)=1800, opening ts = inception)
comes from the design spec, not the implementation's output.

Discriminating assertions (named for the reviewer's TEST INTEGRITY AUDIT -- each names the exact
single-line implementation mutation it catches):
- test_fills_seen_total_sums_all_rollup_records: fills_seen 2/5/1 -> 8. 2+5+1 is distinct from
  every proper subset (2, 5, 1, 7, 6, 3), so `return 0`, a constant, or summing only records[0]
  / the last rollup from fills_seen_total() all yield != 8 -> FAILS.
- test_activity_span_is_opening_ts_and_max_last_update: the max last_update_ts (1800) sits on an
  EARLIER record than the last rollup (1500), so a mutation returning the last rollup's ts (or the
  opening ts as latest) yields != 1800 -> FAILS.
- test_fills_seen_total_skips_corrupt_records_without_raising / _status_block_survives_corrupt_*:
  a "garbage"/negative/missing fills_seen must be SKIPPED. Dropping the defensive parse -- bare
  `int(record["fills_seen"])` -- raises ValueError/KeyError (accessor test) or the block guard
  zeros the whole read-out 4 -> 0 (block test) -> FAILS. Verifies safety invariant 3 (never
  raise into the status path) and 5 (honest labeling; no exchange-trade-implying key).
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

from controllers._shared.purse_ledger import PurseLedger  # noqa: E402
from hummingbot.core.data_type.common import OrderType, PriceType  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _without(record, key):
    """A copy of `record` with `key` removed (to build a rollup missing its fills_seen)."""
    r = dict(record)
    r.pop(key, None)
    return r


# ============================================================ PurseLedger accessor unit tests

class _LedgerHarness(unittest.TestCase):
    """Builds PurseLedger documents with KNOWN records. Valid journals go through the real
    write+load path; deliberately-corrupt records (which load() rejects) are placed straight into
    the in-memory `_doc` to exercise the accessors' defensive skip -- the only way a malformed
    record could ever reach the read path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "unit.purse.json"

    def _ledger(self):
        return PurseLedger(self._path, controller_id="cid",
                           controller_name="range_inventory_ladder", trading_pair="XMR-USDT")

    @staticmethod
    def _opening(seq=1, epoch="epoch-1", ts=1000.0):
        return {
            "seq": seq, "ts": ts, "kind": "opening_epoch", "epoch_id": epoch,
            "owned_quote": "100", "owned_base": "0", "seed_value_quote": "100",
            "reference_price": "300", "wallet_quote_total": "100", "wallet_base_total": "0",
            "unavailable_quote": "0", "unavailable_base": "0",
            "contributed_opening_quote": "100", "earned_opening_quote": "0",
            "opening_basis_quality": "current_equity_only", "predecessor": None,
            "note": "init_ts=1000",
        }

    @staticmethod
    def _reseed(seq, epoch, prev, token):
        return {
            "seq": seq, "ts": 1000.0 + seq, "kind": "reseed_epoch", "epoch_id": epoch,
            "prev_epoch_id": prev, "token": token,
            "old_owned_quote": "100", "old_owned_base": "0", "old_seed_value_quote": "100",
            "new_owned_quote": "80", "new_owned_base": "0", "new_seed_value_quote": "80",
            "reference_price": "300",
        }

    @staticmethod
    def _rollup(seq, epoch, fills, last_update_ts):
        return {
            "seq": seq, "ts": last_update_ts, "kind": "fills_rollup", "epoch_id": epoch,
            "base_delta_cum": "0", "quote_delta_cum": "0", "fees_quote_cum": "0",
            "fills_seen": fills, "last_update_ts": last_update_ts,
        }

    @staticmethod
    def _doc(records, sequence):
        return {
            "purse_schema_version": 1, "controller_id": "cid",
            "controller_name": "range_inventory_ladder", "trading_pair": "XMR-USDT",
            "sequence": sequence, "records": records,
        }

    def _load(self, *records):
        """Write a VALID journal to disk and load it through the real validating path."""
        doc = self._doc(list(records), records[-1]["seq"])
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(doc, f)
        ledger = self._ledger()
        ledger.load()
        return ledger


class TestFillsSeenTotal(_LedgerHarness):

    def test_fills_seen_total_sums_all_rollup_records(self):
        # Per contract each epoch holds at most ONE fills_rollup (append/load reject a second),
        # so three rollups span three epochs; fills_seen_total sums across ALL of them: 2+5+1=8.
        ledger = self._load(
            self._opening(1, "epoch-1", ts=1000.0),
            self._rollup(2, "epoch-1", fills=2, last_update_ts=1100.0),
            self._reseed(3, "epoch-2", "epoch-1", "1:800"),
            self._rollup(4, "epoch-2", fills=5, last_update_ts=1200.0),
            self._reseed(5, "epoch-3", "epoch-2", "2:800"),
            self._rollup(6, "epoch-3", fills=1, last_update_ts=1300.0),
        )
        self.assertEqual(8, ledger.fills_seen_total())

    def test_empty_and_unloaded_ledger_total_is_zero(self):
        # Unloaded ledger -> 0 (no journal adopted); a journal with an opening but no rollup -> 0.
        self.assertEqual(0, self._ledger().fills_seen_total())
        ledger = self._load(self._opening(1, "epoch-1", ts=1000.0))
        self.assertEqual(0, ledger.fills_seen_total())

    def test_fills_seen_total_skips_corrupt_records_without_raising(self):
        # load() rejects a non-int fills_seen, so these corrupt records can only reach the accessor
        # as an in-memory anomaly (a partially-written / future-kind record). The accessor SKIPS
        # each -- "garbage", negative, missing -- and sums only the valid ones: 4 + 1 = 5.
        ledger = self._ledger()
        ledger._doc = self._doc([
            self._opening(1, "epoch-1", ts=1000.0),
            self._rollup(2, "epoch-1", fills=4, last_update_ts=1100.0),
            {**self._rollup(3, "epoch-2", fills=0, last_update_ts=1200.0), "fills_seen": "garbage"},
            {**self._rollup(4, "epoch-3", fills=0, last_update_ts=1300.0), "fills_seen": -3},
            _without(self._rollup(5, "epoch-4", fills=0, last_update_ts=1400.0), "fills_seen"),
            self._rollup(6, "epoch-5", fills=1, last_update_ts=1500.0),
        ], 6)
        # Bare int(record["fills_seen"]) would raise ValueError on "garbage" / KeyError on the
        # missing one / include -3 -- any of which breaks this equality (or errors the test).
        self.assertEqual(5, ledger.fills_seen_total())
        # The span reads ts fields (all valid here), so the corrupt fills_seen leave it intact.
        self.assertEqual((1000.0, 1500.0), ledger.activity_span())


class TestActivitySpan(_LedgerHarness):

    def test_no_opening_and_unloaded_span_is_none_none(self):
        self.assertEqual((None, None), self._ledger().activity_span())

    def test_opening_only_span_is_open_ended(self):
        # Opening epoch but no rollup -> inception known, latest None (no activity booked yet).
        ledger = self._load(self._opening(1, "epoch-1", ts=1234.0))
        self.assertEqual((1234.0, None), ledger.activity_span())

    def test_activity_span_is_opening_ts_and_max_last_update(self):
        # earliest = FIRST opening_epoch ts (1000). latest = MAX last_update_ts across rollups.
        # epoch-1's rollup (1800) precedes epoch-2's (1500), so the max sits on an EARLIER record
        # than the last rollup -- a "return the last rollup's ts" mutation would yield 1500.
        ledger = self._load(
            self._opening(1, "epoch-1", ts=1000.0),
            self._rollup(2, "epoch-1", fills=2, last_update_ts=1800.0),
            self._reseed(3, "epoch-2", "epoch-1", "1:800"),
            self._rollup(4, "epoch-2", fills=5, last_update_ts=1500.0),
        )
        self.assertEqual((1000.0, 1800.0), ledger.activity_span())
        self.assertEqual(7, ledger.fills_seen_total())


# ============================================================ controller surface tests

def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
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
        id="ctrl-hbdash-p1",
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


def _valid_state(controller_id, *, owned_quote, owned_base, seed_value, init_ts="1000"):
    return {
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


class _CtrlHarness(unittest.TestCase):

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

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value):
        ctrl._state = _valid_state(
            ctrl.config.id, owned_quote=owned_quote, owned_base=owned_base, seed_value=seed_value,
        )
        ctrl._state_loaded = True

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())


class TestPurseStatusBlockActivity(_CtrlHarness):

    def test_status_block_and_custom_info_expose_activity(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=400, owned_base=0, seed_value=400)
        self._cycle(ctrl, mdp, 1000.0)   # bootstrap: opening_epoch ts = mdp.time() = 1000.0

        # Inject a known booked-fill rollup into the loaded journal (epoch-1 is open post-bootstrap;
        # this is the module's own append path, not a hand-built record). fills_seen -> 3.
        ctrl._purse.update_fills_rollup(epoch_id="epoch-1", d_base=D("1"), d_quote=D("-300"),
                                        d_fees=D("0.5"), fills=3, ts=1500.0)
        ctrl._purse.save()
        # Refresh processed_data so custom_info reflects the rollup through the real cycle path.
        self._cycle(ctrl, mdp, 1600.0)

        pb = ctrl._purse_status_block(D("300"))
        self.assertEqual(3, pb["fills_seen_total"])
        self.assertEqual(1000.0, pb["fills_first_ts"])
        self.assertEqual(1500.0, pb["fills_last_ts"])

        purse = ctrl.get_custom_info()["purse"]
        self.assertEqual(3, purse["fills_seen_total"])
        self.assertEqual(1000.0, purse["fills_first_ts"])
        self.assertEqual(1500.0, purse["fills_last_ts"])
        # JSON-safe primitives survive get_custom_info -> MQTT -> snapshot serialization unchanged.
        self.assertIsInstance(purse["fills_seen_total"], int)
        self.assertIsInstance(purse["fills_first_ts"], float)
        self.assertIsInstance(purse["fills_last_ts"], float)
        # Honest labeling (safety invariant 5): no exchange-trade-implying key was introduced.
        self.assertNotIn("trades", purse)
        self.assertNotIn("trade_count", purse)

    def test_status_block_zeroes_activity_when_no_journal(self):
        # Never cycled -> purse not ready -> the read-out defaults to the safe (0, None, None).
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        pb = ctrl._purse_status_block()
        self.assertIs(False, pb["purse_ready"])
        self.assertEqual(0, pb["fills_seen_total"])
        self.assertIsNone(pb["fills_first_ts"])
        self.assertIsNone(pb["fills_last_ts"])

    def test_status_block_survives_corrupt_fills_seen_and_keeps_valid_count(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=400, owned_base=0, seed_value=400)
        self._cycle(ctrl, mdp, 1000.0)   # bootstrap

        # A VALID booked-fill rollup (fills_seen=4) ...
        ctrl._purse.update_fills_rollup(epoch_id="epoch-1", d_base=D("0"), d_quote=D("0"),
                                        d_fees=D("0"), fills=4, ts=1500.0)
        # ... plus a corrupt in-memory rollup (load() would reject a non-int fills_seen; this
        # simulates a partially-written / future-kind record reaching the read path).
        ctrl._purse._doc["records"].append({
            "seq": 999, "ts": 1600.0, "kind": "fills_rollup", "epoch_id": "epoch-corrupt",
            "base_delta_cum": "0", "quote_delta_cum": "0", "fees_quote_cum": "0",
            "fills_seen": "garbage", "last_update_ts": 1600.0,
        })

        # The block must NOT raise (safety invariant 3); the corrupt record is skipped and the
        # VALID fill is still counted. Under a bare-int mutation the accessor raises, the block
        # guard zeros the whole read-out, and this 4 -> 0 assert FAILS.
        pb = ctrl._purse_status_block(D("300"))
        self.assertEqual(4, pb["fills_seen_total"])
        self.assertEqual(1000.0, pb["fills_first_ts"])
        self.assertEqual(1600.0, pb["fills_last_ts"])   # max of the two valid last_update_ts
        # Every other purse key is unaffected -- the block behaves exactly as today.
        self.assertEqual(D("400"), D(pb["contributed"]))
        self.assertEqual("epoch-1", pb["epoch_id"])
        self.assertIs(True, pb["purse_ready"])


if __name__ == "__main__":
    unittest.main()
