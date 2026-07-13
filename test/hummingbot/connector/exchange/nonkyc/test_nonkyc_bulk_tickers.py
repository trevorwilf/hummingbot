"""Bulk /tickers snapshot tests (the 2026-07-13 429 storm).

hummingbot-api's ticker pool asks the connector for the last price of EVERY listed
pair (~400) every 30 seconds. The base ExchangePyBase implementation fans that out
into one GET /ticker/{symbol} per pair, which tripped NonKYC's rate limit instantly
and 429'd every endpoint, including the fallbacks.

Under test:
- get_last_traded_prices with more than one pair is served from ONE GET /tickers call
- the snapshot is cached for _TICKERS_SNAPSHOT_TTL_S and refreshed after it lapses
- concurrent bulk callers share a single in-flight request (no stampede)
- a single-pair request keeps the fresher per-symbol endpoint
- the per-pair fallback path reuses the shared snapshot (N failing pairs -> 1 request)
- malformed rows and unknown pairs are skipped, never fatal
"""
import asyncio
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock

from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange

TICKERS_RESPONSE = [
    {"ticker_id": "ARRR_USDT", "last_price": "0.1234"},
    {"ticker_id": "XMR_USDT", "last_price": "335.5"},
    {"ticker_id": "DASH_USDT", "last_price": "24.18"},
    {"ticker_id": "BROKEN_USDT"},                              # no last_price -> skipped
    {"last_price": "1.0"},                                     # no ticker_id  -> skipped
    {"ticker_id": "BAD_USDT", "last_price": "not-a-number"},   # unparsable    -> skipped
]


class TestBulkTickers(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["ARRR-USDT"],
            trading_required=False,
        )
        self.exchange._set_trading_pair_symbol_map(bidict({
            "ARRR/USDT": "ARRR-USDT",
            "XMR/USDT": "XMR-USDT",
            "DASH/USDT": "DASH-USDT",
        }))
        self._now = 1_000.0
        self.exchange._time = lambda: self._now
        self.tickers_requests = 0
        self.primary_requests = []

        async def fake_api_request(method=None, path_url=None, **kwargs):
            if path_url == CONSTANTS.TICKER_BOOK_PATH_URL:
                self.tickers_requests += 1
                return TICKERS_RESPONSE
            if path_url.startswith(f"{CONSTANTS.TICKER_INFO_PATH_URL}/"):
                self.primary_requests.append(path_url)
                return {"last_price": "0.1111"}
            raise AssertionError(f"unexpected path: {path_url}")

        self.exchange._api_request = AsyncMock(side_effect=fake_api_request)

    async def test_multi_pair_request_uses_single_tickers_call(self):
        prices = await self.exchange.get_last_traded_prices(
            ["ARRR-USDT", "XMR-USDT", "DASH-USDT"])
        self.assertEqual(1, self.tickers_requests)
        self.assertEqual([], self.primary_requests)  # no per-pair fan-out
        self.assertEqual(
            {"ARRR-USDT": 0.1234, "XMR-USDT": 335.5, "DASH-USDT": 24.18}, prices)

    async def test_snapshot_cached_within_ttl_and_refreshed_after(self):
        await self.exchange.get_last_traded_prices(["ARRR-USDT", "XMR-USDT"])
        await self.exchange.get_last_traded_prices(["DASH-USDT", "XMR-USDT"])
        self.assertEqual(1, self.tickers_requests)      # inside TTL -> cached
        self._now += self.exchange._TICKERS_SNAPSHOT_TTL_S + 1
        await self.exchange.get_last_traded_prices(["ARRR-USDT", "XMR-USDT"])
        self.assertEqual(2, self.tickers_requests)      # TTL lapsed -> refreshed

    async def test_concurrent_bulk_calls_share_one_request(self):
        await asyncio.gather(
            self.exchange.get_last_traded_prices(["ARRR-USDT", "XMR-USDT"]),
            self.exchange.get_last_traded_prices(["DASH-USDT", "XMR-USDT"]),
            self.exchange.get_last_traded_prices(["ARRR-USDT", "DASH-USDT"]),
        )
        self.assertEqual(1, self.tickers_requests)

    async def test_single_pair_keeps_the_fresher_primary_endpoint(self):
        price = await self.exchange.get_last_traded_prices(["ARRR-USDT"])
        self.assertEqual({"ARRR-USDT": 0.1111}, price)
        self.assertEqual(0, self.tickers_requests)
        self.assertEqual([f"{CONSTANTS.TICKER_INFO_PATH_URL}/ARRR/USDT"], self.primary_requests)

    async def test_unknown_pair_is_skipped_not_fatal(self):
        prices = await self.exchange.get_last_traded_prices(
            ["ARRR-USDT", "GONE-USDT", "XMR-USDT"])
        self.assertEqual({"ARRR-USDT": 0.1234, "XMR-USDT": 335.5}, prices)

    async def test_pair_missing_from_snapshot_is_omitted(self):
        self.exchange._set_trading_pair_symbol_map(bidict({
            "ARRR/USDT": "ARRR-USDT",
            "NEW/USDT": "NEW-USDT",      # mapped, but the exchange returns no ticker row
            "XMR/USDT": "XMR-USDT",
        }))
        prices = await self.exchange.get_last_traded_prices(
            ["ARRR-USDT", "NEW-USDT", "XMR-USDT"])
        self.assertNotIn("NEW-USDT", prices)
        self.assertEqual(0.1234, prices["ARRR-USDT"])

    async def test_malformed_rows_are_ignored(self):
        prices = await self.exchange.get_last_traded_prices(["ARRR-USDT", "XMR-USDT"])
        snapshot = self.exchange._tickers_snapshot_cache
        self.assertNotIn("BROKEN_USDT", snapshot)
        self.assertNotIn("BAD_USDT", snapshot)
        self.assertEqual(2, len(prices))


class TestPerPairFallbackSharesSnapshot(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["ARRR-USDT"],
            trading_required=False,
        )
        self.exchange._set_trading_pair_symbol_map(bidict({
            "ARRR/USDT": "ARRR-USDT",
            "XMR/USDT": "XMR-USDT",
        }))
        self.exchange._time = lambda: 1_000.0
        self.tickers_requests = 0

        async def fake_api_request(method=None, path_url=None, **kwargs):
            if path_url == CONSTANTS.TICKER_BOOK_PATH_URL:
                self.tickers_requests += 1
                return TICKERS_RESPONSE
            raise IOError("HTTP status is 429")  # every primary per-symbol call fails

        self.exchange._api_request = AsyncMock(side_effect=fake_api_request)

    async def test_failing_pairs_share_one_snapshot_fetch(self):
        p1 = await self.exchange._get_last_traded_price("ARRR-USDT")
        p2 = await self.exchange._get_last_traded_price("XMR-USDT")
        self.assertEqual(0.1234, p1)
        self.assertEqual(335.5, p2)
        self.assertEqual(1, self.tickers_requests)  # shared snapshot, not one per pair

    async def test_pair_absent_everywhere_still_raises(self):
        self.exchange._set_trading_pair_symbol_map(bidict({
            "ARRR/USDT": "ARRR-USDT",
            "GONE/USDT": "GONE-USDT",
        }))
        with self.assertRaises(ValueError):
            await self.exchange._get_last_traded_price("GONE-USDT")


if __name__ == "__main__":
    import unittest
    unittest.main()
