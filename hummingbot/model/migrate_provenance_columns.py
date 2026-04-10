"""
Adds provenance columns to existing TradeFill, OrderStatus, and Order tables.
Also migrates MarketData table to use auto-increment id PK.
Safe to run multiple times -- checks for column existence before adding.
Uses ALTER TABLE ADD COLUMN which is safe for PostgreSQL and SQLite.
"""
import logging
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

MIGRATIONS = [
    # (table_name, column_name, column_type_sql)
    ("TradeFill", "exchange_timestamp_ms", "BIGINT"),
    ("TradeFill", "received_timestamp_ms", "BIGINT"),
    ("TradeFill", "source_channel", "TEXT"),
    ("TradeFill", "liquidity_role", "TEXT"),
    ("TradeFill", "controller_id", "TEXT"),
    ("TradeFill", "executor_id", "TEXT"),
    ("OrderStatus", "exchange_timestamp_ms", "BIGINT"),
    ("OrderStatus", "received_timestamp_ms", "BIGINT"),
    ("OrderStatus", "source_channel", "TEXT"),
    ("Order", "trade_type", "TEXT"),
    ("Order", "controller_id", "TEXT"),
    ("Order", "executor_id", "TEXT"),
    ("TradeFill", "exchange_order_id", "TEXT"),
    ("Order", "bot_run_id", "TEXT"),
    ("Order", "level_id", "TEXT"),
    ("TradeFill", "bot_run_id", "TEXT"),
    ("TradeFill", "level_id", "TEXT"),
    ("OrderStatus", "bot_run_id", "TEXT"),
    ("OrderStatus", "exchange_order_id", "TEXT"),
    ("OrderStatus", "level_id", "TEXT"),
]


def migrate_provenance_columns(engine: Engine):
    """Add provenance columns if they don't exist. Safe to call repeatedly."""
    inspector = inspect(engine)

    for table_name, column_name, column_type in MIGRATIONS:
        if not inspector.has_table(table_name):
            continue
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        if column_name not in existing_columns:
            sql = f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {column_type}'
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
                logger.info(f"Migration: added {table_name}.{column_name} ({column_type})")
            except Exception as e:
                logger.warning(f"Migration: failed to add {table_name}.{column_name}: {e}")


def migrate_market_data_pk(engine: Engine):
    """
    Migrate MarketData table to use auto-increment id instead of timestamp-only PK.
    Safe to call repeatedly -- checks for column existence before modifying.
    """
    inspector = inspect(engine)
    if not inspector.has_table("MarketData"):
        return

    existing_columns = {col["name"] for col in inspector.get_columns("MarketData")}
    if "id" in existing_columns:
        return  # Already migrated

    dialect = engine.dialect.name

    try:
        with engine.connect() as conn:
            if dialect == "postgresql":
                conn.execute(text('ALTER TABLE "MarketData" ADD COLUMN id BIGSERIAL'))
                pk_result = conn.execute(text("""
                    SELECT constraint_name FROM information_schema.table_constraints
                    WHERE table_name = 'MarketData' AND constraint_type = 'PRIMARY KEY'
                """))
                for row in pk_result:
                    conn.execute(text(f'ALTER TABLE "MarketData" DROP CONSTRAINT "{row[0]}"'))
                conn.execute(text('ALTER TABLE "MarketData" ADD PRIMARY KEY (id)'))
                conn.commit()
                logger.info("Migration: MarketData PK changed to auto-increment id (PostgreSQL)")
            elif dialect == "sqlite":
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS "MarketData_new" (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        exchange TEXT NOT NULL,
                        trading_pair TEXT NOT NULL,
                        mid_price REAL NOT NULL,
                        best_bid REAL NOT NULL,
                        best_ask REAL NOT NULL,
                        order_book TEXT
                    )
                """))
                conn.execute(text("""
                    INSERT INTO "MarketData_new" (timestamp, exchange, trading_pair, mid_price, best_bid, best_ask, order_book)
                    SELECT timestamp, exchange, trading_pair, mid_price, best_bid, best_ask, order_book
                    FROM "MarketData"
                """))
                conn.execute(text('DROP TABLE "MarketData"'))
                conn.execute(text('ALTER TABLE "MarketData_new" RENAME TO "MarketData"'))
                conn.commit()
                logger.info("Migration: MarketData PK changed to auto-increment id (SQLite)")
    except Exception as e:
        logger.warning(f"Migration: MarketData PK migration failed (non-fatal): {e}")
