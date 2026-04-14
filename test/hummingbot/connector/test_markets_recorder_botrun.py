"""Test that BotRun row creation uses the correct engine accessor."""
import unittest
from unittest.mock import MagicMock


class TestMarketsRecorderBotRun(unittest.TestCase):

    def test_engine_property_is_used_not_get_engine(self):
        """Verify markets_recorder uses .engine (property) not .get_engine() (non-existent)."""
        from hummingbot.model.sql_connection_manager import SQLConnectionManager
        self.assertTrue(
            isinstance(
                getattr(SQLConnectionManager, 'engine', None),
                property
            ),
            "SQLConnectionManager.engine must be a property"
        )
        self.assertFalse(
            hasattr(SQLConnectionManager, 'get_engine'),
            "SQLConnectionManager.get_engine should NOT exist"
        )

    def test_botrun_creation_code_path(self):
        """Verify the code at markets_recorder.py references .engine not .get_engine()."""
        import inspect
        from hummingbot.connector.markets_recorder import MarketsRecorder
        source = inspect.getsource(MarketsRecorder)
        self.assertNotIn(".get_engine()", source,
                         "markets_recorder.py must not call .get_engine()")
        self.assertIn(".engine.dialect.name", source,
                      "markets_recorder.py must use .engine.dialect.name")


if __name__ == "__main__":
    unittest.main()
