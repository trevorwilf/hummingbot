"""
Tests for LifecycleWriter: JSONL write, DB write, health counters, per-run files, gzip.
"""
import gzip
import json
import os
import tempfile
import time
import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
from hummingbot.persistence.lifecycle_event import LifecycleEvent
from hummingbot.persistence.lifecycle_writer import LifecycleWriter


class FakeSQLManager:
    """Minimal mock of SQLConnectionManager for testing."""
    def __init__(self, engine):
        self._engine = engine

    def get_engine(self):
        return self._engine

    def get_new_session(self):
        return Session(bind=self._engine)


class TestLifecycleWriterJSONL(unittest.TestCase):
    """Test JSONL write produces valid JSON lines."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.sql_mgr = FakeSQLManager(self.engine)
        self.bot_run_id = str(uuid.uuid4())
        self.writer = LifecycleWriter(
            sql_manager=self.sql_mgr,
            bot_run_id=self.bot_run_id,
            log_dir=self.tmpdir,
        )

    def tearDown(self):
        try:
            self.writer.close()
        except Exception:
            pass

    def test_jsonl_write_valid_json(self):
        evt = LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            price="50000",
            amount="0.1",
        )
        self.writer.write(evt)
        with open(self.writer.jsonl_path, "r") as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["event_type"], "fill")
        self.assertEqual(parsed["schema_name"], "lifecycle_v1")
        self.assertIn("event_id", parsed)
        self.assertIn("bot_run_id", parsed)
        self.assertEqual(parsed["bot_run_id"], self.bot_run_id)

    def test_jsonl_multiple_events(self):
        for i in range(10):
            evt = LifecycleEvent(
                event_type="fill",
                connector="nonkyc",
                trading_pair="BTC-USDT",
                price=str(50000 + i),
            )
            self.writer.write(evt)
        with open(self.writer.jsonl_path, "r") as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 10)
        for line in lines:
            parsed = json.loads(line)
            self.assertEqual(parsed["event_type"], "fill")

    def test_per_run_file_naming(self):
        """Test file is in logs/lifecycle/<date>/<bot_run_id>.jsonl"""
        self.assertIn("lifecycle", self.writer.jsonl_path)
        self.assertIn(self.bot_run_id, self.writer.jsonl_path)
        self.assertTrue(self.writer.jsonl_path.endswith(".jsonl"))


class TestLifecycleWriterDB(unittest.TestCase):
    """Test DB write creates OrderLifecycleEvent rows."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.sql_mgr = FakeSQLManager(self.engine)
        self.bot_run_id = str(uuid.uuid4())
        self.writer = LifecycleWriter(
            sql_manager=self.sql_mgr,
            bot_run_id=self.bot_run_id,
            log_dir=self.tmpdir,
        )

    def tearDown(self):
        try:
            self.writer.close()
        except Exception:
            pass

    def test_db_write_creates_row(self):
        evt = LifecycleEvent(
            event_type="submit_acked",
            connector="mexc",
            trading_pair="ETH-USDT",
            client_order_id="order_001",
        )
        self.writer.write(evt)
        session = Session(bind=self.engine)
        rows = session.query(OrderLifecycleEvent).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_type, "submit_acked")
        self.assertEqual(rows[0].client_order_id, "order_001")
        self.assertEqual(rows[0].bot_run_id, self.bot_run_id)
        session.close()

    def test_event_id_dedup(self):
        """Same event_id should not be inserted twice."""
        event_id = str(uuid.uuid4())
        for _ in range(2):
            evt = LifecycleEvent(
                event_type="fill",
                connector="nonkyc",
                trading_pair="BTC-USDT",
                event_id=event_id,
            )
            self.writer.write(evt)
        session = Session(bind=self.engine)
        rows = session.query(OrderLifecycleEvent).filter(
            OrderLifecycleEvent.event_id == event_id
        ).all()
        # First write succeeds, second fails (unique constraint) but doesn't crash
        self.assertEqual(len(rows), 1)
        session.close()

    def test_db_failure_doesnt_prevent_jsonl(self):
        """If DB write fails, JSONL should still succeed."""
        # Write one event normally
        evt = LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
        )
        self.writer.write(evt)
        self.assertEqual(self.writer.jsonl_writes_ok, 1)
        self.assertEqual(self.writer.db_writes_ok, 1)

        # Force a DB failure by using a duplicate event_id
        evt2 = LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            event_id=evt.event_id,  # duplicate
        )
        self.writer.write(evt2)
        self.assertEqual(self.writer.jsonl_writes_ok, 2)  # JSONL still succeeds
        self.assertEqual(self.writer.db_writes_failed, 1)  # DB failed


class TestLifecycleWriterCounters(unittest.TestCase):
    """Test health counters increment correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.sql_mgr = FakeSQLManager(self.engine)
        self.writer = LifecycleWriter(
            sql_manager=self.sql_mgr,
            bot_run_id="test-run",
            log_dir=self.tmpdir,
        )

    def tearDown(self):
        try:
            self.writer.close()
        except Exception:
            pass

    def test_counters_increment(self):
        for i in range(5):
            self.writer.write(LifecycleEvent(
                event_type="fill", connector="x", trading_pair="A-B",
            ))
        self.assertEqual(self.writer.events_total, 5)
        self.assertEqual(self.writer.jsonl_writes_ok, 5)
        self.assertEqual(self.writer.db_writes_ok, 5)
        self.assertGreater(self.writer.last_success_ts_ms, 0)


class TestLifecycleWriterGzip(unittest.TestCase):
    """Test gzip of old run files."""

    def test_compress_old_runs(self):
        tmpdir = tempfile.mkdtemp()
        engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(engine)
        sql_mgr = FakeSQLManager(engine)

        # Create an "old" file
        old_dir = os.path.join(tmpdir, "lifecycle", "2020-01-01")
        os.makedirs(old_dir, exist_ok=True)
        old_file = os.path.join(old_dir, "old-run.jsonl")
        with open(old_file, "w") as f:
            f.write('{"event_type":"test"}\n')
        # Set mtime to 2 days ago
        old_time = time.time() - 172800
        os.utime(old_file, (old_time, old_time))

        writer = LifecycleWriter(
            sql_manager=sql_mgr,
            bot_run_id="current-run",
            log_dir=tmpdir,
        )
        writer.close()  # triggers _compress_old_runs

        # Old file should be gzipped
        self.assertFalse(os.path.exists(old_file))
        self.assertTrue(os.path.exists(old_file + ".gz"))

        # Verify gzip content
        with gzip.open(old_file + ".gz", "rb") as f:
            content = f.read().decode("utf-8")
        self.assertIn("test", content)


if __name__ == "__main__":
    unittest.main()
