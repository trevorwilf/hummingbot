import time
from unittest import TestCase

from hummingbot.connector.exchange.nonkyc.nonkyc_order_book import NonkycOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessageType


class NonkycOrderBookTests(TestCase):

    def test_snapshot_message_from_exchange(self):
        msg = {
            "bids": [{"price": "49900.00", "quantity": "2.000"}],
            "asks": [{"price": "50100.00", "quantity": "1.500"}],
            "symbol": "BTC/USDT",
            "sequence": 1000,
            "timestamp": "2026-02-26T12:00:00.000Z",
        }
        timestamp = time.time()
        snapshot = NonkycOrderBook.snapshot_message_from_exchange(
            msg, timestamp, metadata={"trading_pair": "BTC-USDT"}
        )

        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot.type)
        self.assertEqual("BTC-USDT", snapshot.trading_pair)
        self.assertEqual(1000, snapshot.update_id)
        self.assertEqual(1, len(snapshot.bids))
        self.assertEqual(1, len(snapshot.asks))

    def test_snapshot_message_from_ws(self):
        """WS snapshotOrderbook params have the same shape as REST snapshot."""
        params = {
            "bids": [{"price": "49900.00", "quantity": "2.000"}],
            "asks": [{"price": "50100.00", "quantity": "1.500"}],
            "symbol": "BTC/USDT",
            "sequence": 1000,
            "timestamp": "2026-02-26T12:00:00.000Z",
        }
        timestamp = time.time()
        snapshot = NonkycOrderBook.snapshot_message_from_exchange(
            params, timestamp, metadata={"trading_pair": "BTC-USDT"}
        )

        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot.type)
        self.assertEqual("BTC-USDT", snapshot.trading_pair)

    def test_diff_message_from_exchange(self):
        msg = {
            "method": "updateOrderbook",
            "params": {
                "asks": [{"price": "50100.00", "quantity": "1.500"}],
                "bids": [{"price": "49900.00", "quantity": "2.000"}],
                "symbol": "BTC/USDT",
                "timestamp": "2026-02-26T12:00:00.000Z",
                "sequence": 1001,
            }
        }
        timestamp = time.time()
        diff = NonkycOrderBook.diff_message_from_exchange(
            msg, timestamp, metadata={"trading_pair": "BTC-USDT"}
        )

        self.assertEqual(OrderBookMessageType.DIFF, diff.type)
        self.assertEqual("BTC-USDT", diff.trading_pair)
        self.assertEqual(1, len(diff.bids))
        self.assertEqual(1, len(diff.asks))

    def test_trade_message_from_exchange_with_iso_timestamp(self):
        """Test that ISO timestamp (no timestampms) is handled without KeyError."""
        msg = {
            "method": "updateTrades",
            "params": {
                "symbol": "BTC/USDT",
                "data": [
                    {
                        "id": 12345,
                        "price": "50000.00",
                        "quantity": "0.5",
                        "side": "buy",
                        "timestamp": "2022-10-19T16:34:25.041Z",
                    }
                ],
            }
        }
        trade = NonkycOrderBook.trade_message_from_exchange(
            msg, metadata={"trading_pair": "BTC-USDT"}
        )

        self.assertEqual("BTC-USDT", trade.trading_pair)
        # The timestamp should be approximately 1666197265041 ms
        expected_ms = 1666197265041.0
        self.assertAlmostEqual(expected_ms, trade.content["update_id"], delta=1.0)

    def test_trade_message_from_exchange_with_timestampms(self):
        """Backward compatibility: timestampms field should still work."""
        msg = {
            "method": "updateTrades",
            "params": {
                "symbol": "BTC/USDT",
                "data": [
                    {
                        "id": 12345,
                        "price": "50000.00",
                        "quantity": "0.5",
                        "side": "sell",
                        "timestampms": 1666197265041,
                    }
                ],
            }
        }
        trade = NonkycOrderBook.trade_message_from_exchange(
            msg, metadata={"trading_pair": "BTC-USDT"}
        )

        self.assertEqual("BTC-USDT", trade.trading_pair)
        self.assertEqual(1666197265041.0, trade.content["update_id"])

    def test_trade_messages_from_exchange_multiple(self):
        """Phase 5B: trade_messages_from_exchange returns all trades."""
        msg = {
            "method": "snapshotTrades",
            "params": {
                "symbol": "BTC/USDT",
                "data": [
                    {"id": "t1", "price": "50000.00", "quantity": "0.5", "side": "buy", "timestampms": 1666197265041},
                    {"id": "t2", "price": "50001.00", "quantity": "0.3", "side": "sell", "timestampms": 1666197265042},
                    {"id": "t3", "price": "49999.00", "quantity": "0.1", "side": "buy", "timestampms": 1666197265043},
                ],
            }
        }
        messages = NonkycOrderBook.trade_messages_from_exchange(
            msg, metadata={"trading_pair": "BTC-USDT"})

        self.assertEqual(3, len(messages))
        self.assertEqual("t1", messages[0].content["trade_id"])
        self.assertEqual("t2", messages[1].content["trade_id"])
        self.assertEqual("t3", messages[2].content["trade_id"])

    def test_trade_messages_from_exchange_empty_data(self):
        """Phase 5B: Empty data array should return empty list."""
        msg = {
            "method": "snapshotTrades",
            "params": {
                "symbol": "BTC/USDT",
                "data": [],
            }
        }
        messages = NonkycOrderBook.trade_messages_from_exchange(
            msg, metadata={"trading_pair": "BTC-USDT"})

        self.assertEqual(0, len(messages))
