"""
Tests for the LifecycleEvent canonical dataclass.
"""
import json
import unittest
import uuid

from hummingbot.persistence.lifecycle_event import LifecycleEvent
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent


class TestLifecycleEventDataclass(unittest.TestCase):
    """Test LifecycleEvent serialization methods."""

    def test_to_dict_drops_none(self):
        evt = LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            price="50000",
            amount="0.1",
        )
        d = evt.to_dict()
        self.assertNotIn("exchange_ts_ms", d)
        self.assertNotIn("fee_json", d)
        self.assertIn("event_type", d)
        self.assertIn("price", d)
        self.assertEqual(d["event_type"], "fill")

    def test_to_dict_includes_all_set_fields(self):
        evt = LifecycleEvent(
            event_type="submit_acked",
            connector="mexc",
            trading_pair="ETH-USDT",
            client_order_id="order_123",
            trade_type="BUY",
            order_type="LIMIT",
            price="3000",
            amount="1.5",
            liquidity_role="maker",
        )
        d = evt.to_dict()
        self.assertEqual(d["client_order_id"], "order_123")
        self.assertEqual(d["liquidity_role"], "maker")
        self.assertIn("event_id", d)
        self.assertIn("emitted_ts_ms", d)

    def test_to_db_kwargs_includes_all_fields(self):
        evt = LifecycleEvent(
            event_type="cancel_confirmed",
            connector="nonkyc",
            trading_pair="BTC-USDT",
        )
        kwargs = evt.to_db_kwargs()
        # Should include None fields (for DB defaults)
        self.assertIn("exchange_ts_ms", kwargs)
        self.assertIsNone(kwargs["exchange_ts_ms"])
        self.assertEqual(kwargs["event_type"], "cancel_confirmed")

    def test_to_db_kwargs_can_construct_orm_row(self):
        evt = LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            client_order_id="order_456",
            price="50000",
            amount="0.1",
            fee_json={"percent": 0.001},
        )
        kwargs = evt.to_db_kwargs()
        ole = OrderLifecycleEvent(**kwargs)
        self.assertEqual(ole.event_type, "fill")
        self.assertEqual(ole.client_order_id, "order_456")
        self.assertEqual(ole.price, "50000")
        self.assertEqual(ole.fee_json, {"percent": 0.001})

    def test_event_id_is_uuid(self):
        evt = LifecycleEvent(event_type="test", connector="x", trading_pair="Y-Z")
        # Should be a valid UUID
        parsed = uuid.UUID(evt.event_id)
        self.assertIsNotNone(parsed)

    def test_emitted_ts_ms_auto_populated(self):
        evt = LifecycleEvent(event_type="test", connector="x", trading_pair="Y-Z")
        self.assertIsInstance(evt.emitted_ts_ms, int)
        self.assertGreater(evt.emitted_ts_ms, 0)

    def test_round_trip_to_dict_json_parse(self):
        """Test LifecycleEvent -> to_dict -> JSON -> parse -> fields match."""
        evt = LifecycleEvent(
            event_type="fill",
            connector="nonkyc",
            trading_pair="BTC-USDT",
            client_order_id="order_rt",
            trade_type="BUY",
            price="50000",
            amount="0.1",
            fee_json={"percent": 0.001, "flat_fees": []},
            liquidity_role="taker",
        )
        d = evt.to_dict()
        json_str = json.dumps(d, default=str)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["event_type"], "fill")
        self.assertEqual(parsed["connector"], "nonkyc")
        self.assertEqual(parsed["client_order_id"], "order_rt")
        self.assertEqual(parsed["price"], "50000")
        self.assertEqual(parsed["liquidity_role"], "taker")
        self.assertEqual(parsed["fee_json"]["percent"], 0.001)
        self.assertNotIn("exchange_ts_ms", parsed)  # None dropped


if __name__ == "__main__":
    unittest.main()
