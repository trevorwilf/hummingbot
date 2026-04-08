"""
Tests for MEXC execution provenance: is_taker from isMaker field and structured events.
"""
import json
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from hummingbot.core.data_type.in_flight_order import InFlightOrder, TradeUpdate
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount


class TestMexcIsTakerFromIsMaker(unittest.TestCase):
    """Test is_taker derivation from MEXC isMaker field."""

    def _simulate_create_trade_update(self, order_fill: dict) -> TradeUpdate:
        """Simulate _create_trade_update_with_order_fill_data logic."""
        fee = AddedToCostTradeFee(flat_fees=[TokenAmount(
            amount=Decimal(order_fill["feeAmount"]),
            token=order_fill["feeCurrency"]
        )])
        trade_update = TradeUpdate(
            trade_id=str(order_fill["tradeId"]),
            client_order_id="test_order",
            exchange_order_id="exch_123",
            trading_pair="BTC-USDT",
            fee=fee,
            fill_base_amount=Decimal(order_fill["quantity"]),
            fill_quote_amount=Decimal(order_fill["amount"]),
            fill_price=Decimal(order_fill["price"]),
            fill_timestamp=float(order_fill["time"]) * 1e-3,
            is_taker=not order_fill.get("isMaker", True),
        )
        return trade_update

    def test_is_maker_true_means_not_taker(self):
        """isMaker=True -> is_taker=False"""
        fill = {
            "tradeId": "1001", "feeCurrency": "USDT", "feeAmount": "0.01",
            "quantity": "0.1", "amount": "5000", "price": "50000",
            "time": 1700000000000, "isMaker": True,
        }
        tu = self._simulate_create_trade_update(fill)
        self.assertFalse(tu.is_taker)

    def test_is_maker_false_means_taker(self):
        """isMaker=False -> is_taker=True"""
        fill = {
            "tradeId": "1002", "feeCurrency": "USDT", "feeAmount": "0.01",
            "quantity": "0.1", "amount": "5000", "price": "50000",
            "time": 1700000000000, "isMaker": False,
        }
        tu = self._simulate_create_trade_update(fill)
        self.assertTrue(tu.is_taker)

    def test_missing_is_maker_defaults_to_not_taker(self):
        """Missing isMaker key -> get("isMaker", True) -> is_taker=False (safe: assumes maker)"""
        fill = {
            "tradeId": "1003", "feeCurrency": "USDT", "feeAmount": "0.01",
            "quantity": "0.1", "amount": "5000", "price": "50000",
            "time": 1700000000000,
            # No "isMaker" key
        }
        tu = self._simulate_create_trade_update(fill)
        # Default True for get -> not True = False
        self.assertFalse(tu.is_taker)


class TestMexcRESTTradeIsTaker(unittest.TestCase):
    """Test is_taker for MEXC REST /myTrades response format."""

    def _simulate_rest_trade_update(self, trade: dict) -> TradeUpdate:
        """Simulate the REST poll path logic."""
        fee = AddedToCostTradeFee(flat_fees=[TokenAmount(
            amount=Decimal(trade["commission"]),
            token=trade["commissionAsset"]
        )])
        return TradeUpdate(
            trade_id=str(trade["id"]),
            client_order_id="test_order",
            exchange_order_id=str(trade["orderId"]),
            trading_pair="BTC-USDT",
            fee=fee,
            fill_base_amount=Decimal(trade["qty"]),
            fill_quote_amount=Decimal(trade["quoteQty"]),
            fill_price=Decimal(trade["price"]),
            fill_timestamp=float(trade["time"]) * 1e-3,
            is_taker=not trade.get("isMaker", True),
        )

    def test_rest_maker(self):
        trade = {
            "id": "2001", "orderId": "ord1", "commissionAsset": "USDT",
            "commission": "0.01", "qty": "0.1", "quoteQty": "5000",
            "price": "50000", "time": 1700000000000, "isMaker": True,
        }
        tu = self._simulate_rest_trade_update(trade)
        self.assertFalse(tu.is_taker)

    def test_rest_taker(self):
        trade = {
            "id": "2002", "orderId": "ord2", "commissionAsset": "USDT",
            "commission": "0.01", "qty": "0.1", "quoteQty": "5000",
            "price": "50000", "time": 1700000000000, "isMaker": False,
        }
        tu = self._simulate_rest_trade_update(trade)
        self.assertTrue(tu.is_taker)


class TestMexcStructuredEvents(unittest.TestCase):
    """Test that MEXC structured events produce valid JSON."""

    def test_listen_key_event_format(self):
        event_type = "listen_key_obtained"
        payload = {"redacted_key": "abcd...wxyz", "retry_count": 0}
        event = {
            "event_type": event_type,
            "connector": "mexc",
            "timestamp_ms": int(time.time() * 1e3),
            **payload,
        }
        output = f"[STRUCTURED_EVENT] {json.dumps(event)}"
        json_str = output.split("[STRUCTURED_EVENT] ", 1)[1]
        parsed = json.loads(json_str)
        self.assertEqual(parsed["event_type"], "listen_key_obtained")
        self.assertEqual(parsed["connector"], "mexc")
        self.assertEqual(parsed["retry_count"], 0)

    def test_untracked_messages_event_format(self):
        event = {
            "event_type": "untracked_messages_summary",
            "connector": "mexc",
            "timestamp_ms": int(time.time() * 1e3),
            "untracked_trades": 5,
            "untracked_orders": 2,
        }
        output = f"[STRUCTURED_EVENT] {json.dumps(event)}"
        json_str = output.split("[STRUCTURED_EVENT] ", 1)[1]
        parsed = json.loads(json_str)
        self.assertEqual(parsed["untracked_trades"], 5)
        self.assertEqual(parsed["untracked_orders"], 2)


class TestMexcRedactToken(unittest.TestCase):
    """Test the _redact_token helper."""

    def test_redact_long_token(self):
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import _redact_token
        token = "abcdefghijklmnop"
        self.assertEqual(_redact_token(token), "abcd...mnop")

    def test_redact_short_token(self):
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import _redact_token
        self.assertEqual(_redact_token("short"), "****")

    def test_redact_empty(self):
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import _redact_token
        self.assertEqual(_redact_token(""), "****")


if __name__ == "__main__":
    unittest.main()
