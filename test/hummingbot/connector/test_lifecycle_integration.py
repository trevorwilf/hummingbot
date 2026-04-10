"""
Integration test: simulate complete order lifecycle through MarketsRecorder + LifecycleWriter.
Verifies submit_acked, fill, completed events in both DB and JSONL.
"""
import json
import os
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.order import Order
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
from hummingbot.model.order_status import OrderStatus
from hummingbot.model.trade_fill import TradeFill
from hummingbot.persistence.lifecycle_event import LifecycleEvent
from hummingbot.persistence.lifecycle_writer import LifecycleWriter


class FakeSQLManager:
    def __init__(self, engine):
        self._engine = engine

    def get_engine(self):
        return self._engine

    def get_new_session(self):
        return Session(bind=self._engine)


class TestLifecycleIntegration(unittest.TestCase):
    """Simulate a complete order lifecycle through LifecycleWriter."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.sql_mgr = FakeSQLManager(self.engine)
        self.bot_run_id = str(uuid.uuid4())
        self.writer = LifecycleWriter(
            sql_manager=self.sql_mgr,
            bot_run_id=self.bot_run_id,
            log_dir=self.tmpdir,
        )
        self.client_order_id = f"buy-btc-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        try:
            self.writer.close()
        except Exception:
            pass

    def test_full_order_lifecycle(self):
        """submit_acked -> fill -> completed, all events share client_order_id and bot_run_id."""
        # 1. submit_acked
        self.writer.write(LifecycleEvent(
            event_type="submit_acked",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            client_order_id=self.client_order_id,
            exchange_order_id="EX123",
            trade_type="BUY",
            order_type="LIMIT",
            price="50000",
            amount="0.1",
        ))

        # 2. fill
        self.writer.write(LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            client_order_id=self.client_order_id,
            exchange_order_id="EX123",
            exchange_trade_id="T001",
            trade_type="BUY",
            order_type="LIMIT",
            price="50000",
            amount="0.1",
            cum_fill_qty="0.1",
            fee_json={"percent": 0, "flat_fees": [{"amount": "0.05", "token": "USDT"}]},
            liquidity_role="maker",
            source_channel="ws",
        ))

        # 3. completed
        self.writer.write(LifecycleEvent(
            event_type="completed",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            client_order_id=self.client_order_id,
            exchange_order_id="EX123",
            trade_type="BUY",
        ))

        # Verify DB
        session = Session(bind=self.engine)
        events = session.query(OrderLifecycleEvent).filter(
            OrderLifecycleEvent.client_order_id == self.client_order_id
        ).order_by(OrderLifecycleEvent.id).all()
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].event_type, "submit_acked")
        self.assertEqual(events[1].event_type, "fill")
        self.assertEqual(events[2].event_type, "completed")

        # All share same bot_run_id
        for ev in events:
            self.assertEqual(ev.bot_run_id, self.bot_run_id)

        # All event_ids are unique
        event_ids = [ev.event_id for ev in events]
        self.assertEqual(len(event_ids), len(set(event_ids)))
        session.close()

        # Verify JSONL
        with open(self.writer.jsonl_path, "r") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(line["bot_run_id"], self.bot_run_id)
            self.assertEqual(line["schema_name"], "lifecycle_v1")
            self.assertIn("event_id", line)

        # All lines parse as valid JSON (strict JSONL)
        with open(self.writer.jsonl_path, "r") as f:
            for i, raw_line in enumerate(f):
                if raw_line.strip():
                    json.loads(raw_line)  # should not raise

    def test_cancel_lifecycle(self):
        """submit_acked -> cancel_confirmed."""
        self.writer.write(LifecycleEvent(
            event_type="submit_acked",
            connector="mexc",
            trading_pair="ETH-USDT",
            client_order_id=self.client_order_id,
            trade_type="SELL",
            order_type="LIMIT",
            price="3000",
            amount="1.0",
        ))
        self.writer.write(LifecycleEvent(
            event_type="cancel_confirmed",
            connector="mexc",
            trading_pair="ETH-USDT",
            client_order_id=self.client_order_id,
        ))

        session = Session(bind=self.engine)
        events = session.query(OrderLifecycleEvent).filter(
            OrderLifecycleEvent.client_order_id == self.client_order_id
        ).all()
        self.assertEqual(len(events), 2)
        types = {e.event_type for e in events}
        self.assertEqual(types, {"submit_acked", "cancel_confirmed"})
        session.close()

    def test_writer_counters_after_lifecycle(self):
        for _ in range(3):
            self.writer.write(LifecycleEvent(
                event_type="fill", connector="x", trading_pair="A-B",
            ))
        self.assertEqual(self.writer.events_total, 3)
        self.assertEqual(self.writer.jsonl_writes_ok, 3)
        self.assertEqual(self.writer.db_writes_ok, 3)


if __name__ == "__main__":
    unittest.main()
