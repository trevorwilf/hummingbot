"""
Phase 6 unit tests: Rate oracle source registration.

Tests NonkycRateSource implementation and RATE_ORACLE_SOURCES registration.
"""
import asyncio
import json
import re
import unittest
from decimal import Decimal
from typing import Awaitable
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.rate_oracle.rate_oracle import RATE_ORACLE_SOURCES


class TestPhase6RateOracle(unittest.TestCase):
    """Tests for NonKYC rate oracle source."""

    def async_run(self, coro: Awaitable):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- Test 1: NonKYC registered in RATE_ORACLE_SOURCES --

    def test_nonkyc_in_rate_oracle_sources(self):
        """Verify 'nonkyc' is a key in RATE_ORACLE_SOURCES."""
        self.assertIn("nonkyc", RATE_ORACLE_SOURCES)

    # -- Test 2: Rate source class can be instantiated --

    def test_rate_source_instantiation(self):
        """Verify NonkycRateSource can be created."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource
        source = NonkycRateSource()
        self.assertEqual(source.name, "nonkyc")

    # -- Test 3: Rate source name property --

    def test_rate_source_name(self):
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource
        source = NonkycRateSource()
        self.assertEqual(source.name, "nonkyc")

    # -- Test 4: get_prices with valid ticker data --

    @aioresponses()
    def test_get_prices_returns_mid_prices(self, mock_api):
        """Valid tickers -> mid price = (bid + ask) / 2."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([
            {
                "ticker_id": "BTC_USDT",
                "base_currency": "BTC",
                "target_currency": "USDT",
                "bid": "65000.00",
                "ask": "65100.00",
                "last_price": "65050.00",
            },
            {
                "ticker_id": "ETH_USDT",
                "base_currency": "ETH",
                "target_currency": "USDT",
                "bid": "3400.00",
                "ask": "3410.00",
                "last_price": "3405.00",
            },
        ]))

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices())

        self.assertIn("BTC-USDT", prices)
        self.assertIn("ETH-USDT", prices)
        self.assertEqual(prices["BTC-USDT"], Decimal("65050.00"))
        self.assertEqual(prices["ETH-USDT"], Decimal("3405.00"))

    # -- Test 5: get_prices skips zero bid/ask --

    @aioresponses()
    def test_get_prices_skips_zero_bid_ask(self, mock_api):
        """Pairs with bid=0 or ask=0 are excluded."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([
            {
                "ticker_id": "BTC_USDT",
                "base_currency": "BTC",
                "target_currency": "USDT",
                "bid": "65000.00",
                "ask": "65100.00",
            },
            {
                "ticker_id": "DEAD_USDT",
                "base_currency": "DEAD",
                "target_currency": "USDT",
                "bid": "0",
                "ask": "0",
            },
        ]))

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices())

        self.assertIn("BTC-USDT", prices)
        self.assertNotIn("DEAD-USDT", prices)

    # -- Test 6: get_prices with quote_token filter --

    @aioresponses()
    def test_get_prices_filters_by_quote_token(self, mock_api):
        """When quote_token is specified, only matching pairs are returned."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([
            {
                "ticker_id": "BTC_USDT",
                "base_currency": "BTC",
                "target_currency": "USDT",
                "bid": "65000.00",
                "ask": "65100.00",
            },
            {
                "ticker_id": "ETH_BTC",
                "base_currency": "ETH",
                "target_currency": "BTC",
                "bid": "0.0530",
                "ask": "0.0535",
            },
        ]))

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices(quote_token="USDT"))

        self.assertIn("BTC-USDT", prices)
        self.assertNotIn("ETH-BTC", prices)

    # -- Test 7: get_prices skips crossed books --

    @aioresponses()
    def test_get_prices_skips_crossed_book(self, mock_api):
        """Pairs where bid > ask are excluded."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([
            {
                "ticker_id": "BTC_USDT",
                "base_currency": "BTC",
                "target_currency": "USDT",
                "bid": "65100.00",
                "ask": "65000.00",  # Crossed: bid > ask
            },
        ]))

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices())

        self.assertNotIn("BTC-USDT", prices)

    # -- Test 8: get_prices handles API error gracefully --

    @aioresponses()
    def test_get_prices_handles_api_error(self, mock_api):
        """If /tickers fails, return empty dict instead of crashing."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=500, body="Internal Server Error")

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices())

        self.assertEqual(prices, {})

    # -- Test 9: get_prices skips entries with missing base/target --

    @aioresponses()
    def test_get_prices_skips_missing_currencies(self, mock_api):
        """Tickers missing base_currency or target_currency are skipped."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([
            {
                "ticker_id": "BROKEN",
                "bid": "100.00",
                "ask": "101.00",
                # Missing base_currency and target_currency
            },
            {
                "ticker_id": "BTC_USDT",
                "base_currency": "BTC",
                "target_currency": "USDT",
                "bid": "65000.00",
                "ask": "65100.00",
            },
        ]))

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices())

        self.assertEqual(len(prices), 1)
        self.assertIn("BTC-USDT", prices)

    # -- Test 10: _build_nonkyc_connector_without_private_keys --

    def test_build_connector_without_keys(self):
        """Connector created by rate source has empty keys and trading_required=False."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        source = NonkycRateSource()
        source._ensure_exchange()

        self.assertIsNotNone(source._exchange)
        self.assertEqual(source._exchange.api_key, "")
        self.assertEqual(source._exchange.secret_key, "")
        self.assertFalse(source._exchange.is_trading_required)

    # -- Test 11: RATE_ORACLE_SOURCES class is correct type --

    def test_rate_source_class_is_correct(self):
        """The registered class is NonkycRateSource."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource
        self.assertIs(RATE_ORACLE_SOURCES["nonkyc"], NonkycRateSource)

    # -- Test 12: Rate source inherits RateSourceBase --

    def test_rate_source_inherits_base(self):
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource
        from hummingbot.core.rate_oracle.sources.rate_source_base import RateSourceBase
        self.assertTrue(issubclass(NonkycRateSource, RateSourceBase))

    # -- Test 13: Multiple valid pairs computed correctly --

    @aioresponses()
    def test_get_prices_many_pairs(self, mock_api):
        """Verify multiple pairs including small-cap are handled."""
        from hummingbot.core.rate_oracle.sources.nonkyc_rate_source import NonkycRateSource

        url = web_utils.public_rest_url(CONSTANTS.TICKER_BOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([
            {"ticker_id": "BTC_USDT", "base_currency": "BTC", "target_currency": "USDT",
             "bid": "65000", "ask": "65100"},
            {"ticker_id": "SAL_USDT", "base_currency": "SAL", "target_currency": "USDT",
             "bid": "0.0210", "ask": "0.0212"},
            {"ticker_id": "ETH_BTC", "base_currency": "ETH", "target_currency": "BTC",
             "bid": "0.0530", "ask": "0.0535"},
        ]))

        source = NonkycRateSource()
        prices = self.async_run(source.get_prices())

        self.assertEqual(len(prices), 3)
        self.assertEqual(prices["BTC-USDT"], Decimal("65050"))
        self.assertEqual(prices["SAL-USDT"], Decimal("0.0211"))
        self.assertEqual(prices["ETH-BTC"], Decimal("0.05325"))


if __name__ == "__main__":
    unittest.main()
