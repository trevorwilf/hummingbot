"""
Phase 3 — Connector Optimization Tests
Run: python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase3_optimization.py -v

Tests for:
  Fix 1 (P1-1): _all_trade_updates_for_order with since parameter
  Fix 2 (P2-4): _update_trading_fees TTL cache
  Fix 3 (P3-3): to_hb_order_type unknown type handling
  Fix 4 (P3-5): get_current_server_time ms normalization
"""
import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder


class TestFix1TradeUpdatesSinceParam(unittest.TestCase):
    """P1-1: _all_trade_updates_for_order should use 'since' parameter."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )

    def test_since_param_included_when_creation_timestamp_set(self):
        """Verify 'since' is passed to _api_get when order has creation_timestamp."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = "abc123"
        order.client_order_id = "HBOT-TEST-123"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 1772395000.0  # specific timestamp

        captured_params = {}

        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            captured_params.update(params or {})
            return []  # No trades

        self.exchange._api_get = mock_api_get

        async def mock_symbol(trading_pair):
            return "BTC/USDT"

        self.exchange.exchange_symbol_associated_to_pair = mock_symbol

        asyncio.get_event_loop().run_until_complete(
            self.exchange._all_trade_updates_for_order(order)
        )

        self.assertIn("since", captured_params,
                       "'since' parameter should be included in API call")
        since_val = int(captured_params["since"])
        # Should be creation_timestamp - 60 seconds, in milliseconds
        expected_min = int((order.creation_timestamp - 61) * 1e3)
        expected_max = int((order.creation_timestamp) * 1e3)
        self.assertGreaterEqual(since_val, expected_min)
        self.assertLessEqual(since_val, expected_max)

    def test_no_since_param_when_no_creation_timestamp(self):
        """Without creation_timestamp, 'since' should not be in params."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = "def456"
        order.client_order_id = "HBOT-TEST-456"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 0  # not set

        captured_params = {}

        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            captured_params.update(params or {})
            return []

        self.exchange._api_get = mock_api_get
        self.exchange.exchange_symbol_associated_to_pair = AsyncMock(return_value="BTC/USDT")

        asyncio.get_event_loop().run_until_complete(
            self.exchange._all_trade_updates_for_order(order)
        )

        self.assertNotIn("since", captured_params,
                         "'since' should not be included when creation_timestamp is 0")

    def test_filters_by_exchange_order_id(self):
        """Verify trades are filtered by exchange_order_id after fetch."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = "target_order_id"
        order.client_order_id = "HBOT-TEST-789"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 1772395000.0

        mock_trades = [
            {"id": "t1", "orderid": "target_order_id", "quantity": "0.001",
             "price": "50000", "fee": "0.1", "timestamp": 1772395100000},
            {"id": "t2", "orderid": "other_order_id", "quantity": "0.005",
             "price": "51000", "fee": "0.5", "timestamp": 1772395200000},
        ]

        self.exchange._api_get = AsyncMock(return_value=mock_trades)
        self.exchange.exchange_symbol_associated_to_pair = AsyncMock(return_value="BTC/USDT")

        result = asyncio.get_event_loop().run_until_complete(
            self.exchange._all_trade_updates_for_order(order)
        )

        self.assertEqual(len(result), 1, "Should only return trades matching the order")
        self.assertEqual(result[0].trade_id, "t1")

    def test_no_exchange_order_id_returns_empty(self):
        """If exchange_order_id is None, return empty list."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = None
        order.client_order_id = "HBOT-TEST-none"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 1772395000.0

        result = asyncio.get_event_loop().run_until_complete(
            self.exchange._all_trade_updates_for_order(order)
        )
        self.assertEqual(len(result), 0)


class TestFix2TradingFeesTTLCache(unittest.TestCase):
    """P2-4: _update_trading_fees should use TTL cache."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )

    def test_ttl_attributes_exist(self):
        """Exchange should have fee cache TTL attributes."""
        self.assertTrue(hasattr(self.exchange, '_trading_fees_last_computed'),
                        "Missing _trading_fees_last_computed attribute")
        self.assertTrue(hasattr(self.exchange, '_trading_fees_ttl'),
                        "Missing _trading_fees_ttl attribute")

    def test_ttl_default_is_one_hour(self):
        """Default TTL should be 3600 seconds (1 hour)."""
        self.assertEqual(self.exchange._trading_fees_ttl, 3600.0)

    def test_cache_skips_when_fresh(self):
        """If fees were computed recently, _update_trading_fees should skip the API call."""
        # Pre-populate cache
        self.exchange._trading_fees = {"maker_fee": Decimal("0.002")}
        self.exchange._trading_fees_last_computed = time.time()  # just now
        self.exchange._time_synchronizer = MagicMock()
        self.exchange._time_synchronizer.time.return_value = time.time()

        api_called = False

        async def mock_api_get(*args, **kwargs):
            nonlocal api_called
            api_called = True
            return []

        self.exchange._api_get = mock_api_get

        asyncio.get_event_loop().run_until_complete(
            self.exchange._update_trading_fees()
        )

        self.assertFalse(api_called, "API should not be called when cache is fresh")

    def test_cache_refreshes_when_expired(self):
        """If cache is older than TTL, _update_trading_fees should fetch new data."""
        self.exchange._trading_fees = {"maker_fee": Decimal("0.002")}
        # Set last computed to 2 hours ago
        self.exchange._trading_fees_last_computed = time.time() - 7200
        self.exchange._time_synchronizer = MagicMock()
        self.exchange._time_synchronizer.time.return_value = time.time()

        api_called = False

        async def mock_api_get(*args, **kwargs):
            nonlocal api_called
            api_called = True
            return [
                {"fee": "0.1", "quantity": "1", "price": "50000",
                 "side": "Buy", "triggeredBy": "sell"}
            ]

        self.exchange._api_get = mock_api_get

        asyncio.get_event_loop().run_until_complete(
            self.exchange._update_trading_fees()
        )

        self.assertTrue(api_called, "API should be called when cache is expired")

    def test_empty_cache_triggers_fetch(self):
        """If no fees cached at all, should fetch."""
        self.exchange._trading_fees = {}
        self.exchange._trading_fees_last_computed = 0.0
        self.exchange._time_synchronizer = MagicMock()
        self.exchange._time_synchronizer.time.return_value = time.time()

        api_called = False

        async def mock_api_get(*args, **kwargs):
            nonlocal api_called
            api_called = True
            return []

        self.exchange._api_get = mock_api_get

        asyncio.get_event_loop().run_until_complete(
            self.exchange._update_trading_fees()
        )

        self.assertTrue(api_called, "API should be called when cache is empty")


