"""
Adds provenance columns to existing TradeFill, OrderStatus, and Order tables.
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
    ("Order", "controller_id", "TEXT"),
    ("Order", "executor_id", "TEXT"),
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
