"""
True connector-path live smoke tests for NonKYC.

These tests instantiate the actual production connector classes and validate
end-to-end behavior against the live exchange. They are NOT part of the
per-PR blocking lane.

Markers: live_api (requires NONKYC_API_KEY and NONKYC_API_SECRET env vars)
"""
import asyncio
import os
import unittest
from decimal import Decimal

import pytest

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange

pytestmark = pytest.mark.live_api


class TestNonkycLiveConnectorSmoke(unittest.TestCase):
    """Smoke tests using the real connector pipeline."""

    def setUp(self):
        self.api_key = os.environ.get("NONKYC_API_KEY")
        self.api_secret = os.environ.get("NONKYC_API_SECRET")
        if not self.api_key or not self.api_secret:
            pytest.skip("NONKYC_API_KEY and NONKYC_API_SECRET required")

        self.exchange = NonkycExchange(
            nonkyc_api_key=self.api_key,
            nonkyc_api_secret=self.api_secret,
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        if hasattr(self, 'loop') and self.loop:
            self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout=30))

    def test_public_network_check(self):
        """Connector can reach the exchange (server time / ping)."""
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def check():
            api_factory = web_utils.build_api_factory()
            rest = await api_factory.get_rest_assistant()
            data = await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.PING_PATH_URL),
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.PING_PATH_URL,
            )
            return data

        result = self._run(check())
        self.assertIsNotNone(result)

    def test_public_orderbook_snapshot(self):
        """Connector fetches a real order book snapshot via the production data source."""
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def fetch_ob():
            api_factory = web_utils.build_api_factory()
            rest = await api_factory.get_rest_assistant()
            data = await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL),
                params={"symbol": "BTC/USDT", "limit": "10"},
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.MARKET_ORDERBOOK_PATH_URL,
            )
            return data

        result = self._run(fetch_ob())
        self.assertIn("ask", result)
        self.assertIn("bid", result)

    def test_authenticated_balance_fetch(self):
        """Connector fetches real balances via the production auth path."""
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def fetch_balances():
            api_factory = web_utils.build_api_factory(
                auth=self.exchange.authenticator
            )
            rest = await api_factory.get_rest_assistant()
            data = await rest.execute_request(
                url=web_utils.private_rest_url(path_url=CONSTANTS.USER_BALANCES_PATH_URL),
                method=RESTMethod.GET,
                is_auth_required=True,
                throttler_limit_id=CONSTANTS.USER_BALANCES_PATH_URL,
            )
            return data

        result = self._run(fetch_balances())
        self.assertIsInstance(result, list)

    def test_authenticated_active_orders(self):
        """Connector fetches active orders via the production path."""
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def fetch_orders():
            api_factory = web_utils.build_api_factory(
                auth=self.exchange.authenticator
            )
            rest = await api_factory.get_rest_assistant()
            data = await rest.execute_request(
                url=web_utils.private_rest_url(path_url=CONSTANTS.ACCOUNT_ORDERS_PATH_URL),
                params={"symbol": "BTC/USDT"},
                method=RESTMethod.GET,
                is_auth_required=True,
                throttler_limit_id=CONSTANTS.ACCOUNT_ORDERS_PATH_URL,
            )
            return data

        result = self._run(fetch_orders())
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
