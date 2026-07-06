"""Phase 8 hardening: diagnostic JSONL rotation.

_write_diagnostic_event appended forever with no size cap; multi-week sessions grow the
file unbounded (synchronous I/O in the async loop). The file now rotates at
diagnostic_log_max_bytes (default 50 MB), keeping diagnostic_log_backup_count backups
(default 3): name.jsonl -> .1, shifting .1 -> .2 etc. The size stat is throttled to once
per 60 s, and rotation failures are swallowed (the method's never-crash contract) with the
append proceeding against the current file.
"""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pydantic
import pytest

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

import range_inventory_ladder as ril  # noqa: E402
from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)


def _config(**overrides):
    defaults = dict(
        id="ctrl-rot",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("100"),
        buy_prices=[Decimal("321")],
        buy_amounts_pct=[Decimal("1")],
        sell_prices=[Decimal("350")],
        sell_amounts_pct=[Decimal("1")],
        diagnostic_log_enabled=True,
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "diag.jsonl"

    def _build(self, *, now=1000.0, **config_overrides):
        self.mdp = MagicMock()
        self.mdp.time.return_value = now
        ctrl = RangeInventoryLadderController(
            _config(**config_overrides), market_data_provider=self.mdp, actions_queue=MagicMock()
        )
        patcher = patch.object(
            type(ctrl), "diagnostic_log_path", new_callable=PropertyMock,
            return_value=self.log_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return ctrl

    def _advance(self, t):
        self.mdp.time.return_value = t


class TestRotation(_Harness):

    def test_write_past_threshold_rotates_to_dot_1(self):
        ctrl = self._build(diagnostic_log_max_bytes=200)
        self.log_path.write_text("x" * 500, encoding="utf-8")  # already over the cap

        self._advance(2000.0)  # past the 60s stat throttle
        ctrl._write_diagnostic_event("test_event", detail="after_rotation")

        rotated = Path(f"{self.log_path}.1")
        self.assertTrue(rotated.exists())
        self.assertEqual("x" * 500, rotated.read_text(encoding="utf-8"))
        # the current file contains only the fresh event
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("test_event", content)
        self.assertNotIn("x" * 100, content)

    def test_keep_count_enforced_oldest_deleted(self):
        ctrl = self._build(diagnostic_log_max_bytes=200, diagnostic_log_backup_count=3)
        self.log_path.write_text("current" + "x" * 500, encoding="utf-8")
        Path(f"{self.log_path}.1").write_text("gen1", encoding="utf-8")
        Path(f"{self.log_path}.2").write_text("gen2", encoding="utf-8")
        Path(f"{self.log_path}.3").write_text("gen3", encoding="utf-8")

        self._advance(2000.0)
        ctrl._write_diagnostic_event("test_event")

        self.assertIn("gen1", Path(f"{self.log_path}.2").read_text(encoding="utf-8"))
        self.assertIn("gen2", Path(f"{self.log_path}.3").read_text(encoding="utf-8"))
        self.assertIn("current", Path(f"{self.log_path}.1").read_text(encoding="utf-8"))
        self.assertFalse(Path(f"{self.log_path}.4").exists())  # gen3 deleted, keep=3

    def test_rotation_failure_swallowed_append_still_happens(self):
        ctrl = self._build(diagnostic_log_max_bytes=200)
        self.log_path.write_text("x" * 500, encoding="utf-8")

        self._advance(2000.0)
        with patch.object(ril.os, "replace", side_effect=OSError("locked")):
            ctrl._write_diagnostic_event("test_event", detail="still_appended")  # must not raise

        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("x" * 500, content)          # rotation failed -> same file
        self.assertIn("still_appended", content)   # ...but the append proceeded

    def test_size_check_throttled_to_once_per_window(self):
        ctrl = self._build(diagnostic_log_max_bytes=200)
        self.log_path.write_text("seed", encoding="utf-8")

        # Path.exists() may itself stat, so assert on deltas: within the 60s window the
        # rotation check returns before ANY filesystem inspection of the log file.
        stat_calls = []
        real_stat = Path.stat

        def counting_stat(path_self, *args, **kwargs):
            if str(path_self) == str(self.log_path):
                stat_calls.append(1)
            return real_stat(path_self, *args, **kwargs)

        with patch.object(Path, "stat", counting_stat):
            self._advance(2000.0)
            ctrl._write_diagnostic_event("e1")  # window opens -> inspects the file
            first_check_calls = len(stat_calls)
            self.assertGreaterEqual(first_check_calls, 1)

            self._advance(2030.0)
            ctrl._write_diagnostic_event("e2")  # inside 60s window -> throttled, NO stat
            self._advance(2059.0)
            ctrl._write_diagnostic_event("e3")  # still inside -> NO stat
            self.assertEqual(first_check_calls, len(stat_calls))

            self._advance(2061.0)
            ctrl._write_diagnostic_event("e4")  # window elapsed -> inspects again
            self.assertGreater(len(stat_calls), first_check_calls)

    def test_no_rotation_below_threshold(self):
        ctrl = self._build(diagnostic_log_max_bytes=10_000)
        self.log_path.write_text("small", encoding="utf-8")
        self._advance(2000.0)
        ctrl._write_diagnostic_event("test_event")
        self.assertFalse(Path(f"{self.log_path}.1").exists())
        self.assertIn("small", self.log_path.read_text(encoding="utf-8"))


class TestRotationConfig(unittest.TestCase):

    def test_defaults_50mb_keep_3(self):
        cfg = _config()
        self.assertEqual(52_428_800, cfg.diagnostic_log_max_bytes)
        self.assertEqual(3, cfg.diagnostic_log_backup_count)
        for field in ("diagnostic_log_max_bytes", "diagnostic_log_backup_count"):
            extra = RangeInventoryLadderConfig.model_fields[field].json_schema_extra or {}
            self.assertFalse(extra.get("is_updatable", True), field)

    def test_validators_reject_non_positive(self):
        with pytest.raises(pydantic.ValidationError):
            _config(diagnostic_log_max_bytes=0)
        with pytest.raises(pydantic.ValidationError):
            _config(diagnostic_log_backup_count=0)
        with pytest.raises(pydantic.ValidationError):
            _config(diagnostic_log_backup_count=-1)


if __name__ == "__main__":
    unittest.main()
