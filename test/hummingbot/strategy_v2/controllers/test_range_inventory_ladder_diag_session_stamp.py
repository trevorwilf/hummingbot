"""Diagnostic-filename session stamp tests (2026-07-12 fix).

The diagnostic jsonl now carries a session-start datetime stamp
(`<name>.jsonl` -> `<name>_{YYYYMMDD-HHMMSS}.jsonl`, local time, fixed for the session's
lifetime) so files from multiple runs of one strategy can be harvested into a single
analysis directory without colliding. ONLY the diagnostic file is stamped: the state
`.json` and its `.json.owner` marker MUST keep their exact configured names across
restarts -- the resume/ownership logic depends on finding them at fixed paths.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (  # noqa: E402
    D,
    _make_mdp,
)

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

STAMPED_RE = re.compile(r"range_inventory_ladder_[a-z0-9_]+_diagnostic_\d{8}-\d{6}\.jsonl")

SESSION_1_STAMP = "20260712-081634"
SESSION_2_STAMP = "20260713-093012"


class TestDiagnosticSessionStamp(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, self._old_cwd)

    def _build(self, stamp: str, *, controller_id="ctrl-stamp",
               diag_name="range_inventory_ladder_dash_usdt_diagnostic.jsonl",
               state_name="range_inventory_ladder_dash_usdt.json"):
        balances = {"DASH": [D(0), D(0)], "USDT": [D(100), D(100)]}
        mdp = _make_mdp(balances=balances, mid=24.30, bid=24.29, ask=24.31)
        config = RangeInventoryLadderConfig(
            id=controller_id,
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="DASH-USDT",
            total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("23.0"), Decimal("22.5")],
            buy_amounts_pct=[Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("25.5"), Decimal("26.0")],
            sell_amounts_pct=[Decimal("1"), Decimal("1")],
            min_order_quote=Decimal("1"),
            diagnostic_log_file_name=diag_name,
            state_file_name=state_name,
        )
        with patch("range_inventory_ladder.time.strftime", return_value=stamp):
            ctrl = RangeInventoryLadderController(
                config, market_data_provider=mdp, actions_queue=MagicMock()
            )
        return ctrl

    def _full_state(self, ctrl, *, owned_quote="123.45", owned_base="0.5"):
        return {
            "schema_version": ctrl.STATE_SCHEMA_VERSION,
            "controller_name": "range_inventory_ladder",
            "controller_type": "market_making",
            "controller_id": ctrl.config.id,
            "connector_name": "nonkyc",
            "trading_pair": "DASH-USDT",
            "base_asset": "DASH",
            "quote_asset": "USDT",
            "initialized": True,
            "initialized_timestamp": 1000.0,
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "initial_managed_quote": owned_quote,
            "initial_claimed_base_amount": owned_base,
            "initial_reference_price": "24.30",
            "owned_quote": owned_quote,
            "owned_base": owned_base,
            "seed_value_quote": owned_quote,
            "tracked_fill_executor_ids": [],
        }

    def test_two_sessions_two_stamped_files_state_names_identical(self):
        """The mandated regression: two controller sessions with an advancing clock produce
        two DISTINCT stamped diagnostic names matching the required regex, while the state
        .json / .json.owner names are byte-identical across sessions and session 2 resumes
        state from the unstamped .json."""
        ctrl1 = self._build(SESSION_1_STAMP)
        ctrl2 = self._build(SESSION_2_STAMP)

        diag1, diag2 = ctrl1.diagnostic_log_path.name, ctrl2.diagnostic_log_path.name
        self.assertEqual(
            f"range_inventory_ladder_dash_usdt_diagnostic_{SESSION_1_STAMP}.jsonl", diag1)
        self.assertEqual(
            f"range_inventory_ladder_dash_usdt_diagnostic_{SESSION_2_STAMP}.jsonl", diag2)
        self.assertNotEqual(diag1, diag2)
        for name in (diag1, diag2):
            self.assertRegex(name, STAMPED_RE)

        # State-file naming is untouched and byte-identical across the restart.
        self.assertEqual("range_inventory_ladder_dash_usdt.json", ctrl1.state_path.name)
        self.assertEqual(ctrl1.state_path, ctrl2.state_path)

        # Session 1 persists state at the unstamped path; session 2 resumes from it.
        ctrl1._state = self._full_state(ctrl1)
        ctrl1._state_loaded = True
        ctrl1._save_state()
        self.assertTrue(ctrl1.state_path.exists())
        self.assertTrue(Path(f"{ctrl1.state_path}.owner").exists())

        ctrl2._load_state()
        self.assertEqual("123.45", ctrl2._state["owned_quote"])
        self.assertEqual("0.5", ctrl2._state["owned_base"])
        self.assertTrue(ctrl2._state["initialized"])
        # No quarantine/recovery took place -- a genuine resume.
        self.assertIsNone(ctrl2._state_recovery_reason)

    def test_stamp_is_fixed_for_the_session_lifetime(self):
        ctrl = self._build(SESSION_1_STAMP)
        first = ctrl.diagnostic_log_path
        # Even if the wall clock moves on, the session keeps its start stamp.
        with patch("range_inventory_ladder.time.strftime", return_value="20270101-000000"):
            self.assertEqual(first, ctrl.diagnostic_log_path)

    def test_diagnostic_events_write_to_the_stamped_file(self):
        ctrl = self._build(SESSION_1_STAMP)
        ctrl._write_diagnostic_event("stamp_probe", value="42")
        stamped = Path("data") / f"range_inventory_ladder_dash_usdt_diagnostic_{SESSION_1_STAMP}.jsonl"
        self.assertTrue(stamped.exists())
        record = json.loads(stamped.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("stamp_probe", record["event_type"])
        # The unstamped legacy name is no longer written.
        self.assertFalse((Path("data") / "range_inventory_ladder_dash_usdt_diagnostic.jsonl").exists())

    def test_default_diagnostic_name_is_stamped_too(self):
        ctrl = self._build(SESSION_1_STAMP, diag_name=None)
        self.assertEqual(
            f"range_inventory_ladder_ctrl-stamp.diagnostic_{SESSION_1_STAMP}.jsonl",
            ctrl.diagnostic_log_path.name)

    def test_owner_marker_name_tracks_the_state_file_not_the_diagnostic(self):
        ctrl = self._build(SESSION_1_STAMP)
        ctrl._state = self._full_state(ctrl)
        ctrl._state_loaded = True
        ctrl._save_state()
        owner = Path("data") / "range_inventory_ladder_dash_usdt.json.owner"
        self.assertTrue(owner.exists())
        payload = json.loads(owner.read_text(encoding="utf-8"))
        self.assertEqual("ctrl-stamp", payload["controller_id"])


if __name__ == "__main__":
    unittest.main()
