# -*- coding: ascii -*-
"""
Kraken connector LIVE read-only smoke test
==========================================
Location: test/hummingbot/connector/exchange/kraken/test_kraken_live_api.py

Validates the REAL Kraken connector code path (auth signing, nonce, request/response parsing,
WebSocket token + private stream, public market data) against the live Kraken API using the
actual KrakenExchange / data-source classes.

SAFETY -- THIS TEST IS STRICTLY READ-ONLY.
It NEVER places, edits or cancels orders, and never withdraws or transfers funds. Only read-only
endpoints are exercised: Balance, OpenOrders, GetWebSocketsToken, public Ticker/Depth/OHLC, plus the
private user-stream (listen only). Do not add order-mutating calls to this file.

Setup:
  1. pip install python-dotenv
  2. Add to repo-root .env:
       KRAKEN_API_KEY=your_key
       KRAKEN_API_SECRET=your_secret
  3. Run:  python test/hummingbot/connector/exchange/kraken/test_kraken_live_api.py
     or:   pytest test/hummingbot/connector/exchange/kraken/test_kraken_live_api.py -v
Tests skip automatically when credentials are absent or the connector cannot be imported
(e.g. Cython extensions not built).
"""
import asyncio
import os
import unittest
from decimal import Decimal
from pathlib import Path


def _find_repo_root():
    current = Path(__file__).resolve().parent
    for _ in range(12):
        if (current / ".env").exists() or (current / ".git").exists():
            return current
        current = current.parent
    return Path(__file__).resolve().parent


try:
    from dotenv import load_dotenv
    load_dotenv(_find_repo_root() / ".env")
except ImportError:
    pass

API_KEY = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")

try:
    from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS
    from hummingbot.connector.exchange.kraken.kraken_api_order_book_data_source import (
        KrakenAPIOrderBookDataSource,
    )
    from hummingbot.connector.exchange.kraken.kraken_api_user_stream_data_source import (
        KrakenAPIUserStreamDataSource,
    )
    from hummingbot.connector.exchange.kraken.kraken_exchange import KrakenExchange
    from hummingbot.core.web_assistant.connections.data_types import RESTMethod
    _IMPORT_OK = True
except Exception:  # pragma: no cover - environment dependent (Cython not built, etc.)
    _IMPORT_OK = False


