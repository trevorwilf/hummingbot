"""
Tests for MarketData primary key fix (auto-increment id instead of timestamp-only PK).
"""
import unittest
from decimal import Decimal

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.market_data import MarketData
from hummingbot.model.order import Order  # noqa: F401 — needed for mapper init
from hummingbot.model.order_status import OrderStatus  # noqa: F401
from hummingbot.model.trade_fill import TradeFill  # noqa: F401
from hummingbot.model.migrate_provenance_columns import migrate_market_data_pk


class TestMarketDataNewSchema(unittest.TestCase):
    """Test fresh DB creation with new schema."""

    def test_fresh_db_has_id_column(self):
        engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(engine)
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("MarketData")}
        self.assertIn("id", cols)
        self.assertIn("timestamp", cols)

    def test_multiple_rows_same_timestamp_different_pairs(self):
        """Multiple rows with same timestamp but different exchange/pair can coexist."""
        engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(engine)
        session = Session(bind=engine)

        ts = Decimal("1700000000.123456")
        for pair in ["BTC-USDT", "ETH-USDT", "ARRR-USDT"]:
            md = MarketData(
                timestamp=ts,
                exchange="test_exchange",
                trading_pair=pair,
                mid_price=Decimal("50000"),
                best_bid=Decimal("49999"),
                best_ask=Decimal("50001"),
                order_book={"bid": [], "ask": []},
            )
            session.add(md)
        session.commit()

        results = session.query(MarketData).all()
        self.assertEqual(len(results), 3)
        session.close()

    def test_multiple_rows_same_timestamp_same_pair(self):
        """Multiple rows with same timestamp AND same pair should work (different snapshots)."""
        engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(engine)
        session = Session(bind=engine)

        ts = Decimal("1700000000.123456")
        for i in range(3):
            md = MarketData(
                timestamp=ts,
                exchange="test_exchange",
                trading_pair="BTC-USDT",
                mid_price=Decimal(str(50000 + i)),
                best_bid=Decimal("49999"),
                best_ask=Decimal("50001"),
                order_book={"bid": [], "ask": []},
            )
            session.add(md)
        session.commit()

        results = session.query(MarketData).all()
        self.assertEqual(len(results), 3)
        session.close()


class TestMarketDataPKMigration(unittest.TestCase):
    """Test migration from old schema to new schema."""

    def _create_old_schema_engine(self):
        """Create engine with old MarketData schema (timestamp as PK)."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE "MarketData" (
                    timestamp REAL PRIMARY KEY NOT NULL,
                    exchange TEXT NOT NULL,
                    trading_pair TEXT NOT NULL,
                    mid_price REAL NOT NULL,
                    best_bid REAL NOT NULL,
                    best_ask REAL NOT NULL,
                    order_book TEXT
                )
            """))
            # Insert some test data
            conn.execute(text("""
                INSERT INTO "MarketData" (timestamp, exchange, trading_pair, mid_price, best_bid, best_ask)
                VALUES (1700000000.0, 'test', 'BTC-USDT', 50000, 49999, 50001)
            """))
            conn.execute(text("""
                INSERT INTO "MarketData" (timestamp, exchange, trading_pair, mid_price, best_bid, best_ask)
                VALUES (1700000001.0, 'test', 'ETH-USDT', 3000, 2999, 3001)
            """))
            conn.commit()
        return engine

    def test_migration_adds_id_column(self):
        engine = self._create_old_schema_engine()
        migrate_market_data_pk(engine)

        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("MarketData")}
        self.assertIn("id", cols)

        # Data should be preserved
        with engine.connect() as conn:
            result = conn.execute(text('SELECT COUNT(*) FROM "MarketData"'))
            count = result.scalar()
            self.assertEqual(count, 2)

    def test_migration_is_idempotent(self):
        engine = self._create_old_schema_engine()
        migrate_market_data_pk(engine)
        # Second run should be a no-op
        migrate_market_data_pk(engine)

        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("MarketData")}
        self.assertIn("id", cols)

    def test_migration_on_fresh_db_is_noop(self):
        """On a fresh DB where create_all already ran, migration is a no-op."""
        engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(engine)
        # Should not raise
        migrate_market_data_pk(engine)

        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("MarketData")}
        self.assertIn("id", cols)


if __name__ == "__main__":
    unittest.main()
