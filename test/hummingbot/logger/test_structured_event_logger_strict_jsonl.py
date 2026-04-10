"""
Test that the StructuredEventLogger produces strict JSONL: every line is valid JSON,
no prefixed lines, no duplicates from dual-write.
"""
import json
import os
import tempfile
import unittest

from hummingbot.logger.structured_event_logger import StructuredEventLogger, get_structured_logger


class TestStrictJSONLOutput(unittest.TestCase):
    """Under real usage, JSONL file must contain only valid JSON lines."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        StructuredEventLogger._instance = None
        StructuredEventLogger._initialized = False
        self.logger = StructuredEventLogger()
        self.logger.setup(log_dir=self.tmpdir)
        self.logger.set_bot_run_id("strict-test-run")

    def tearDown(self):
        for h in self.logger._logger.handlers[:]:
            h.close()
            self.logger._logger.removeHandler(h)
        StructuredEventLogger._instance = None
        StructuredEventLogger._initialized = False

    def test_all_lines_valid_json_no_prefix(self):
        """Emit various events, verify every line is strict JSON."""
        self.logger.emit("order_created", order_id="o1", price="100")
        self.logger.emit("trade_fill_persisted", order_id="o1", amount="0.5")
        self.logger.emit("bot_run_started", strategy_name="pmm")
        self.logger.emit("order_cancel_requested", order_id="o2")
        self.logger.emit("connector_fill_received", trade_id="t1")
        for h in self.logger._logger.handlers:
            h.flush()

        path = os.path.join(self.tmpdir, "structured_events.jsonl")
        with open(path, "r") as f:
            lines = [l for l in f if l.strip()]

        self.assertEqual(len(lines), 5, "Expected exactly 5 lines")
        for i, line in enumerate(lines):
            # Must parse as JSON
            event = json.loads(line)
            # Must not start with a prefix like [STRUCTURED_EVENT]
            self.assertFalse(line.strip().startswith("["),
                             f"Line {i} has unexpected prefix")
            # Must have required envelope fields
            self.assertIn("event_id", event)
            self.assertIn("event_type", event)
            self.assertIn("schema_name", event)
            self.assertEqual(event["bot_run_id"], "strict-test-run")

    def test_no_duplicate_lines(self):
        """Each emit() should produce exactly one line."""
        for i in range(20):
            self.logger.emit("test_event", index=i)
        for h in self.logger._logger.handlers:
            h.flush()

        path = os.path.join(self.tmpdir, "structured_events.jsonl")
        with open(path, "r") as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 20)


if __name__ == "__main__":
    unittest.main()
