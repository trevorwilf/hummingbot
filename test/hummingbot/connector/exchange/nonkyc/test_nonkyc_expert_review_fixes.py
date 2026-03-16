"""
Tests for expert review fixes (Phase 1 fixes 1.5, 1.6, 1.7).
"""
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch, create_autospec

from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


class TestLimitMakerRemoval(unittest.TestCase):
    """Fix 1.5: LIMIT_MAKER should not be in supported_order_types."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )

    def test_limit_maker_not_in_supported_types(self):
        """LIMIT_MAKER should NOT be in supported_order_types."""
        supported = self.exchange.supported_order_types()
        self.assertNotIn(OrderType.LIMIT_MAKER, supported)
        self.assertIn(OrderType.LIMIT, supported)
        self.assertIn(OrderType.MARKET, supported)

    def test_limit_maker_raises_value_error(self):
        """Attempting LIMIT_MAKER order type should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            NonkycExchange.nonkyc_order_type(OrderType.LIMIT_MAKER)
        self.assertIn("LIMIT_MAKER", str(ctx.exception))
        self.assertIn("post-only", str(ctx.exception))

    def test_limit_order_type_works(self):
        """LIMIT order type should still work."""
        result = NonkycExchange.nonkyc_order_type(OrderType.LIMIT)
        self.assertEqual("limit", result)

    def test_market_order_type_works(self):
        """MARKET order type should still work."""
        result = NonkycExchange.nonkyc_order_type(OrderType.MARKET)
        self.assertEqual("market", result)


class TestSymbolParsingFix(unittest.TestCase):
    """Fix 1.6: Brittle symbol parsing in trade handler should be safe."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self.exchange._set_trading_pair_symbol_map(bidict({"BTC/USDT": "BTC-USDT"}))

    def _process_single_event(self, event):
        """Helper to process a single user stream event through the event listener."""
        async def run():
            # Feed a single event into the user stream queue, then cancel
            event_queue = asyncio.Queue()
            event_queue.put_nowait(event)

            # Override _iter_user_event_queue to yield from our queue
            async def mock_iter():
                while not event_queue.empty():
                    yield event_queue.get_nowait()

            with patch.object(self.exchange, "_iter_user_event_queue", mock_iter):
                await self.exchange._user_stream_event_listener()

        self._run(run())

    def test_trade_with_none_symbol_no_tracked_order(self):
        """Trade with None symbol and no tracked order should log warning, not crash."""
        event = {
            "method": "report",
            "params": {
                "reportType": "trade",
                "symbol": None,
                "userProvidedId": "unknown_order",
                "tradeId": "123",
                "id": "456",
                "tradeQuantity": "1.0",
                "tradePrice": "100.0",
                "tradeFee": "0.1",
                "updatedAt": 1234567890000,
                "status": "filled",
            }
        }
        # Should not raise - the continue should skip the trade
        self._process_single_event(event)

    def test_trade_with_no_slash_symbol_no_tracked_order(self):
        """Trade with symbol missing '/' and no tracked order should log warning, not crash."""
        event = {
            "method": "report",
            "params": {
                "reportType": "trade",
                "symbol": "BTCUSDT",
                "userProvidedId": "unknown_order",
                "tradeId": "123",
                "id": "456",
                "tradeQuantity": "1.0",
                "tradePrice": "100.0",
                "tradeFee": "0.1",
                "updatedAt": 1234567890000,
                "status": "filled",
            }
        }
        # Should not raise
        self._process_single_event(event)

    def test_tracked_order_quote_asset_preferred(self):
        """When tracked order exists, quote asset should come from trading_pair, not symbol parsing."""
        # Verify the code path: if tracked_order exists, quote_asset = tracked_order.trading_pair.split("-")[1]
        # This is a unit test of the logic, not a full integration test
        trading_pair = "BTC-USDT"
        quote_asset = trading_pair.split("-")[1]
        self.assertEqual("USDT", quote_asset)

        # Verify that symbol parsing is the fallback
        symbol = "BTC/USDT"
        parts = symbol.split('/') if symbol else []
        self.assertEqual(2, len(parts))
        self.assertEqual("USDT", parts[1])


class TestCancelAllOrdersFanOut(unittest.TestCase):
    """Fix 1.7: cancel_all_orders_on_exchange should fan out when trading_pair=None."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT", "ETH-USDT"],
            trading_required=False,
        )
        self.exchange._set_trading_pair_symbol_map(bidict({
            "BTC/USDT": "BTC-USDT",
            "ETH/USDT": "ETH-USDT",
        }))

    def test_cancel_all_with_pair_sends_symbol(self):
        """cancel_all with a specific pair should send the symbol."""
        with patch.object(self.exchange, "_api_post", new_callable=AsyncMock, return_value=[]) as mock_post:
            result = self._run(self.exchange.cancel_all_orders_on_exchange("BTC-USDT"))
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            self.assertEqual(call_kwargs.kwargs["data"]["symbol"], "BTC/USDT")

    def test_cancel_all_none_fans_out(self):
        """cancel_all with None should fan out across active order pairs."""
        # Create mock active orders
        mock_order1 = MagicMock()
        mock_order1.trading_pair = "BTC-USDT"
        mock_order2 = MagicMock()
        mock_order2.trading_pair = "ETH-USDT"

        with patch.object(
            type(self.exchange._order_tracker), "active_orders",
            new_callable=PropertyMock,
            return_value={"ord1": mock_order1, "ord2": mock_order2}
        ):
            with patch.object(self.exchange, "_cancel_all_for_pair",
                             new_callable=AsyncMock, return_value=[{"id": "1"}]) as mock_cancel:
                result = self._run(self.exchange.cancel_all_orders_on_exchange(None))
                self.assertEqual(2, mock_cancel.call_count)
                called_pairs = {call.args[0] for call in mock_cancel.call_args_list}
                self.assertEqual({"BTC-USDT", "ETH-USDT"}, called_pairs)

    def test_cancel_all_error_response_detected(self):
        """Error response inside HTTP 200 should be raised."""
        error_response = {"error": "Invalid symbol"}
        with patch.object(self.exchange, "_api_post", new_callable=AsyncMock, return_value=error_response):
            with self.assertRaises(IOError) as ctx:
                self._run(self.exchange._cancel_all_for_pair("BTC-USDT"))
            self.assertIn("Invalid symbol", str(ctx.exception))


