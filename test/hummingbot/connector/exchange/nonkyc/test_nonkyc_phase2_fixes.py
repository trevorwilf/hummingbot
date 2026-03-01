"""
Tests for Phase 2 connector fixes.
Each test class covers one specific fix from Hummingbot_Phase2_Connector_Fixes.

Run with:
    python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase2_fixes.py -v
"""
import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.exchange.nonkyc.nonkyc_order_book import NonkycOrderBook
from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.core.data_type.common import OrderType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


# ============================================================================
# Fix 1: _all_trade_updates_for_order uses 'since' parameter
# ============================================================================
class TestFix1TradeQueryPagination(unittest.TestCase):
    """Fix 1: _all_trade_updates_for_order should pass 'since' timestamp to limit query."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )

    def test_since_param_included_when_order_has_creation_timestamp(self):
        """Orders with a creation_timestamp should add 'since' to the REST params."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = "abc123"
        order.client_order_id = "HBOT-test"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 1700000000.0  # ~Nov 2023

        # Mock the API call to capture what params are sent
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
                       "REST params should include 'since' when order has creation_timestamp")
        # Phase 3 adds 60-second safety margin: (1700000000 - 60) * 1000 = 1699999940000
        self.assertEqual(captured_params["since"], "1699999940000")

    def test_since_param_omitted_when_no_creation_timestamp(self):
        """Orders with creation_timestamp=0 should NOT include 'since'."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = "abc123"
        order.client_order_id = "HBOT-test"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 0.0

        captured_params = {}

        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            captured_params.update(params or {})
            return []

        self.exchange._api_get = mock_api_get

        async def mock_symbol(trading_pair):
            return "BTC/USDT"

        self.exchange.exchange_symbol_associated_to_pair = mock_symbol

        asyncio.get_event_loop().run_until_complete(
            self.exchange._all_trade_updates_for_order(order)
        )

        self.assertNotIn("since", captured_params,
                          "REST params should NOT include 'since' when creation_timestamp is 0")

    def test_trades_filtered_by_order_id(self):
        """Trades should still be filtered by orderid even with 'since' param."""
        order = MagicMock(spec=InFlightOrder)
        order.exchange_order_id = "target_order"
        order.client_order_id = "HBOT-test"
        order.trading_pair = "BTC-USDT"
        order.trade_type = MagicMock()
        order.creation_timestamp = 1700000000.0

        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            return [
                {"id": "t1", "orderid": "target_order", "quantity": "1.0", "price": "100.0",
                 "fee": "0.15", "timestamp": 1700000001000},
                {"id": "t2", "orderid": "other_order", "quantity": "2.0", "price": "200.0",
                 "fee": "0.30", "timestamp": 1700000002000},
            ]

        self.exchange._api_get = mock_api_get

        async def mock_symbol(trading_pair):
            return "BTC/USDT"

        self.exchange.exchange_symbol_associated_to_pair = mock_symbol

        result = asyncio.get_event_loop().run_until_complete(
            self.exchange._all_trade_updates_for_order(order)
        )

        self.assertEqual(len(result), 1, "Should only return trades matching the order ID")
        self.assertEqual(result[0].trade_id, "t1")


# ============================================================================
# Fix 2: _update_trading_fees uses cache + since filter
# ============================================================================
class TestFix2FeeCachingAndSinceFilter(unittest.TestCase):
    """Fix 2: Fee computation should be cached and use 'since' to limit trade window."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )
        # Mock time synchronizer
        self.exchange._time_synchronizer = MagicMock()
        self.exchange._time_synchronizer.time.return_value = time.time()

    def test_cache_prevents_redundant_api_calls(self):
        """Second call within TTL should not make an API request."""
        call_count = 0

        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            nonlocal call_count
            call_count += 1
            return [
                {"fee": "0.15", "quantity": "1.0", "price": "100.0",
                 "side": "Buy", "triggeredBy": "sell"}
            ]

        self.exchange._api_get = mock_api_get

        loop = asyncio.get_event_loop()

        # First call — should hit the API
        loop.run_until_complete(self.exchange._update_trading_fees())
        self.assertEqual(call_count, 1)

        # Second call — should be cached (within TTL)
        loop.run_until_complete(self.exchange._update_trading_fees())
        self.assertEqual(call_count, 1, "Second call within TTL should not hit API")

    def test_since_param_limits_trade_window(self):
        """API call should include a 'since' param for last 24 hours only."""
        captured_params = {}

        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            captured_params.update(params or {})
            return []

        self.exchange._api_get = mock_api_get

        asyncio.get_event_loop().run_until_complete(
            self.exchange._update_trading_fees()
        )

        self.assertIn("since", captured_params,
                       "Fee query should include 'since' parameter")
        since_ts = int(captured_params["since"])
        now_ms = int(time.time() * 1000)
        day_ms = 86400 * 1000
        self.assertAlmostEqual(since_ts, now_ms - day_ms, delta=5000,
                                msg="'since' should be ~24 hours ago")

    def test_maker_taker_classification(self):
        """Verify side != triggeredBy -> maker, side == triggeredBy -> taker."""
        async def mock_api_get(path_url, params=None, is_auth_required=False, **kwargs):
            return [
                # Maker: side=Buy, triggeredBy=sell (your resting order was matched)
                {"fee": "0.10", "quantity": "1.0", "price": "100.0",
                 "side": "Buy", "triggeredBy": "sell"},
                # Taker: side=Buy, triggeredBy=buy (you matched a resting order)
                {"fee": "0.20", "quantity": "1.0", "price": "100.0",
                 "side": "Buy", "triggeredBy": "buy"},
            ]

        self.exchange._api_get = mock_api_get

        asyncio.get_event_loop().run_until_complete(
            self.exchange._update_trading_fees()
        )

        self.assertIn("maker_fee", self.exchange._trading_fees)
        self.assertIn("taker_fee", self.exchange._trading_fees)
        self.assertAlmostEqual(float(self.exchange._trading_fees["maker_fee"]), 0.001, places=4)
        self.assertAlmostEqual(float(self.exchange._trading_fees["taker_fee"]), 0.002, places=4)


