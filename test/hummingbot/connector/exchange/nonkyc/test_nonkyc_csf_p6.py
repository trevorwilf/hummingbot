"""Phase 6 (CSF-V1) tests: LOG-5 structured-event logger plumbing and log template wiring."""
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock


class TestStructuredEventEmitter(unittest.TestCase):
    """Verify that _emit_structured_event writes bare JSON via hummingbot.structured_events."""

    def _make_exchange(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
        exchange = MagicMock(spec=NonkycExchange)
        exchange._emit_structured_event = NonkycExchange._emit_structured_event.__get__(exchange)
        mock_log = MagicMock()
        mock_log.info = MagicMock()
        exchange.logger = MagicMock(return_value=mock_log)
        return exchange

    def test_emitter_writes_json_to_hummingbot_structured_events_logger(self):
        # Would-have-caught: before the fix, _emit_structured_event only logged via self.logger()
        # and never via hummingbot.structured_events, so structured_file_handler never received
        # events. This test captures the hummingbot.structured_events handler directly.
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        capture_handler = _Capture()
        structured_logger = logging.getLogger("hummingbot.structured_events")
        structured_logger.addHandler(capture_handler)
        orig_level = structured_logger.level
        orig_propagate = structured_logger.propagate
        structured_logger.setLevel(logging.INFO)
        structured_logger.propagate = False

        try:
            exchange = self._make_exchange()
            exchange._emit_structured_event("test_event", {"key": "value", "amount": 42})
        finally:
            structured_logger.removeHandler(capture_handler)
            structured_logger.setLevel(orig_level)
            structured_logger.propagate = orig_propagate

        self.assertGreater(len(captured), 0, "No messages reached hummingbot.structured_events")
        parsed = json.loads(captured[0])
        self.assertEqual("test_event", parsed["event_type"])
        self.assertEqual("nonkyc", parsed["connector"])
        self.assertEqual("value", parsed["key"])
        self.assertEqual(42, parsed["amount"])
        self.assertIn("timestamp_ms", parsed)

    def test_bare_json_does_not_contain_structured_event_prefix(self):
        # The hummingbot.structured_events message must be bare JSON (no [STRUCTURED_EVENT] prefix)
        # so the JSONL file is machine-readable without pre-processing.
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        structured_logger = logging.getLogger("hummingbot.structured_events")
        structured_logger.addHandler(captured_handler := _Capture())
        orig_level = structured_logger.level
        orig_propagate = structured_logger.propagate
        structured_logger.setLevel(logging.INFO)
        structured_logger.propagate = False

        try:
            exchange = self._make_exchange()
            exchange._emit_structured_event("fill", {"pair": "ARRR/USDT"})
        finally:
            structured_logger.removeHandler(captured_handler)
            structured_logger.setLevel(orig_level)
            structured_logger.propagate = orig_propagate

        self.assertGreater(len(captured), 0)
        raw = captured[0]
        self.assertNotIn("[STRUCTURED_EVENT]", raw)
        json.loads(raw)  # must parse cleanly

    def test_source_logger_still_receives_prefixed_line(self):
        # The [STRUCTURED_EVENT] line must remain on the source logger for grep-ability.
        source_messages = []

        exchange = self._make_exchange()
        mock_src = MagicMock()
        mock_src.info = lambda m: source_messages.append(m)
        exchange.logger = MagicMock(return_value=mock_src)

        # Suppress hummingbot.structured_events during this test
        structured_logger = logging.getLogger("hummingbot.structured_events")
        orig_propagate = structured_logger.propagate
        structured_logger.propagate = False
        try:
            exchange._emit_structured_event("order_created", {"side": "buy"})
        finally:
            structured_logger.propagate = orig_propagate

        prefix_lines = [m for m in source_messages if "[STRUCTURED_EVENT]" in m]
        self.assertGreater(len(prefix_lines), 0, "[STRUCTURED_EVENT] prefix missing from source logger")


class TestLogTemplate(unittest.TestCase):
    """Verify the template v14 changes in hummingbot_logs_TEMPLATE.yml."""

    TEMPLATE_PATH = os.path.normpath(
        os.path.join(os.path.dirname(__file__),
                     "..", "..", "..", "..", "..",
                     "hummingbot", "templates", "hummingbot_logs_TEMPLATE.yml")
    )

    def _raw(self) -> str:
        with open(self.TEMPLATE_PATH, encoding="utf-8") as f:
            return f.read()

    def test_template_version_14(self):
        self.assertIn("template_version: 14", self._raw())

    def test_errors_file_handler_defined(self):
        raw = self._raw()
        self.assertIn("errors_file_handler:", raw)
        self.assertIn("errors.log", raw)

    def test_structured_events_logger_wired_to_structured_file_handler(self):
        raw = self._raw()
        lines = raw.splitlines()
        in_block = False
        for line in lines:
            if "hummingbot.structured_events:" in line:
                in_block = True
            if in_block and "handlers:" in line:
                self.assertIn("structured_file_handler", line)
                return
        self.fail("hummingbot.structured_events handlers line not found")

    def test_root_logger_wired_to_errors_file_handler(self):
        raw = self._raw()
        lines = raw.splitlines()
        in_root = False
        for line in lines:
            if line.strip() == "root:":
                in_root = True
            if in_root and "handlers:" in line:
                self.assertIn("errors_file_handler", line)
                return
        self.fail("root handlers line not found")

    def test_template_yaml_parseable(self):
        import yaml
        raw = self._raw()
        with tempfile.TemporaryDirectory() as tmpdir:
            substituted = raw.replace("$PROJECT_DIR", tmpdir).replace("$STRATEGY_FILE_PATH", "test")
            config = yaml.safe_load(substituted)
        self.assertEqual(14, config.get("template_version"))
        self.assertIn("errors_file_handler", config["handlers"])
        self.assertIn("structured_file_handler", config["handlers"])


if __name__ == "__main__":
    unittest.main()