@unittest.skipUnless(_IMPORT_OK, "Kraken connector could not be imported (build Cython extensions).")
@unittest.skipUnless(API_KEY and API_SECRET, "KRAKEN_API_KEY / KRAKEN_API_SECRET not set in .env.")
class KrakenLiveReadOnlyTests(unittest.TestCase):
    """Every test here is read-only. No order placement / cancellation / withdrawal, ever.

    All tests share one event loop and one connector instance so the underlying aiohttp session
    stays bound to a single live loop across the suite.
    """

    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        cls.exchange = KrakenExchange(
            kraken_api_key=API_KEY,
            kraken_secret_key=API_SECRET,
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        # Preflight gate: SKIP (not fail) the whole suite when a public Time call can't complete. This
        # covers (a) no network / sandboxed CI runs and (b) running inside the full async test directory
        # where another test's loop management leaves no usable loop for this custom-loop suite. This file
        # is intended to be run on its own (python test_kraken_live_api.py or pytest on this file), where
        # all checks execute and pass. A public Time call needs no auth and fails fast.
        try:
            cls.loop.run_until_complete(asyncio.wait_for(
                cls.exchange._api_request_with_retry(
                    method=RESTMethod.GET, path_url=CONSTANTS.TIME_PATH_URL),
                timeout=10))
        except Exception as exc:
            try:
                cls.loop.run_until_complete(
                    cls.exchange._web_assistants_factory._connections_factory.close())
            except Exception:
                pass
            cls.loop.close()
            raise unittest.SkipTest(
                f"Kraken live preflight failed ({type(exc).__name__}); run this file standalone for live checks.")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.loop.run_until_complete(
                cls.exchange._web_assistants_factory._connections_factory.close())
        except Exception:
            pass
        try:
            cls.loop.close()
        except Exception:
            pass

    def _run(self, coro, timeout=30):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout))

    # --- private (auth) read-only ---

    def test_auth_balance_readonly(self):
        # A successful private Balance call proves the nonce + HMAC signature are valid live.
        result = self._run(self.exchange._api_request_with_retry(
            method=RESTMethod.POST, path_url=CONSTANTS.BALANCE_PATH_URL, is_auth_required=True))
        self.assertIsInstance(result, dict)
        for amt in result.values():
            Decimal(amt)  # every value must parse as a Decimal amount

    def test_open_orders_readonly(self):
        result = self._run(self.exchange._api_request_with_retry(
            method=RESTMethod.POST, path_url=CONSTANTS.OPEN_ORDERS_PATH_URL, is_auth_required=True))
        self.assertIn("open", result)
        self.assertIsInstance(result["open"], dict)

    def test_update_balances_has_no_phantom_subbalances(self):
        # BAL-LIVE-1 regression on the live account: staked (.S) / bonded (.B) / on-hold (.HOLD)
        # sub-balances must not leak into available as phantom tradable assets.
        self._run(self.exchange._update_balances())
        leaked = [a for a in self.exchange.available_balances if "." in a]
        self.assertEqual([], leaked, f"Non-spot sub-balances leaked into available: {leaked}")

    def test_ws_auth_token(self):
        uss = KrakenAPIUserStreamDataSource(self.exchange, api_factory=self.exchange._web_assistants_factory)
        token = self._run(uss.get_auth_token())
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 10)

    def test_private_ws_stream_first_frame(self):
        # Listen-only: connect to the private WS, subscribe, and read the initial openOrders snapshot.
        async def _listen():
            uss = KrakenAPIUserStreamDataSource(self.exchange,
                                                api_factory=self.exchange._web_assistants_factory)
            q = asyncio.Queue()
            task = asyncio.create_task(uss.listen_for_user_stream(q))
            try:
                return await asyncio.wait_for(q.get(), timeout=20)
            finally:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        msg = self._run(_listen(), timeout=30)
        self.assertIsInstance(msg, list)
        self.assertIn(msg[-2], (CONSTANTS.USER_ORDERS_ENDPOINT_NAME, CONSTANTS.USER_TRADES_ENDPOINT_NAME))

    # --- public read-only ---

    def test_public_last_traded_price(self):
        prices = self._run(self.exchange.get_last_traded_prices(["BTC-USDT"]))
        self.assertIn("BTC-USDT", prices)
        self.assertGreater(prices["BTC-USDT"], 0)

    def test_trading_rules_parse(self):
        self._run(self.exchange._update_trading_rules())
        self.assertIn("BTC-USDT", self.exchange.trading_rules)
        rule = self.exchange.trading_rules["BTC-USDT"]
        self.assertGreater(rule.min_price_increment, Decimal("0"))
        self.assertGreater(rule.min_base_amount_increment, Decimal("0"))

    def test_order_book_snapshot(self):
        ds = KrakenAPIOrderBookDataSource(trading_pairs=["BTC-USDT"], connector=self.exchange,
                                          api_factory=self.exchange._web_assistants_factory)
        snap = self._run(ds._order_book_snapshot("BTC-USDT"))
        self.assertGreater(len(snap.bids), 0)
        self.assertGreater(len(snap.asks), 0)
        # A coherent book: best bid strictly below best ask.
        self.assertLess(snap.bids[0].price, snap.asks[0].price)


if __name__ == "__main__":
    if not (API_KEY and API_SECRET):
        print("SKIP: set KRAKEN_API_KEY / KRAKEN_API_SECRET in .env to run live tests.")
    else:
        unittest.main(verbosity=2)