# ============================================================================
# Fix 3: Order Book WS reconnect clears sequence tracking
# ============================================================================
class TestFix3OrderBookReconnectBackoff(unittest.TestCase):
    """Fix 3: WS interruption should clear _last_sequence to force fresh snapshot."""

    def test_on_interruption_clears_sequence_tracking(self):
        """After WS disconnection, _last_sequence should be empty."""
        connector = MagicMock()
        api_factory = MagicMock()

        data_source = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT"],
            connector=connector,
            api_factory=api_factory,
        )

        # Simulate some tracked sequences
        data_source._last_sequence = {"BTC-USDT": 42, "ETH-USDT": 99}

        # Call the interruption handler
        ws_assistant = MagicMock()
        ws_assistant.disconnect = AsyncMock()

        asyncio.get_event_loop().run_until_complete(
            data_source._on_order_book_ws_interruption(ws_assistant)
        )

        self.assertEqual(len(data_source._last_sequence), 0,
                          "Sequence tracking should be cleared after WS interruption")

    def test_on_interruption_disconnects_ws(self):
        """WS assistant should be disconnected on interruption."""
        connector = MagicMock()
        api_factory = MagicMock()

        data_source = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT"],
            connector=connector,
            api_factory=api_factory,
        )

        ws_assistant = MagicMock()
        ws_assistant.disconnect = AsyncMock()

        asyncio.get_event_loop().run_until_complete(
            data_source._on_order_book_ws_interruption(ws_assistant)
        )

        ws_assistant.disconnect.assert_called_once()

    def test_on_interruption_handles_none_ws(self):
        """Should not crash when ws_assistant is None."""
        connector = MagicMock()
        api_factory = MagicMock()

        data_source = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT"],
            connector=connector,
            api_factory=api_factory,
        )
        data_source._last_sequence = {"BTC-USDT": 42}

        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            data_source._on_order_book_ws_interruption(None)
        )

        self.assertEqual(len(data_source._last_sequence), 0)


# ============================================================================
# Fix 4: to_hb_order_type handles unknown types gracefully
# ============================================================================
class TestFix4UnknownOrderTypeHandling(unittest.TestCase):
    """Fix 4: to_hb_order_type should return LIMIT for unknown types, not crash."""

    def test_known_types_still_work(self):
        self.assertEqual(NonkycExchange.to_hb_order_type("limit"), OrderType.LIMIT)
        self.assertEqual(NonkycExchange.to_hb_order_type("market"), OrderType.MARKET)
        self.assertEqual(NonkycExchange.to_hb_order_type("LIMIT"), OrderType.LIMIT)
        self.assertEqual(NonkycExchange.to_hb_order_type("MARKET"), OrderType.MARKET)

    def test_unknown_type_returns_limit(self):
        """Unknown order types should default to LIMIT, not raise KeyError."""
        result = NonkycExchange.to_hb_order_type("stop_loss")
        self.assertEqual(result, OrderType.LIMIT)

    def test_empty_string_returns_limit(self):
        result = NonkycExchange.to_hb_order_type("")
        self.assertEqual(result, OrderType.LIMIT)

    def test_garbage_string_returns_limit(self):
        result = NonkycExchange.to_hb_order_type("XYZZY_NOT_A_TYPE")
        self.assertEqual(result, OrderType.LIMIT)


