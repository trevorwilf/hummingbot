"""
MEXC Public Contract Smoke Tests

These tests validate public (no API key required) MEXC REST and WebSocket
endpoints against the live exchange. They catch schema drift, field renames,
and protocol changes before they break the connector at runtime.

These are NOT part of the per-PR blocking lane. Run daily or pre-release.
"""
import asyncio
import json
import unittest

import pytest

pytestmark = pytest.mark.live_api


class TestMexcPublicContractSmoke(unittest.TestCase):
    """Public-only smoke tests -- no API key required."""

    BASE_URL = "https://api.mexc.com"

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=30))
        finally:
            loop.close()

    def test_exchange_info_schema(self):
        """GET /api/v3/exchangeInfo returns expected top-level fields."""
        import aiohttp

        async def fetch():
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.BASE_URL}/api/v3/exchangeInfo") as resp:
                    self.assertEqual(200, resp.status)
                    data = await resp.json()
                    return data

        data = self._run(fetch())
        self.assertIn("symbols", data)
        self.assertIsInstance(data["symbols"], list)
        if data["symbols"]:
            sym = data["symbols"][0]
            self.assertIn("symbol", sym)
            self.assertIn("status", sym)
            self.assertIn("baseAsset", sym)
            self.assertIn("quoteAsset", sym)

    def test_ticker_24hr_schema(self):
        """GET /api/v3/ticker/24hr returns expected fields and types."""
        import aiohttp

        async def fetch():
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/api/v3/ticker/24hr",
                    params={"symbol": "BTCUSDT"}
                ) as resp:
                    self.assertEqual(200, resp.status)
                    data = await resp.json()
                    return data

        data = self._run(fetch())
        # Can be a list or single object
        if isinstance(data, list):
            data = data[0]
        self.assertIn("symbol", data)
        self.assertIn("lastPrice", data)
        self.assertIn("volume", data)

    def test_depth_snapshot_schema(self):
        """GET /api/v3/depth returns bids/asks in expected format."""
        import aiohttp

        async def fetch():
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/api/v3/depth",
                    params={"symbol": "BTCUSDT", "limit": "5"}
                ) as resp:
                    self.assertEqual(200, resp.status)
                    data = await resp.json()
                    return data

        data = self._run(fetch())
        self.assertIn("bids", data)
        self.assertIn("asks", data)
        self.assertIsInstance(data["bids"], list)
        self.assertIsInstance(data["asks"], list)
        if data["bids"]:
            # Each bid/ask should be [price, quantity]
            self.assertEqual(2, len(data["bids"][0]))

    def test_public_websocket_trade_stream(self):
        """Public WS trade stream connects and receives data."""
        import websockets

        async def connect():
            uri = "wss://wbs.mexc.com/ws"
            async with websockets.connect(uri) as ws:
                # Subscribe to BTC/USDT trades
                sub_msg = {
                    "method": "SUBSCRIPTION",
                    "params": ["spot@public.deals.v3.api@BTCUSDT"],
                    "id": 1
                }
                await ws.send(json.dumps(sub_msg))
                # Wait for a response (subscription ack or data)
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(response)
                return data

        data = self._run(connect())
        # Should receive some response (ack or data)
        self.assertIsNotNone(data)


if __name__ == "__main__":
    unittest.main()
