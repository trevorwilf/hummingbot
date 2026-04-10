"""
PostgreSQL integration tests. Skipped unless HBOT_TEST_PG_URL is set.
Run with:
    HBOT_TEST_PG_URL="postgresql+psycopg2://hbot:pass@localhost/hbot_test" python -m pytest test/hummingbot/persistence/test_pg_integration.py -v
"""
import os
import time
import unittest
import uuid

import pytest

PG_TEST_URL = os.environ.get("HBOT_TEST_PG_URL")


@pytest.mark.skipif(not PG_TEST_URL, reason="Set HBOT_TEST_PG_URL to run PostgreSQL integration tests")
class TestPgIntegration(unittest.TestCase):
    """PostgreSQL integration tests for lifecycle models."""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from hummingbot.model import HummingbotBase
        cls.engine = create_engine(PG_TEST_URL)
        HummingbotBase.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_ole_insert_and_query(self):
        from sqlalchemy.orm import Session
        from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
        event_id = str(uuid.uuid4())
        session = Session(bind=self.engine)
        ole = OrderLifecycleEvent(
            event_id=event_id,
            event_type="fill",
            event_version=1,
            emitted_ts_ms=int(time.time() * 1e3),
            connector="nonkyc",
            trading_pair="BTC-USDT",
        )
        session.add(ole)
        session.commit()
        result = session.query(OrderLifecycleEvent).filter(
            OrderLifecycleEvent.event_id == event_id
        ).one()
        self.assertEqual(result.event_type, "fill")
        session.close()

    def test_bot_run_insert_and_update(self):
        from sqlalchemy.orm import Session
        from hummingbot.model.bot_run import BotRun
        run_id = str(uuid.uuid4())
        session = Session(bind=self.engine)
        br = BotRun(id=run_id, started_ts_ms=int(time.time() * 1e3))
        session.add(br)
        session.commit()
        br = session.query(BotRun).filter(BotRun.id == run_id).one()
        br.ended_ts_ms = int(time.time() * 1e3)
        br.stop_reason = "test"
        session.commit()
        session.close()

    def test_dedup_on_conflict(self):
        from sqlalchemy.orm import Session
        from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
        event_id = str(uuid.uuid4())
        session = Session(bind=self.engine)
        ole1 = OrderLifecycleEvent(
            event_id=event_id, event_type="fill", event_version=1,
            emitted_ts_ms=int(time.time() * 1e3), connector="x", trading_pair="A-B",
        )
        session.add(ole1)
        session.commit()
        # Second insert should fail due to unique constraint
        ole2 = OrderLifecycleEvent(
            event_id=event_id, event_type="fill", event_version=1,
            emitted_ts_ms=int(time.time() * 1e3), connector="x", trading_pair="A-B",
        )
        session.add(ole2)
        with self.assertRaises(Exception):
            session.commit()
        session.rollback()
        session.close()


if __name__ == "__main__":
    unittest.main()