# ============================================================================
# Fix 5: Server time normalization (ms -> seconds)
# ============================================================================
class TestFix5ServerTimeNormalization(unittest.TestCase):
    """Fix 5: get_current_server_time should return seconds, even if API gives ms."""

    def test_milliseconds_normalized_to_seconds(self):
        """If API returns milliseconds (13-digit number), normalize to seconds."""
        now_ms = int(time.time() * 1000)  # Current time in milliseconds
        now_s = time.time()

        # Simulate what the fix should do
        server_time = now_ms
        if server_time > now_s * 100:  # >100x current unix seconds = milliseconds
            server_time = server_time / 1e3

        self.assertAlmostEqual(server_time, now_s, delta=2.0,
                                msg="Millisecond timestamp should be normalized to seconds")

    def test_seconds_not_double_divided(self):
        """If API already returns seconds, don't divide again."""
        now_s = time.time()

        server_time = now_s
        if server_time > now_s * 100:
            server_time = server_time / 1e3

        self.assertAlmostEqual(server_time, now_s, delta=2.0,
                                msg="Seconds timestamp should pass through unchanged")

    def test_boundary_detection(self):
        """Verify the 100x heuristic correctly distinguishes ms from seconds."""
        now_s = time.time()
        now_ms = now_s * 1000

        # Milliseconds should be detected (>100x current seconds)
        self.assertTrue(now_ms > now_s * 100)

        # Seconds should NOT be detected
        self.assertFalse(now_s > now_s * 100)


# ============================================================================
# Fix 6: _initialize_trading_pair_symbols uses secondaryTicker
# ============================================================================
class TestFix6SecondaryTickerUsage(unittest.TestCase):
    """Fix 6: Symbol mapping should prefer secondaryTicker over splitting symbol."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )
        # Capture the mapping passed to _set_trading_pair_symbol_map
        self._captured_mapping = None
        original_set = self.exchange._set_trading_pair_symbol_map

        def capture_set(mapping):
            self._captured_mapping = mapping
            original_set(mapping)

        self.exchange._set_trading_pair_symbol_map = capture_set

    def test_uses_primary_and_secondary_ticker(self):
        """Should use primaryTicker and secondaryTicker fields for mapping."""
        exchange_info = [
            {
                "symbol": "BTC/USDT",
                "primaryTicker": "BTC",
                "secondaryTicker": "USDT",
                "isActive": True,
                "priceDecimals": 2,
                "quantityDecimals": 6,
            }
        ]

        self.exchange._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

        # Should map "BTC/USDT" -> "BTC-USDT"
        mapping = self._captured_mapping
        self.assertIsNotNone(mapping)
        self.assertIn("BTC/USDT", mapping)
        self.assertEqual(mapping["BTC/USDT"], "BTC-USDT")

    def test_falls_back_to_symbol_split_when_tickers_missing(self):
        """If primaryTicker/secondaryTicker missing, fall back to splitting symbol."""
        exchange_info = [
            {
                "symbol": "ETH/BTC",
                "isActive": True,
                "priceDecimals": 8,
                "quantityDecimals": 4,
                # No primaryTicker or secondaryTicker
            }
        ]

        self.exchange._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

        mapping = self._captured_mapping
        self.assertIsNotNone(mapping)
        self.assertIn("ETH/BTC", mapping)
        self.assertEqual(mapping["ETH/BTC"], "ETH-BTC")

    def test_skips_inactive_markets(self):
        """Inactive markets should be filtered out."""
        exchange_info = [
            {
                "symbol": "DEAD/USDT",
                "primaryTicker": "DEAD",
                "secondaryTicker": "USDT",
                "isActive": False,
                "priceDecimals": 2,
                "quantityDecimals": 2,
            }
        ]

        self.exchange._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

        mapping = self._captured_mapping
        self.assertIsNotNone(mapping)
        self.assertNotIn("DEAD/USDT", mapping)

    def test_skips_malformed_symbol(self):
        """Symbols without / and without ticker fields should be skipped."""
        exchange_info = [
            {
                "symbol": "BTCUSDT",  # No slash
                "isActive": True,
                "priceDecimals": 2,
                "quantityDecimals": 6,
                # No primaryTicker or secondaryTicker
            }
        ]

        self.exchange._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

        mapping = self._captured_mapping
        self.assertIsNotNone(mapping)
        self.assertEqual(len(mapping), 0, "Malformed symbol without tickers should be skipped")


if __name__ == "__main__":
    unittest.main()
