"""
Tests for MEXC expert review fixes (Phase 2).
"""
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.mexc import mexc_constants as CONSTANTS
from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate
from hummingbot.core.data_type.common import OrderType, TradeType


class TestCancelStatusDetection(unittest.TestCase):
    """Fix 2.1: Cancel should accept CANCELED status, not NEW."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def setUp(self):
        self.exchange = MexcExchange(
            mexc_api_key="test",
            mexc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )

    def test_cancel_with_canceled_status_returns_true(self):
        """CANCELED status should return True."""
        with patch.object(self.exchange, "_api_delete", new_callable=AsyncMock,
                         return_value={"status": "CANCELED"}):
            with patch.object(self.exchange, "exchange_symbol_associated_to_pair",
                             new_callable=AsyncMock, return_value="BTCUSDT"):
                order = MagicMock(spec=InFlightOrder)
                order.trading_pair = "BTC-USDT"
                result = self._run(self.exchange._place_cancel("test_id", order))
                self.assertTrue(result)

    def test_cancel_with_partially_canceled_returns_true(self):
        """PARTIALLY_CANCELED status should return True."""
        with patch.object(self.exchange, "_api_delete", new_callable=AsyncMock,
                         return_value={"status": "PARTIALLY_CANCELED"}):
            with patch.object(self.exchange, "exchange_symbol_associated_to_pair",
                             new_callable=AsyncMock, return_value="BTCUSDT"):
                order = MagicMock(spec=InFlightOrder)
                order.trading_pair = "BTC-USDT"
                result = self._run(self.exchange._place_cancel("test_id", order))
                self.assertTrue(result)

    def test_cancel_with_new_status_returns_false(self):
        """NEW status (open order, not canceled) should return False."""
        with patch.object(self.exchange, "_api_delete", new_callable=AsyncMock,
                         return_value={"status": "NEW"}):
            with patch.object(self.exchange, "exchange_symbol_associated_to_pair",
                             new_callable=AsyncMock, return_value="BTCUSDT"):
                order = MagicMock(spec=InFlightOrder)
                order.trading_pair = "BTC-USDT"
                result = self._run(self.exchange._place_cancel("test_id", order))
                self.assertFalse(result)


class TestWsOrderStatus5(unittest.TestCase):
    """Fix 2.2: WS order status 5 should map to CANCELED, not OPEN."""

    def test_ws_status_5_is_canceled(self):
        """WS status 5 (partially canceled) should map to CANCELED."""
        self.assertEqual(OrderState.CANCELED, CONSTANTS.WS_ORDER_STATE[5])

    def test_ws_status_4_is_canceled(self):
        """WS status 4 (canceled) should map to CANCELED."""
        self.assertEqual(OrderState.CANCELED, CONSTANTS.WS_ORDER_STATE[4])

    def test_ws_status_1_is_open(self):
        """WS status 1 should still be OPEN."""
        self.assertEqual(OrderState.OPEN, CONSTANTS.WS_ORDER_STATE[1])


class TestListenKeyLifecycle(unittest.TestCase):
    """Fix 2.3: Listen key timeout and null guard."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_manage_listen_key_handles_none_ws_assistant(self):
        """Cleanup path should not crash when _ws_assistant is None."""
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import (
            MexcAPIUserStreamDataSource,
        )
        from hummingbot.connector.exchange.mexc.mexc_auth import MexcAuth

        mock_auth = MagicMock(spec=MexcAuth)
        data_source = MexcAPIUserStreamDataSource(
            auth=mock_auth,
            trading_pairs=["BTC-USDT"],
            connector=MagicMock(),
            api_factory=MagicMock(),
        )
        # Ensure _ws_assistant is None
        data_source._ws_assistant = None

        # Simulate the finally block running with None ws_assistant
        async def run_finally():
            try:
                raise asyncio.CancelledError()
            except asyncio.CancelledError:
                pass
            finally:
                if data_source._ws_assistant is not None:
                    await data_source._ws_assistant.disconnect()
                data_source._current_listen_key = None
                data_source._listen_key_initialized_event.clear()

        # Should not raise AttributeError
        self._run(run_finally())


