"""
True connector-path live smoke tests for NonKYC.

These tests instantiate the actual production connector classes and validate
end-to-end behavior against the live exchange. They are NOT part of the
per-PR blocking lane.

Markers: live_api (requires NONKYC_API_KEY and NONKYC_API_SECRET env vars)

Event loop note: Uses a class-level shared event loop to avoid aiohttp
session/connector binding issues between tests. Each test creates a fresh
NonkycExchange instance but shares the same event loop so cached aiohttp
sessions don't reference a closed loop.
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
        self.assertIn("asks", result)
        self.assertIn("bids", result)
        self.assertIsInstance(result["asks"], list)
        self.assertIsInstance(result["bids"], list)

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


@pytest.mark.live_api
class TestNonkycPublicWsSmoke(unittest.TestCase):
    """Public WebSocket smoke tests — no API keys required."""

    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'loop') and cls.loop and not cls.loop.is_closed():
            cls.loop.run_until_complete(cls.loop.shutdown_asyncgens())
            cls.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout=30))

    def test_public_ws_orderbook_snapshot(self):
        """Connect to NonKYC WS and receive an orderbook snapshot."""
        import websockets
        import json as _json

        async def check():
            async with websockets.connect("wss://ws.nonkyc.io") as ws:
                sub = {"method": "subscribeOrderbook", "params": {"symbol": "BTC/USDT"}, "id": 1}
                await ws.send(_json.dumps(sub))
                for _ in range(10):
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = _json.loads(msg)
                    if data.get("method") == "snapshotOrderbook":
                        return data
                return None

        result = self._run(check())
        self.assertIsNotNone(result, "Did not receive orderbook snapshot")
        params = result.get("params", {})
        self.assertIn("asks", params)
        self.assertIn("bids", params)
        self.assertIn("sequence", params)

    def test_public_ws_trade_stream(self):
        """Connect to NonKYC WS and receive ticker events."""
        import websockets
        import json as _json

        async def check():
            async with websockets.connect("wss://ws.nonkyc.io") as ws:
                sub = {"method": "subscribeTicker", "params": {"symbol": "BTC/USDT"}, "id": 1}
                await ws.send(_json.dumps(sub))
                for _ in range(10):
                    msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    data = _json.loads(msg)
                    if data.get("method") == "ticker":
                        return data
                return None

        result = self._run(check())
        self.assertIsNotNone(result, "Did not receive ticker event")


if __name__ == "__main__":
    unittest.main()
