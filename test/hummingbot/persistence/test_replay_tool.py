"""
Tests for the JSONL-to-DB replay tool.
"""
import gzip
import json
import os
import tempfile
import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from hummingbot.model import HummingbotBase
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
from hummingbot.model.bot_run import BotRun

# Import replay functions directly
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
from replay_lifecycle_jsonl_to_db import replay_file, find_jsonl_files


def _make_event(event_type="fill", **overrides):
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "event_version": 1,
        "schema_name": "lifecycle_v1",
        "bot_run_id": "test-run-001",
        "emitted_ts_ms": 1712700000000,
        "connector": "nonkyc",
        "trading_pair": "BTC-USDT",
    }
    event.update(overrides)
    return event


def _write_jsonl(filepath, events):
    with open(filepath, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, default=str) + "\n")


class TestReplayFile(unittest.TestCase):
    """Test replaying JSONL into SQLite."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def _new_stats(self):
        return {
            "files_processed": 0, "lines_read": 0, "valid_events": 0,
            "invalid_skipped": 0, "inserted": 0, "duplicates_skipped": 0, "errors": 0,
        }

    def test_replay_sample_file(self):
        events = [_make_event() for _ in range(10)]
        filepath = os.path.join(self.tmpdir, "test.jsonl")
        _write_jsonl(filepath, events)

        stats = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats)

        self.assertEqual(stats["lines_read"], 10)
        self.assertEqual(stats["valid_events"], 10)
        self.assertEqual(stats["inserted"], 10)
        self.assertEqual(stats["errors"], 0)

        session = self.session_factory()
        rows = session.query(OrderLifecycleEvent).all()
        self.assertEqual(len(rows), 10)
        session.close()

    def test_replay_idempotent(self):
        """Replay twice, verify no duplicate rows."""
        events = [_make_event() for _ in range(5)]
        filepath = os.path.join(self.tmpdir, "test.jsonl")
        _write_jsonl(filepath, events)

        stats1 = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats1)
        self.assertEqual(stats1["inserted"], 5)

        stats2 = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats2)
        self.assertEqual(stats2["duplicates_skipped"], 5)
        self.assertEqual(stats2["inserted"], 0)

        session = self.session_factory()
        rows = session.query(OrderLifecycleEvent).all()
        self.assertEqual(len(rows), 5)
        session.close()

    def test_replay_gzip_file(self):
        events = [_make_event() for _ in range(3)]
        filepath = os.path.join(self.tmpdir, "test.jsonl.gz")
        with gzip.open(filepath, "wt", encoding="utf-8") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        stats = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats)
        self.assertEqual(stats["inserted"], 3)

    def test_validate_only(self):
        events = [_make_event() for _ in range(7)]
        filepath = os.path.join(self.tmpdir, "test.jsonl")
        _write_jsonl(filepath, events)

        stats = self._new_stats()
        replay_file(filepath, None, 500, 1, True, stats)
        self.assertEqual(stats["valid_events"], 7)
        self.assertEqual(stats["inserted"], 0)

        # No DB rows
        session = self.session_factory()
        rows = session.query(OrderLifecycleEvent).all()
        self.assertEqual(len(rows), 0)
        session.close()

    def test_malformed_line_skipped(self):
        filepath = os.path.join(self.tmpdir, "test.jsonl")
        with open(filepath, "w") as f:
            f.write(json.dumps(_make_event()) + "\n")
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps(_make_event()) + "\n")

        stats = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats)
        self.assertEqual(stats["lines_read"], 3)
        self.assertEqual(stats["valid_events"], 2)
        self.assertEqual(stats["invalid_skipped"], 1)
        self.assertEqual(stats["inserted"], 2)

    def test_schema_version_rejection(self):
        events = [
            _make_event(event_version=1),
            _make_event(event_version=99),
            _make_event(event_version=1),
        ]
        filepath = os.path.join(self.tmpdir, "test.jsonl")
        _write_jsonl(filepath, events)

        stats = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats)
        self.assertEqual(stats["valid_events"], 2)
        self.assertEqual(stats["invalid_skipped"], 1)
        self.assertEqual(stats["inserted"], 2)

    def test_bot_run_events_upserted(self):
        events = [
            _make_event("bot_run_started", bot_run_id="run-x", strategy_name="pmm",
                         connectors=["nonkyc"], trading_pairs=["BTC-USDT"]),
            _make_event("fill"),
            _make_event("bot_run_stopped", bot_run_id="run-x", stop_reason="operator_stop"),
        ]
        filepath = os.path.join(self.tmpdir, "test.jsonl")
        _write_jsonl(filepath, events)

        stats = self._new_stats()
        replay_file(filepath, self.session_factory, 500, 1, False, stats)

        session = self.session_factory()
        br = session.query(BotRun).filter(BotRun.id == "run-x").one()
        self.assertEqual(br.strategy_name, "pmm")
        self.assertEqual(br.stop_reason, "operator_stop")
        session.close()


class TestFindFiles(unittest.TestCase):
    def test_find_jsonl_files(self):
        tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmpdir, "sub"))
        open(os.path.join(tmpdir, "a.jsonl"), "w").close()
        open(os.path.join(tmpdir, "sub", "b.jsonl.gz"), "w").close()
        open(os.path.join(tmpdir, "c.txt"), "w").close()
        files = find_jsonl_files(tmpdir)
        self.assertEqual(len(files), 2)


if __name__ == "__main__":
    unittest.main()
