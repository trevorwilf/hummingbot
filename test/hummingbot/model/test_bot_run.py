"""
Tests for BotRun model: table creation, insert, and query.
"""
import time
import unittest

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.bot_run import BotRun


class TestBotRunModel(unittest.TestCase):
    """Test BotRun table creation and basic CRUD."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.session = Session(bind=self.engine)

    def tearDown(self):
        self.session.close()

    def test_table_exists(self):
        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("BotRun"))

    def test_columns_present(self):
        inspector = inspect(self.engine)
        cols = {c["name"] for c in inspector.get_columns("BotRun")}
        for expected in ("id", "started_ts_ms", "ended_ts_ms", "strategy_name",
                         "config_file_path", "config_hash", "connectors",
                         "trading_pairs", "db_backend", "stop_reason",
                         "host_name", "git_sha"):
            self.assertIn(expected, cols, f"Missing column: {expected}")

    def test_insert_and_query(self):
        now_ms = int(time.time() * 1e3)
        bot_run = BotRun(
            id="test-run-001",
            started_ts_ms=now_ms,
            strategy_name="test_strategy",
            config_file_path="test.yml",
            connectors=["nonkyc", "mexc"],
            trading_pairs=["BTC-USDT", "ETH-USDT"],
            db_backend="sqlite",
            host_name="test-host",
        )
        self.session.add(bot_run)
        self.session.commit()

        result = self.session.query(BotRun).filter(BotRun.id == "test-run-001").one()
        self.assertEqual(result.strategy_name, "test_strategy")
        self.assertEqual(result.connectors, ["nonkyc", "mexc"])
        self.assertIsNone(result.ended_ts_ms)
        self.assertIsNone(result.stop_reason)

    def test_update_on_stop(self):
        now_ms = int(time.time() * 1e3)
        bot_run = BotRun(
            id="test-run-002",
            started_ts_ms=now_ms,
        )
        self.session.add(bot_run)
        self.session.commit()

        # Simulate stop
        result = self.session.query(BotRun).filter(BotRun.id == "test-run-002").one()
        result.ended_ts_ms = now_ms + 5000
        result.stop_reason = "operator_stop"
        self.session.commit()

        updated = self.session.query(BotRun).filter(BotRun.id == "test-run-002").one()
        self.assertEqual(updated.ended_ts_ms, now_ms + 5000)
        self.assertEqual(updated.stop_reason, "operator_stop")


if __name__ == "__main__":
    unittest.main()
