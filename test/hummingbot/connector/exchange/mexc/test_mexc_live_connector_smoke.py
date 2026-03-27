"""
MEXC Public Connector-Path Live Smoke Tests — NO API KEYS REQUIRED

Uses REAL production connector classes (web_utils, throttler, post-processor)
to validate live MEXC API. Catches schema drift, protobuf changes, and
connector parsing bugs that raw endpoint probes would miss.

Markers: live_api
"""
import asyncio
import time
import unittest

import pytest

pytestmark = pytest.mark.live_api


class TestMexcPublicConnectorSmoke(unittest.TestCase):
    """Production connector-path smoke tests against live MEXC."""

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

    def _make_api_factory(self):
        """Create API factory without time synchronizer to avoid stale loop issues."""
        from hummingbot.connector.exchange.mexc import mexc_web_utils as web_utils
        return web_utils.build_api_factory_without_time_synchronizer_pre_processor(
            throttler=web_utils.create_throttler()
        )

    def test_exchange_info_via_connector_web_utils(self):
        """Fetch exchangeInfo through the production web_utils factory."""
        from hummingbot.connector.exchange.mexc import mexc_constants as CONSTANTS
        from hummingbot.connector.exchange.mexc import mexc_web_utils as web_utils
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def fetch():
            api_factory = self._make_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.EXCHANGE_INFO_PATH_URL),
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.EXCHANGE_INFO_PATH_URL,
                headers={"Content-Type": "application/json"},
            )

        result = self._run(fetch())
        self.assertIn("symbols", result)
        self.assertIsInstance(result["symbols"], list)
        self.assertGreater(len(result["symbols"]), 0)
        sym = result["symbols"][0]
        for field in ("symbol", "status", "baseAsset", "quoteAsset"):
            self.assertIn(field, sym, f"Missing expected field '{field}' in symbol data")

    def test_depth_snapshot_via_connector_web_utils(self):
        """Fetch order book snapshot through production web_utils.

        MUST pass Content-Type: application/json — MEXC rejects the default
        application/x-www-form-urlencoded with 400 'Invalid content Type'.
        """
        from hummingbot.connector.exchange.mexc import mexc_constants as CONSTANTS
        from hummingbot.connector.exchange.mexc import mexc_web_utils as web_utils
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def fetch():
            api_factory = self._make_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL),
                params={"symbol": "BTCUSDT", "limit": "5"},
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
                headers={"Content-Type": "application/json"},
            )

        result = self._run(fetch())
        self.assertIn("bids", result)
        self.assertIn("asks", result)
        self.assertIsInstance(result["bids"], list)
        self.assertIsInstance(result["asks"], list)
        self.assertGreater(len(result["bids"]), 0)
        self.assertGreater(len(result["asks"]), 0)
        bid = result["bids"][0]
        self.assertEqual(2, len(bid), f"Bid entry should have 2 elements, got {len(bid)}")

    def test_server_time_via_connector_web_utils(self):
        """Server time fetch through production web_utils."""
        from hummingbot.connector.exchange.mexc import mexc_constants as CONSTANTS
        from hummingbot.connector.exchange.mexc import mexc_web_utils as web_utils
        from hummingbot.core.web_assistant.connections.data_types import RESTMethod

        async def fetch():
            api_factory = self._make_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL),
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.SERVER_TIME_PATH_URL,
                headers={"Content-Type": "application/json"},
            )

        result = self._run(fetch())
        self.assertIn("serverTime", result)
        self.assertIsInstance(result["serverTime"], int)
        drift_ms = abs(int(time.time() * 1000) - result["serverTime"])
        self.assertLess(drift_ms, 60000, f"Server time drift too large: {drift_ms}ms")


if __name__ == "__main__":
    unittest.main()
