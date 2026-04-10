"""
Tests for OrderLifecycleEvent model: table creation, insert, query, dedup.
"""
import time
import unittest
import uuid

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent


class TestOrderLifecycleEventModel(unittest.TestCase):
    """Test OrderLifecycleEvent table creation and basic operations."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.session = Session(bind=self.engine)

    def tearDown(self):
        self.session.close()

    def test_table_exists(self):
        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("OrderLifecycleEvent"))

    def test_columns_present(self):
        inspector = inspect(self.engine)
        cols = {c["name"] for c in inspector.get_columns("OrderLifecycleEvent")}
        for expected in ("id", "event_id", "bot_run_id", "event_type", "event_version",
                         "emitted_ts_ms", "exchange_ts_ms", "received_ts_ms",
                         "connector", "trading_pair", "client_order_id",
                         "exchange_order_id", "exchange_trade_id",
                         "controller_id", "executor_id", "level_id",
                         "trade_type", "order_type", "position_action",
                         "price", "amount", "cum_fill_qty",
                         "fee_json", "fee_in_quote", "liquidity_role",
                         "source_channel", "best_bid", "best_ask",
                         "mid_price", "spread_bps", "payload"):
            self.assertIn(expected, cols, f"Missing column: {expected}")

    def test_insert_and_query(self):
        event_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1e3)
        ole = OrderLifecycleEvent(
            event_id=event_id,
            bot_run_id="run-001",
            event_type="submit_acked",
            event_version=1,
            emitted_ts_ms=now_ms,
            connector="nonkyc",
            trading_pair="BTC-USDT",
            client_order_id="order_001",
            trade_type="BUY",
            order_type="LIMIT",
            price="50000.00",
            amount="0.1",
        )
        self.session.add(ole)
        self.session.commit()

        result = self.session.query(OrderLifecycleEvent).filter(
            OrderLifecycleEvent.event_id == event_id
        ).one()
        self.assertEqual(result.event_type, "submit_acked")
        self.assertEqual(result.connector, "nonkyc")
        self.assertEqual(result.client_order_id, "order_001")
        self.assertIsNotNone(result.id)  # autoincrement

    def test_event_id_unique_constraint(self):
        event_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1e3)
        ole1 = OrderLifecycleEvent(
            event_id=event_id, event_type="fill", event_version=1,
            emitted_ts_ms=now_ms, connector="nonkyc", trading_pair="BTC-USDT",
        )
        ole2 = OrderLifecycleEvent(
            event_id=event_id, event_type="fill", event_version=1,
            emitted_ts_ms=now_ms, connector="nonkyc", trading_pair="BTC-USDT",
        )
        self.session.add(ole1)
        self.session.commit()
        self.session.add(ole2)
        with self.assertRaises(Exception):
            self.session.commit()

    def test_indexes_created(self):
        inspector = inspect(self.engine)
        indexes = inspector.get_indexes("OrderLifecycleEvent")
        index_names = {idx["name"] for idx in indexes}
        for expected in ("ole_client_order_id_idx", "ole_bot_run_id_idx",
                         "ole_event_type_idx", "ole_connector_pair_idx",
                         "ole_emitted_ts_idx"):
            self.assertIn(expected, index_names, f"Missing index: {expected}")


if __name__ == "__main__":
    unittest.main()
