"""
NonKYC Private Connector Smoke Tests — REQUIRES API KEYS

Uses WebAssistantsFactory directly with auth but WITHOUT time synchronizer
pre-processor to avoid event loop contamination in aggregate test runs.

Markers: live_api
"""
import asyncio
import os
import unittest

import pytest

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

pytestmark = pytest.mark.live_api


class TestNonkycPrivateConnectorSmoke(unittest.TestCase):
    """Authenticated smoke tests — requires NONKYC_API_KEY and NONKYC_API_SECRET."""

    @classmethod
    def setUpClass(cls):
        cls.api_key = os.environ.get("NONKYC_API_KEY")
        cls.api_secret = os.environ.get("NONKYC_API_SECRET")
        if not cls.api_key or not cls.api_secret:
            raise unittest.SkipTest("NONKYC_API_KEY and NONKYC_API_SECRET required")
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'loop') and cls.loop and not cls.loop.is_closed():
            cls.loop.run_until_complete(cls.loop.shutdown_asyncgens())
            cls.loop.close()

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key=self.api_key,
            nonkyc_api_secret=self.api_secret,
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )

    def _run(self, coro):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout=30))

    def _make_auth_api_factory(self):
        """Create API factory with auth but WITHOUT time synchronizer."""
        return WebAssistantsFactory(
            throttler=web_utils.create_throttler(),
            auth=self.exchange.authenticator,
        )

    def test_authenticated_balance_fetch(self):
        """Connector fetches real balances via the production auth path."""
        async def fetch():
            api_factory = self._make_auth_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.private_rest_url(path_url=CONSTANTS.USER_BALANCES_PATH_URL),
                method=RESTMethod.GET,
                is_auth_required=True,
                throttler_limit_id=CONSTANTS.USER_BALANCES_PATH_URL,
            )
        result = self._run(fetch())
        self.assertIsInstance(result, list)

    def test_authenticated_active_orders(self):
        """Connector fetches active orders via the production path."""
        async def fetch():
            api_factory = self._make_auth_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.private_rest_url(path_url=CONSTANTS.ACCOUNT_ORDERS_PATH_URL),
                params={"symbol": "BTC/USDT"},
                method=RESTMethod.GET,
                is_auth_required=True,
                throttler_limit_id=CONSTANTS.ACCOUNT_ORDERS_PATH_URL,
            )
        result = self._run(fetch())
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
