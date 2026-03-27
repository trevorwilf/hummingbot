"""
NonKYC Public Connector Smoke Tests — NO API KEYS REQUIRED

Validates NonKYC public REST and WebSocket endpoints through the production
connector path. Runs in every environment including no-key CI.

Uses build_api_factory_without_time_synchronizer_pre_processor to avoid
event loop contamination when running after other tests in aggregate sweeps.

Markers: live_api
"""
import asyncio
import json
import unittest
from decimal import Decimal

import pytest
import websockets

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest

pytestmark = pytest.mark.live_api


class TestNonkycPublicConnectorSmoke(unittest.TestCase):
    """Public-only smoke tests — NO API keys required.

    Uses build_api_factory_without_time_synchronizer_pre_processor to avoid
    aiohttp session/event-loop binding issues when run in aggregate sweeps.
    """

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
        """Create an API factory safe for aggregate test runs."""
        return web_utils.build_api_factory_without_time_synchronizer_pre_processor(
            throttler=web_utils.create_throttler()
        )

    # ── REST Tests ──────────────────────────────────────────────────────

    def test_public_network_check(self):
        """Connector can reach the exchange (info/ping endpoint)."""
        async def check():
            api_factory = self._make_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.PING_PATH_URL),
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.PING_PATH_URL,
            )
        result = self._run(check())
        self.assertIsNotNone(result)

    def test_public_orderbook_snapshot(self):
        """Connector fetches a real order book snapshot with correct shape."""
        async def fetch_ob():
            api_factory = self._make_api_factory()
            rest = await api_factory.get_rest_assistant()
            return await rest.execute_request(
                url=web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL),
                params={"symbol": "BTC/USDT", "limit": "10"},
                method=RESTMethod.GET,
                throttler_limit_id=CONSTANTS.MARKET_ORDERBOOK_PATH_URL,
            )
        result = self._run(fetch_ob())
        self.assertIn("asks", result)
        self.assertIn("bids", result)
        self.assertIsInstance(result["asks"], list)
        self.assertIsInstance(result["bids"], list)
        self.assertGreater(len(result["asks"]), 0)
        self.assertGreater(len(result["bids"]), 0)

    # ── WebSocket Tests ─────────────────────────────────────────────────

    def test_public_ws_orderbook_snapshot(self):
        """Connect to NonKYC WS and receive an orderbook snapshot."""
        async def check():
            async with websockets.connect("wss://ws.nonkyc.io") as ws:
                sub = {"method": "subscribeOrderbook", "params": {"symbol": "BTC/USDT"}, "id": 1}
                await ws.send(json.dumps(sub))
                for _ in range(15):
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(msg)
                    if data.get("method") == "snapshotOrderbook":
                        return data
                return None
        result = self._run(check())
        self.assertIsNotNone(result, "Did not receive snapshotOrderbook within timeout")
        params = result.get("params", {})
        self.assertIn("asks", params)
        self.assertIn("bids", params)
        self.assertIn("sequence", params)
        self.assertIn("symbol", params)
        self.assertEqual("BTC/USDT", params["symbol"])
        # NonKYC returns sequence as a string — cast to int for validation
        sequence = int(params["sequence"])
        self.assertGreater(sequence, 0)
        self.assertGreater(len(params["asks"]), 0)
        self.assertGreater(len(params["bids"]), 0)

    def test_public_ws_orderbook_update_follows_snapshot(self):
        """After snapshot, subsequent diffs have increasing sequence."""
        async def check():
            async with websockets.connect("wss://ws.nonkyc.io") as ws:
                sub = {"method": "subscribeOrderbook", "params": {"symbol": "BTC/USDT"}, "id": 1}
                await ws.send(json.dumps(sub))
                snapshot_seq = None
                for _ in range(30):
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(msg)
                    method = data.get("method")
                    if method == "snapshotOrderbook":
                        snapshot_seq = int(data["params"]["sequence"])
                    elif method == "updateOrderbook" and snapshot_seq is not None:
                        update_seq = int(data["params"]["sequence"])
                        return snapshot_seq, update_seq
                return snapshot_seq, None
        snapshot_seq, update_seq = self._run(check())
        self.assertIsNotNone(snapshot_seq, "Did not receive snapshot")
        self.assertIsNotNone(update_seq, "Did not receive update after snapshot")
        self.assertGreater(update_seq, snapshot_seq)

    def test_public_ws_orderbook_via_production_data_source(self):
        """Validate orderbook subscription through the production WS assistant path."""
        async def check():
            api_factory = self._make_api_factory()
            ws = await api_factory.get_ws_assistant()
            await ws.connect(ws_url=CONSTANTS.WS_URL,
                             ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL)
            try:
                ob_payload = {
                    "method": CONSTANTS.WS_METHOD_SUBSCRIBE_ORDERBOOK,
                    "params": {"symbol": "BTC/USDT", "limit": CONSTANTS.ORDERBOOK_DEPTH},
                    "id": 1,
                }
                await ws.send(WSJSONRequest(payload=ob_payload))
                async for ws_response in ws.iter_messages():
                    data = ws_response.data
                    if isinstance(data, dict) and data.get("method") == CONSTANTS.SNAPSHOT_EVENT_TYPE:
                        return data
                return None
            finally:
                await ws.disconnect()
        result = self._run(check())
        self.assertIsNotNone(result, "Did not receive snapshotOrderbook via production WS path")
        params = result.get("params", {})
        self.assertIn("asks", params)
        self.assertIn("bids", params)
        self.assertIn("sequence", params)


if __name__ == "__main__":
    unittest.main()
