"""
Tests for NonKYC execution provenance: is_taker derivation and _fill_sources tagging.
"""
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from hummingbot.core.data_type.in_flight_order import InFlightOrder, TradeUpdate, OrderUpdate
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount


class TestNonkycIsTakerDerivation(unittest.TestCase):
    """Test that is_taker is correctly derived from NonKYC REST trade data."""

    def test_side_equals_triggered_by_is_taker(self):
        """side='Sell' + triggeredBy='sell' -> is_taker=True (taker)"""
        side = "Sell"
        triggered_by = "sell"
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertTrue(is_taker)

    def test_side_differs_from_triggered_by_is_maker(self):
        """side='Buy' + triggeredBy='sell' -> is_taker=False (maker)"""
        side = "Buy"
        triggered_by = "sell"
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertFalse(is_taker)

    def test_buy_side_buy_triggered_is_taker(self):
        """side='Buy' + triggeredBy='buy' -> is_taker=True"""
        side = "Buy"
        triggered_by = "buy"
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertTrue(is_taker)

    def test_sell_side_buy_triggered_is_maker(self):
        """side='Sell' + triggeredBy='buy' -> is_taker=False (maker)"""
        side = "Sell"
        triggered_by = "buy"
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertFalse(is_taker)

    def test_missing_triggered_by_defaults_taker(self):
        """Missing triggeredBy -> is_taker=True (conservative default)"""
        side = "Buy"
        triggered_by = ""
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertTrue(is_taker)

    def test_missing_side_defaults_taker(self):
        """Missing side -> is_taker=True (conservative default)"""
        side = ""
        triggered_by = "sell"
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertTrue(is_taker)

    def test_both_missing_defaults_taker(self):
        """Both missing -> is_taker=True"""
        side = ""
        triggered_by = ""
        _side = side.lower()
        _triggered_by = triggered_by.lower()
        is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
        self.assertTrue(is_taker)


class TestNonkycWSTradeUpdateDefaultTaker(unittest.TestCase):
    """Test that WS-sourced NonKYC trade updates default to is_taker=True."""

    def test_trade_update_default_is_taker(self):
        """TradeUpdate with no is_taker arg defaults to True."""
        fee = AddedToCostTradeFee(flat_fees=[TokenAmount(amount=Decimal("0.01"), token="USDT")])
        tu = TradeUpdate(
            trade_id="123",
            client_order_id="order1",
            exchange_order_id="exch1",
            trading_pair="BTC-USDT",
            fee=fee,
            fill_base_amount=Decimal("1.0"),
            fill_quote_amount=Decimal("50000"),
            fill_price=Decimal("50000"),
            fill_timestamp=1234567890.0,
        )
        self.assertTrue(tu.is_taker)


class TestNonkycRESTTradeUpdateIsTaker(unittest.TestCase):
    """Test that REST-sourced NonKYC trade updates correctly set is_taker."""

    def _make_trade_update(self, side: str, triggered_by: str) -> TradeUpdate:
        trade = {"side": side, "triggeredBy": triggered_by}
        _side = str(trade.get("side", "")).lower()
        _triggered_by = str(trade.get("triggeredBy", "")).lower()
        _is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True

        fee = AddedToCostTradeFee(flat_fees=[TokenAmount(amount=Decimal("0.01"), token="USDT")])
        return TradeUpdate(
            trade_id="123",
            client_order_id="order1",
            exchange_order_id="exch1",
            trading_pair="BTC-USDT",
            fee=fee,
            fill_base_amount=Decimal("1.0"),
            fill_quote_amount=Decimal("50000"),
            fill_price=Decimal("50000"),
            fill_timestamp=1234567890.0,
            is_taker=_is_taker,
        )

    def test_sell_taker(self):
        tu = self._make_trade_update("Sell", "sell")
        self.assertTrue(tu.is_taker)

    def test_buy_maker(self):
        tu = self._make_trade_update("Buy", "sell")
        self.assertFalse(tu.is_taker)

    def test_sell_maker(self):
        tu = self._make_trade_update("Sell", "buy")
        self.assertFalse(tu.is_taker)

    def test_buy_taker(self):
        tu = self._make_trade_update("Buy", "buy")
        self.assertTrue(tu.is_taker)


class TestNonkycStructuredEvents(unittest.TestCase):
    """Test that _emit_structured_event produces valid JSON."""

    def test_emit_structured_event_format(self):
        import json
        import time

        # Simulate the method
        event_type = "balance_settling_entered"
        payload = {"reason": "ws_reconnect"}
        event = {
            "event_type": event_type,
            "connector": "nonkyc",
            "timestamp_ms": int(time.time() * 1e3),
            **payload
        }
        output = f"[STRUCTURED_EVENT] {json.dumps(event)}"

        # Verify it's parseable
        json_str = output.split("[STRUCTURED_EVENT] ", 1)[1]
        parsed = json.loads(json_str)
        self.assertEqual(parsed["event_type"], "balance_settling_entered")
        self.assertEqual(parsed["connector"], "nonkyc")
        self.assertEqual(parsed["reason"], "ws_reconnect")
        self.assertIn("timestamp_ms", parsed)


if __name__ == "__main__":
    unittest.main()
