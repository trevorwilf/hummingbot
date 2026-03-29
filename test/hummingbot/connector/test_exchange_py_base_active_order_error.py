"""Tests for Fix 3: _handle_update_error_for_active_order should only increment
not-found counter when _is_order_not_found_during_status_update_error returns True."""
import asyncio
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


class TestActiveOrderErrorHandler(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.exchange = MexcExchange(
            mexc_api_key="test",
            mexc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self.mock_order = MagicMock(spec=InFlightOrder)
        self.mock_order.client_order_id = "test-order-001"
        self.mock_order.trading_pair = "BTC-USDT"

    async def test_dns_failure_does_not_increment_not_found_counter(self):
        """DNS failures are transport errors — must NOT increment not-found counter."""
        from aiohttp import ClientConnectorError
        # Simulate a DNS failure
        dns_error = ConnectionError("Cannot connect to host api.mexc.com:443 ssl:default [DNS lookup failed]")

        with patch.object(self.exchange._order_tracker, 'process_order_not_found',
                          new_callable=AsyncMock) as mock_not_found:
            await self.exchange._handle_update_error_for_active_order(self.mock_order, dns_error)
            mock_not_found.assert_not_called()

    async def test_timeout_does_not_increment_not_found_counter(self):
        """Timeouts mean 'could not reach exchange' — NOT 'order doesn't exist'."""
        timeout_error = asyncio.TimeoutError("Connection timed out")

        with patch.object(self.exchange._order_tracker, 'process_order_not_found',
                          new_callable=AsyncMock) as mock_not_found:
            await self.exchange._handle_update_error_for_active_order(self.mock_order, timeout_error)
            mock_not_found.assert_not_called()

    async def test_generic_http_500_does_not_increment_counter(self):
        """HTTP 500 is a server error — must NOT increment not-found counter."""
        server_error = IOError("HTTP status 500 — Internal Server Error")

        with patch.object(self.exchange._order_tracker, 'process_order_not_found',
                          new_callable=AsyncMock) as mock_not_found:
            await self.exchange._handle_update_error_for_active_order(self.mock_order, server_error)
            mock_not_found.assert_not_called()

    async def test_exchange_not_found_error_increments_counter(self):
        """When _is_order_not_found_during_status_update_error returns True,
        process_order_not_found SHOULD be called."""
        not_found_error = IOError("Order does not exist")

        with patch.object(self.exchange, '_is_order_not_found_during_status_update_error',
                          return_value=True), \
             patch.object(self.exchange._order_tracker, 'process_order_not_found',
                          new_callable=AsyncMock) as mock_not_found:
            await self.exchange._handle_update_error_for_active_order(self.mock_order, not_found_error)
            mock_not_found.assert_called_once_with("test-order-001")

    async def test_connection_reset_does_not_increment_counter(self):
        """Connection reset is a transport error — must NOT increment not-found counter."""
        reset_error = ConnectionResetError("Connection reset by peer")

        with patch.object(self.exchange._order_tracker, 'process_order_not_found',
                          new_callable=AsyncMock) as mock_not_found:
            await self.exchange._handle_update_error_for_active_order(self.mock_order, reset_error)
            mock_not_found.assert_not_called()
