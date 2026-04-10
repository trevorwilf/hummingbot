"""
Tests for provenance columns on TradeFill, OrderStatus, and Order models,
and for the migrate_provenance_columns migration helper.
"""
import unittest
from sqlalchemy import create_engine, inspect, text, Column, BigInteger, Integer, Text
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.trade_fill import TradeFill
from hummingbot.model.order_status import OrderStatus
from hummingbot.model.order import Order
from hummingbot.model.migrate_provenance_columns import migrate_provenance_columns, MIGRATIONS


class TestProvenanceColumnsExist(unittest.TestCase):
    """Test that provenance columns are declared on the SQLAlchemy models."""

    def test_trade_fill_has_provenance_columns(self):
        col_names = {c.name for c in TradeFill.__table__.columns}
        for expected in ("exchange_order_id", "exchange_timestamp_ms", "received_timestamp_ms",
                         "source_channel", "liquidity_role",
                         "controller_id", "executor_id",
                         "bot_run_id", "level_id"):
            self.assertIn(expected, col_names)

    def test_order_status_has_provenance_columns(self):
        col_names = {c.name for c in OrderStatus.__table__.columns}
        for expected in ("exchange_timestamp_ms", "received_timestamp_ms", "source_channel",
                         "bot_run_id", "exchange_order_id", "level_id"):
            self.assertIn(expected, col_names)

    def test_order_has_provenance_columns(self):
        col_names = {c.name for c in Order.__table__.columns}
        for expected in ("trade_type", "controller_id", "executor_id",
                         "bot_run_id", "level_id"):
            self.assertIn(expected, col_names)

    def test_trade_fill_provenance_columns_are_nullable(self):
        for col in TradeFill.__table__.columns:
            if col.name in ("exchange_timestamp_ms", "received_timestamp_ms",
                            "source_channel", "liquidity_role",
                            "controller_id", "executor_id"):
                self.assertTrue(col.nullable, f"{col.name} should be nullable")


class TestProvenanceColumnsInMethods(unittest.TestCase):
    """Test that provenance columns appear in repr, export, bounty, and pandas."""

    def test_attribute_names_for_file_export(self):
        attrs = TradeFill.attribute_names_for_file_export()
        for expected in ("exchange_order_id", "exchange_timestamp_ms", "received_timestamp_ms",
                         "source_channel", "liquidity_role",
                         "controller_id", "executor_id",
                         "bot_run_id", "level_id"):
            self.assertIn(expected, attrs)

    def test_to_pandas_columns(self):
        # to_pandas with empty list should return a DataFrame with the right columns
        df = TradeFill.to_pandas([])
        for expected in ("Exchange_Order_Id", "Exchange_Timestamp_ms", "Received_Timestamp_ms",
                         "Source_Channel", "Liquidity_Role",
                         "Controller_Id", "Executor_Id",
                         "Bot_Run_Id", "Level_Id"):
            self.assertIn(expected, df.columns)


class TestMigrateProvenanceColumns(unittest.TestCase):
    """Test the migration helper on an in-memory SQLite database."""

    def _create_engine_with_old_schema(self):
        """Create an engine with tables that lack provenance columns."""
        engine = create_engine("sqlite:///:memory:")
        # Create minimal tables WITHOUT provenance columns
        with engine.connect() as conn:
            conn.execute(text('''
                CREATE TABLE "Order" (
                    id TEXT PRIMARY KEY,
                    config_file_path TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    base_asset TEXT NOT NULL,
                    quote_asset TEXT NOT NULL,
                    creation_timestamp BIGINT NOT NULL,
                    order_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    leverage INTEGER NOT NULL DEFAULT 1,
                    price REAL NOT NULL,
                    last_status TEXT NOT NULL,
                    last_update_timestamp BIGINT NOT NULL,
                    exchange_order_id TEXT,
                    position TEXT
                )
            '''))
            conn.execute(text('''
                CREATE TABLE "TradeFill" (
                    config_file_path TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    base_asset TEXT NOT NULL,
                    quote_asset TEXT NOT NULL,
                    timestamp BIGINT NOT NULL,
                    order_id TEXT NOT NULL,
                    trade_type TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    leverage INTEGER NOT NULL DEFAULT 1,
                    trade_fee TEXT NOT NULL,
                    trade_fee_in_quote REAL,
                    exchange_trade_id TEXT NOT NULL,
                    position TEXT,
                    PRIMARY KEY (market, order_id, exchange_trade_id)
                )
            '''))
            conn.execute(text('''
                CREATE TABLE "OrderStatus" (
                    id INTEGER PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    timestamp BIGINT NOT NULL,
                    status TEXT NOT NULL
                )
            '''))
            conn.commit()
        return engine

    def test_migration_on_fresh_db(self):
        """create_all already adds the columns, so migration is a no-op."""
        engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(engine)
        # Should not raise
        migrate_provenance_columns(engine)

        inspector = inspect(engine)
        tf_cols = {c["name"] for c in inspector.get_columns("TradeFill")}
        self.assertIn("exchange_timestamp_ms", tf_cols)
        self.assertIn("liquidity_role", tf_cols)

    def test_migration_adds_missing_columns(self):
        """On an old schema, migration should add the new columns."""
        engine = self._create_engine_with_old_schema()
        inspector = inspect(engine)

        # Verify columns are missing before migration
        tf_cols_before = {c["name"] for c in inspector.get_columns("TradeFill")}
        self.assertNotIn("exchange_timestamp_ms", tf_cols_before)

        migrate_provenance_columns(engine)

        # Re-inspect after migration
        inspector = inspect(engine)
        tf_cols_after = {c["name"] for c in inspector.get_columns("TradeFill")}
        self.assertIn("exchange_timestamp_ms", tf_cols_after)
        self.assertIn("received_timestamp_ms", tf_cols_after)
        self.assertIn("source_channel", tf_cols_after)
        self.assertIn("liquidity_role", tf_cols_after)
        self.assertIn("controller_id", tf_cols_after)
        self.assertIn("executor_id", tf_cols_after)

        os_cols = {c["name"] for c in inspector.get_columns("OrderStatus")}
        self.assertIn("exchange_timestamp_ms", os_cols)
        self.assertIn("received_timestamp_ms", os_cols)
        self.assertIn("source_channel", os_cols)

        self.assertIn("exchange_order_id", tf_cols_after)

        o_cols = {c["name"] for c in inspector.get_columns("Order")}
        self.assertIn("trade_type", o_cols)
        self.assertIn("controller_id", o_cols)
        self.assertIn("executor_id", o_cols)

    def test_migration_is_idempotent(self):
        """Running migration twice should not raise."""
        engine = self._create_engine_with_old_schema()
        migrate_provenance_columns(engine)
        # Second run should be a no-op
        migrate_provenance_columns(engine)

        inspector = inspect(engine)
        tf_cols = {c["name"] for c in inspector.get_columns("TradeFill")}
        self.assertIn("exchange_timestamp_ms", tf_cols)


if __name__ == "__main__":
    unittest.main()