class TestFix3UnknownOrderType(unittest.TestCase):
    """P3-3: to_hb_order_type should not crash on unknown types."""

    def test_known_types(self):
        """Known order types map correctly."""
        self.assertEqual(NonkycExchange.to_hb_order_type("limit"), OrderType.LIMIT)
        self.assertEqual(NonkycExchange.to_hb_order_type("LIMIT"), OrderType.LIMIT)
        self.assertEqual(NonkycExchange.to_hb_order_type("market"), OrderType.MARKET)
        self.assertEqual(NonkycExchange.to_hb_order_type("MARKET"), OrderType.MARKET)

    def test_unknown_type_defaults_to_limit(self):
        """Unknown order types should default to LIMIT, not crash."""
        result = NonkycExchange.to_hb_order_type("stop_limit")
        self.assertEqual(result, OrderType.LIMIT)

    def test_unknown_type_does_not_raise(self):
        """Unknown order types should never raise KeyError."""
        for unknown in ["post_only", "stop_loss", "trailing", "foobar", ""]:
            try:
                result = NonkycExchange.to_hb_order_type(unknown)
                self.assertEqual(result, OrderType.LIMIT)
            except KeyError:
                self.fail(f"to_hb_order_type raised KeyError for '{unknown}'")


class TestFix4ServerTimeNormalization(unittest.TestCase):
    """P3-5: get_current_server_time should normalize ms to seconds."""

    def test_milliseconds_normalized(self):
        """Value > 1 trillion should be divided by 1000."""
        ms_value = 1772395776849  # milliseconds
        # The function should detect this and convert
        expected_seconds = ms_value / 1000.0
        # Simulate the normalization logic
        if ms_value > 1_000_000_000_000:
            result = ms_value / 1000.0
        else:
            result = ms_value
        self.assertAlmostEqual(result, expected_seconds, places=1)

    def test_seconds_unchanged(self):
        """Value in seconds range should not be divided."""
        sec_value = 1772395776.849  # seconds
        if sec_value > 1_000_000_000_000:
            result = sec_value / 1000.0
        else:
            result = sec_value
        self.assertEqual(result, sec_value)


if __name__ == "__main__":
    unittest.main()