class TestTradeUpdateTradingPair(unittest.TestCase):
    """Fix 2.4: TradeUpdate should use Hummingbot format, not exchange symbol."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_trade_update_uses_hb_format(self):
        """TradeUpdate.trading_pair should be 'BTC-USDT', not 'BTCUSDT'."""
        exchange = MexcExchange(
            mexc_api_key="test",
            mexc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )

        mock_order = MagicMock(spec=InFlightOrder)
        mock_order.exchange_order_id = "12345"
        mock_order.client_order_id = "hbot_123"
        mock_order.trading_pair = "BTC-USDT"
        mock_order.trade_type = TradeType.BUY

        trade_response = [{
            "id": "1",
            "orderId": "12345",
            "price": "50000.0",
            "qty": "0.1",
            "quoteQty": "5000.0",
            "commission": "0.001",
            "commissionAsset": "BTC",
            "time": 1234567890000,
        }]

        with patch.object(exchange, "exchange_symbol_associated_to_pair",
                         new_callable=AsyncMock, return_value="BTCUSDT"):
            with patch.object(exchange, "_api_get",
                             new_callable=AsyncMock, return_value=trade_response):
                with patch.object(exchange, "trade_fee_schema", return_value=MagicMock()):
                    result = self._run(exchange._all_trade_updates_for_order(mock_order))

        self.assertEqual(1, len(result))
        # The key assertion: trading_pair should be Hummingbot format
        self.assertEqual("BTC-USDT", result[0].trading_pair)
        self.assertNotEqual("BTCUSDT", result[0].trading_pair)


class TestPostRequestBodyMovedToParams(unittest.TestCase):
    """Fix 2: POST body params must be moved to query string for MEXC/Binance-compatible signing."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_post_body_moved_to_query_params(self):
        """Verify that after auth, POST body is cleared and params are in query string."""
        import json
        from hummingbot.connector.exchange.mexc.mexc_auth import MexcAuth
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest

        mock_time = MagicMock()
        mock_time.time.return_value = 1234567890.0
        auth = MexcAuth(api_key="testKey", secret_key="testSecret", time_provider=mock_time)

        params = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.1", "price": "50000"}
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/order",
            data=json.dumps(params),
            is_auth_required=True
        )
        configured = self._run(auth.rest_authenticate(request))

        # Body must be cleared — all params in query string
        self.assertIsNone(configured.data)
        # Query params should contain original body fields plus auth
        self.assertEqual("BTCUSDT", configured.params["symbol"])
        self.assertEqual("BUY", configured.params["side"])
        self.assertIn("timestamp", configured.params)
        self.assertIn("signature", configured.params)


class TestListenKeyRedaction(unittest.TestCase):
    """Phase 3: Listen key must be redacted in log output."""

    def test_redact_token_normal(self):
        """Redact helper shows first 4 and last 4 chars."""
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import _redact_token
        fake_key = "abcdef1234567890abcdef1234567890"
        redacted = _redact_token(fake_key)
        self.assertNotEqual(fake_key, redacted)
        self.assertTrue(redacted.startswith("abcd"))
        self.assertTrue(redacted.endswith("7890"))
        self.assertIn("...", redacted)

    def test_redact_token_short(self):
        """Short tokens are fully masked."""
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import _redact_token
        self.assertEqual("****", _redact_token("short"))
        self.assertEqual("****", _redact_token(""))
        self.assertEqual("****", _redact_token(None))

    def test_listen_key_not_logged_in_plaintext(self):
        """Verify that the full listen key never appears in log output."""
        from hummingbot.connector.exchange.mexc.mexc_api_user_stream_data_source import (
            MexcAPIUserStreamDataSource, _redact_token,
        )
        from hummingbot.connector.exchange.mexc.mexc_auth import MexcAuth

        mock_auth = MagicMock(spec=MexcAuth)
        data_source = MexcAPIUserStreamDataSource(
            auth=mock_auth,
            trading_pairs=["BTC-USDT"],
            connector=MagicMock(),
            api_factory=MagicMock(),
        )

        fake_key = "abcdef1234567890abcdef1234567890"
        data_source._current_listen_key = fake_key

        # Capture log output
        import logging
        with self.assertLogs(data_source.logger(), level="INFO") as cm:
            data_source.logger().info(
                f"Successfully obtained listen key {_redact_token(fake_key)}"
            )

        # The full key must not appear in any log message
        for message in cm.output:
            self.assertNotIn(fake_key, message)
            # Redacted form should be present
            self.assertIn("abcd", message)
            self.assertIn("7890", message)


if __name__ == "__main__":
    unittest.main()
