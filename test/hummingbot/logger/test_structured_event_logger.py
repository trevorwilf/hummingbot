"""
Unit tests for StructuredEventLogger singleton and JSONL emission.
"""
import json
import os
import tempfile
import threading
import unittest

from hummingbot.logger.structured_event_logger import StructuredEventLogger, get_structured_logger


class TestStructuredEventLoggerSingleton(unittest.TestCase):
    """Test singleton behavior."""

    def test_singleton(self):
        a = get_structured_logger()
        b = get_structured_logger()
        self.assertIs(a, b)

    def test_session_id_stable(self):
        logger = get_structured_logger()
        self.assertEqual(len(logger._session_id), 8)


class TestStructuredEventLoggerEmit(unittest.TestCase):
    """Test event emission to JSONL file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Reset singleton for clean test
        StructuredEventLogger._instance = None
        StructuredEventLogger._initialized = False
        self.logger = StructuredEventLogger()
        self.logger.setup(log_dir=self.tmpdir)

    def tearDown(self):
        # Clean up handlers
        for h in self.logger._logger.handlers[:]:
            h.close()
            self.logger._logger.removeHandler(h)
        # Reset singleton
        StructuredEventLogger._instance = None
        StructuredEventLogger._initialized = False

    def _read_events(self):
        path = os.path.join(self.tmpdir, "structured_events.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_emit_writes_valid_jsonl(self):
        self.logger.emit("test_event", key="value")
        # Force flush
        for h in self.logger._logger.handlers:
            h.flush()
        events = self._read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "test_event")
        self.assertEqual(events[0]["key"], "value")

    def test_emit_includes_required_fields(self):
        self.logger.emit("test_event")
        for h in self.logger._logger.handlers:
            h.flush()
        events = self._read_events()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIn("timestamp_ms", ev)
        self.assertIn("event_version", ev)
        self.assertIn("session_id", ev)
        self.assertEqual(ev["event_version"], 1)
        self.assertIsInstance(ev["timestamp_ms"], int)

    def test_emit_never_raises_with_bad_payload(self):
        # Should not raise even with problematic types
        self.logger.emit("test_event", obj=object())
        # default=str should handle it

    def test_emit_multiple_events(self):
        for i in range(5):
            self.logger.emit("test_event", index=i)
        for h in self.logger._logger.handlers:
            h.flush()
        events = self._read_events()
        self.assertEqual(len(events), 5)

    def test_concurrent_emit(self):
        """Test thread safety."""
        errors = []

        def emit_events(n):
            try:
                for i in range(10):
                    self.logger.emit("thread_test", thread=n, index=i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emit_events, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        for h in self.logger._logger.handlers:
            h.flush()
        events = self._read_events()
        self.assertEqual(len(events), 40)  # 4 threads * 10 events


class TestStructuredEventLoggerLazySetup(unittest.TestCase):
    """Test that emit calls setup lazily if not already set up."""

    def setUp(self):
        StructuredEventLogger._instance = None
        StructuredEventLogger._initialized = False

    def tearDown(self):
        logger = StructuredEventLogger()
        for h in logger._logger.handlers[:]:
            h.close()
            logger._logger.removeHandler(h)
        StructuredEventLogger._instance = None
        StructuredEventLogger._initialized = False

    def test_emit_without_explicit_setup(self):
        logger = StructuredEventLogger()
        # Should not raise, should lazily setup
        logger.emit("lazy_test", foo="bar")
        self.assertTrue(logger._setup)


if __name__ == "__main__":
    unittest.main()
