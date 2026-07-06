"""Phase 6 hardening: _save_state flushes + fsyncs before the atomic replace.

mkstemp -> json.dump -> os.replace with no fsync can leave a torn/zero-length state file on
host power loss; the quarantine path then re-initializes from the current wallet, silently
resetting seed_value_quote and the ledger baseline. _save_state now calls f.flush() +
os.fsync(f.fileno()) before os.replace, and best-effort fsyncs the parent directory on POSIX
(wrapped so non-POSIX/test environments never break).
"""
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

import range_inventory_ladder as ril  # noqa: E402
from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)


class TestSaveStateFsync(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

        config = RangeInventoryLadderConfig(
            id="ctrl-fsync",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")],
            buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("350")],
            sell_amounts_pct=[Decimal("1")],
        )
        self.ctrl = RangeInventoryLadderController(
            config, market_data_provider=MagicMock(), actions_queue=MagicMock()
        )
        patcher = patch.object(
            type(self.ctrl), "state_path", new_callable=PropertyMock,
            return_value=self._state_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctrl._state = {"initialized": True, "owned_quote": "123.45"}

    def test_fsync_called_with_temp_file_fd_before_replace(self):
        real_mkstemp = tempfile.mkstemp
        captured = {}

        def capturing_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            captured["fd"] = fd
            captured["path"] = path
            return fd, path

        events = []
        real_replace = ril.os.replace

        def recording_replace(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        def recording_fsync(fd):
            events.append(("fsync", fd))
            # no real fsync needed; behavior under test is the call itself

        with patch.object(ril.tempfile, "mkstemp", side_effect=capturing_mkstemp), \
                patch.object(ril.os, "fsync", side_effect=recording_fsync), \
                patch.object(ril.os, "replace", side_effect=recording_replace):
            self.ctrl._save_state()

        fsync_calls = [e for e in events if isinstance(e, tuple) and e[0] == "fsync"]
        self.assertGreaterEqual(len(fsync_calls), 1)
        # The FIRST fsync is the temp file's fd, and it happens BEFORE the replace.
        self.assertEqual(captured["fd"], fsync_calls[0][1])
        self.assertLess(events.index(fsync_calls[0]), events.index("replace"))

    def test_state_round_trips_through_save(self):
        self.ctrl._state = {"initialized": True, "owned_quote": "99.9", "owned_base": "0.5"}
        self.ctrl._save_state()
        with self._state_path.open("r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(self.ctrl._state, on_disk)

    def test_write_failure_still_unlinks_temp_file(self):
        with patch.object(ril.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.ctrl._save_state()
        leftovers = list(Path(self._tmp.name).glob("*.tmp"))
        self.assertEqual([], leftovers)
        self.assertFalse(self._state_path.exists())

    def test_directory_fsync_failure_is_swallowed(self):
        # On non-POSIX platforms os.open(dir) raises; _save_state must still succeed.
        real_open = ril.os.open

        def failing_dir_open(path, flags, *args, **kwargs):
            if Path(str(path)) == Path(self._tmp.name):
                raise OSError("directories cannot be opened on this platform")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(ril.os, "open", side_effect=failing_dir_open):
            self.ctrl._save_state()
        self.assertTrue(self._state_path.exists())


if __name__ == "__main__":
    unittest.main()