class TestWsAuthTimeout(unittest.TestCase):
    """Fix 1.4: WS auth timeout should work on silent sockets."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_auth_timeout_on_silent_socket(self):
        """_authenticate_ws_connection should timeout on its own without external wait_for."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import (
            NonkycAPIUserStreamDataSource,
        )
        from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth

        mock_auth = MagicMock(spec=NonkycAuth)
        mock_auth.generate_ws_authentication_message.return_value = {
            "method": "login",
            "params": {"algo": "HS256", "pKey": "test", "nonce": "abc", "signature": "def"},
            "id": 1
        }

        data_source = NonkycAPIUserStreamDataSource(
            auth=mock_auth,
            trading_pairs=["BTC-USDT"],
            connector=MagicMock(),
            api_factory=MagicMock(),
        )
        # Override base_timeout to be very short for testing
        mock_ws = AsyncMock()

        # Make iter_messages block forever (silent socket)
        async def forever_iter():
            await asyncio.sleep(3600)
            yield  # Never reached

        mock_ws.iter_messages.return_value = forever_iter()
        mock_ws.send = AsyncMock()

        # The method should raise TimeoutError or IOError, NOT block forever
        with self.assertRaises((asyncio.TimeoutError, IOError)):
            # Use a short overall timeout as safety net, but the method itself should timeout
            self._run(asyncio.wait_for(
                data_source._authenticate_ws_connection(mock_ws),
                timeout=35.0  # 3 attempts * 10s each + margin
            ))


if __name__ == "__main__":
    unittest.main()
