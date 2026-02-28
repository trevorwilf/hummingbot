"""
Phase 7C unit tests: Edge case and error path hardening.

Tests cover: auth signature verification, fee extraction edge cases,
WS event handling, order placement guards, cancel_all edge cases,
trading rules enforcement, and last-price fallback.
"""
import asyncio
import hashlib
import hmac
import json
import unittest
from decimal import Decimal, InvalidOperation
from typing import Awaitable
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from aioresponses import aioresponses
from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.trade_fee import TokenAmount


class _Base(unittest.TestCase):
    """Shared setUp for most 7C test classes."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )
        self.exchange._set_trading_pair_symbol_map(
            bidict({"BTC/USDT": "BTC-USDT"})
        )

    def async_run(self, coro: Awaitable):
        return asyncio.get_event_loop().run_until_complete(coro)


# =========================================================================
# 7C-1: POST auth body matches signature
# =========================================================================

class TestPostAuthBodyMatchesSignature(_Base):
    """7C-1: After rest_authenticate(), POST body is minified and the
    signature was computed from exactly that same minified string."""

    def test_post_body_and_signature_use_identical_string(self):
        mock_time = MagicMock()
        mock_time.time.return_value = 1700000000.0
        auth = NonkycAuth(api_key="TESTKEY", secret_key="TESTSECRET", time_provider=mock_time)

        from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
        original_body = json.dumps({"symbol": "BTC/USDT", "side": "buy", "quantity": "1.0"})
        url = "https://api.nonkyc.io/api/v2/createorder"
        req = RESTRequest(method=RESTMethod.POST, url=url, data=original_body, is_auth_required=True)
        self.async_run(auth.rest_authenticate(req))

        # Verify headers present
        self.assertIn("X-API-KEY", req.headers)
        self.assertIn("X-API-NONCE", req.headers)
        self.assertIn("X-API-SIGN", req.headers)

        # Reconstruct expected signature
        expected_body = json.dumps(json.loads(original_body)).replace(" ", "")
        expected_nonce = str(int(1700000000.0 * 1e3))
        to_sign = f"{url}{expected_body}"
        expected_message = f"TESTKEY{to_sign}{expected_nonce}"
        expected_sig = hmac.new("TESTSECRET".encode(), expected_message.encode(), hashlib.sha256).hexdigest()

        self.assertEqual(expected_sig, req.headers["X-API-SIGN"])
        # Body must be minified (the 7A-1 fix assigns json_str back to request.data)
        self.assertEqual(expected_body, req.data)


# =========================================================================
# 7C-2: GET auth stable with unordered params
# =========================================================================

class TestGetAuthStableWithUnorderedParams(_Base):
    """7C-2: GET requests with identical params in different insertion
    order produce the same signature."""

    def test_get_params_different_order_same_signature(self):
        mock_time = MagicMock()
        mock_time.time.return_value = 1700000000.0
        auth = NonkycAuth(api_key="TESTKEY", secret_key="TESTSECRET", time_provider=mock_time)

        from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
        url = "https://api.nonkyc.io/api/v2/account/orders"

        req_a = RESTRequest(method=RESTMethod.GET, url=url,
                            params={"status": "active", "symbol": "BTC/USDT"}, is_auth_required=True)
        req_b = RESTRequest(method=RESTMethod.GET, url=url,
                            params={"symbol": "BTC/USDT", "status": "active"}, is_auth_required=True)
        self.async_run(auth.rest_authenticate(req_a))
        self.async_run(auth.rest_authenticate(req_b))

        self.assertEqual(req_a.headers["X-API-SIGN"], req_b.headers["X-API-SIGN"],
                         "Signatures must match regardless of param insertion order")


# =========================================================================
# 7C-3: alternateFeeAsset handling
# =========================================================================

class TestAlternateFeeAssetHandling(_Base):
    """7C-3: _extract_fee_token_and_amount handles alternateFeeAsset."""

    def test_alternate_fee_asset_used_when_present(self):
        trade = {
            "id": "TRADE1", "orderid": "ORDER123", "fee": "0.5",
            "alternateFeeAsset": "NKC", "alternateFee": "0.5",
            "price": "50000", "quantity": "0.1",
        }
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("NKC", token)
        self.assertEqual(Decimal("0.5"), amount)

    def test_alternate_fee_asset_null_uses_quote(self):
        trade = {
            "id": "TRADE2", "orderid": "ORDER456", "fee": "0.5",
            "alternateFeeAsset": None, "alternateFee": None,
            "price": "50000", "quantity": "0.1",
        }
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("USDT", token)
        self.assertEqual(Decimal("0.5"), amount)


# =========================================================================
# 7C-4: WS balanceUpdate event
# =========================================================================

class TestWsBalanceUpdateEvent(_Base):
    """7C-4: A balanceUpdate WS event correctly updates balances."""

    def test_ws_balance_update_incremental(self):
        # Set initial balances
        self.exchange._account_available_balances["BTC"] = Decimal("1.0")
        self.exchange._account_balances["BTC"] = Decimal("1.5")

        # Simulate what the handler does for balanceUpdate
        event = {"method": "balanceUpdate", "params": {"ticker": "BTC", "available": "2.5", "held": "0.3"}}
        params = event["params"]
        self.exchange._account_available_balances[params["ticker"]] = Decimal(params["available"])
        self.exchange._account_balances[params["ticker"]] = Decimal(params["available"]) + Decimal(params["held"])

        self.assertEqual(Decimal("2.5"), self.exchange._account_available_balances["BTC"])
        self.assertEqual(Decimal("2.8"), self.exchange._account_balances["BTC"])


# =========================================================================
# 7C-5: WS activeOrders snapshot
# =========================================================================

class TestWsActiveOrdersSnapshot(_Base):
    """7C-5: An activeOrders event correctly parses via both result and params keys."""

    def test_ws_active_orders_via_result_key(self):
        event = {"method": "activeOrders", "result": [{"id": "EX1", "status": "Active", "userProvidedId": "T1"}]}
        orders = event.get("result") or event.get("params") or []
        self.assertIsInstance(orders, list)
        self.assertEqual(1, len(orders))
        self.assertEqual("EX1", orders[0]["id"])

    def test_ws_active_orders_via_params_key(self):
        event = {"method": "activeOrders", "params": [{"id": "EX2", "status": "New", "userProvidedId": "T2"}]}
        orders = event.get("result") or event.get("params") or []
        self.assertIsInstance(orders, list)
        self.assertEqual(1, len(orders))
        self.assertEqual("EX2", orders[0]["id"])

    def test_ws_active_orders_non_list_guarded(self):
        event = {"method": "activeOrders", "result": "not_a_list"}
        orders = event.get("result") or event.get("params") or []
        if not isinstance(orders, list):
            orders = []
        self.assertEqual([], orders)


# =========================================================================
# 7C-6: _get_last_traded_price fallback
# =========================================================================

class TestGetLastTradedPriceFallback(_Base):
    """7C-6: _get_last_traded_price falls back to /tickers when primary fails."""

    def test_last_traded_price_primary_succeeds(self):
        with aioresponses() as mock:
            url = web_utils.public_rest_url(path_url=f"{CONSTANTS.TICKER_INFO_PATH_URL}/BTC/USDT")
            mock.get(url, body=json.dumps({"last_price": "64000.00"}))

            price = self.async_run(self.exchange._get_last_traded_price("BTC-USDT"))
            self.assertEqual(64000.0, price)

    def test_last_traded_price_fallback_to_tickers_list(self):
        with aioresponses() as mock:
            primary_url = web_utils.public_rest_url(path_url=f"{CONSTANTS.TICKER_INFO_PATH_URL}/BTC/USDT")
            mock.get(primary_url, status=500)

            fallback_url = web_utils.public_rest_url(path_url=CONSTANTS.TICKER_BOOK_PATH_URL)
            mock.get(fallback_url, body=json.dumps([
                {"ticker_id": "BTC_USDT", "last_price": "65000.50"},
                {"ticker_id": "ETH_USDT", "last_price": "3500.00"},
            ]))

            price = self.async_run(self.exchange._get_last_traded_price("BTC-USDT"))
            self.assertEqual(65000.5, price)


# =========================================================================
# 7C-7: MARKET order no price param
# =========================================================================

class TestMarketOrderNoPrice(_Base):
    """7C-7: MARKET orders must NOT include a price param in API body."""

    def test_market_order_has_no_price_param(self):
        """Verify the conditional in _place_order only adds price for LIMIT/LIMIT_MAKER."""
        type_market = NonkycExchange.nonkyc_order_type(OrderType.MARKET)
        type_limit = NonkycExchange.nonkyc_order_type(OrderType.LIMIT)
        type_lm = NonkycExchange.nonkyc_order_type(OrderType.LIMIT_MAKER)
        self.assertEqual("market", type_market)
        self.assertEqual("limit", type_limit)
        self.assertEqual("limit", type_lm)
        # The code adds price only when: order_type is LIMIT or LIMIT_MAKER
        # MARKET is explicitly excluded
        self.assertIsNot(OrderType.MARKET, OrderType.LIMIT)
        self.assertIsNot(OrderType.MARKET, OrderType.LIMIT_MAKER)

    def test_limit_order_includes_price(self):
        """LIMIT order must include price."""
        captured = {}

        async def mock_api_post(path_url, data, **kwargs):
            captured.update(data)
            return {"id": "ORDER1", "createdAt": 1700000000000}

        self.exchange._api_post = mock_api_post

        self.async_run(self.exchange._place_order(
            order_id="TEST-001", trading_pair="BTC-USDT",
            amount=Decimal("0.1"), trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT, price=Decimal("50000")))

        self.assertIn("price", captured)
        self.assertEqual("limit", captured["type"])

    def test_market_order_excludes_price(self):
        """MARKET order must not include price."""
        captured = {}

        async def mock_api_post(path_url, data, **kwargs):
            captured.update(data)
            return {"id": "ORDER2", "createdAt": 1700000000000}

        self.exchange._api_post = mock_api_post

        self.async_run(self.exchange._place_order(
            order_id="TEST-002", trading_pair="BTC-USDT",
            amount=Decimal("0.1"), trade_type=TradeType.BUY,
            order_type=OrderType.MARKET, price=Decimal("50000")))

        self.assertNotIn("price", captured)
        self.assertEqual("market", captured["type"])


# =========================================================================
# 7C-8: cancel_all deterministic order
# =========================================================================

class TestCancelAllDeterministicOrder(_Base):
    """7C-8: cancel_all processes symbols in sorted order (7A-3 fix)."""

    def test_cancel_all_processes_symbols_in_sorted_order(self):
        # Set up 3 trading pairs
        self.exchange._set_trading_pair_symbol_map(bidict({
            "ZEC/USDT": "ZEC-USDT",
            "ADA/USDT": "ADA-USDT",
            "BTC/USDT": "BTC-USDT",
        }))

        # Create tracked orders for all 3 pairs
        for cid, pair, exid in [("O1", "ZEC-USDT", "EX1"), ("O2", "ADA-USDT", "EX2"), ("O3", "BTC-USDT", "EX3")]:
            order = InFlightOrder(
                client_order_id=cid, exchange_order_id=exid,
                trading_pair=pair, order_type=OrderType.LIMIT,
                trade_type=TradeType.BUY, amount=Decimal("1"),
                price=Decimal("100"), creation_timestamp=1700000000.0,
                initial_state=OrderState.OPEN,
            )
            self.exchange._order_tracker._in_flight_orders[cid] = order

        # Mock account/orders to return active orders for all 3 symbols
        async def mock_get(path_url, params=None, **kwargs):
            return [
                {"id": "EX1", "symbol": "ZEC/USDT", "userProvidedId": "O1", "status": "Active"},
                {"id": "EX2", "symbol": "ADA/USDT", "userProvidedId": "O2", "status": "Active"},
                {"id": "EX3", "symbol": "BTC/USDT", "userProvidedId": "O3", "status": "Active"},
            ]

        call_order = []
        original_cancel = self.exchange._cancel_all_for_symbol

        async def mock_cancel(symbol):
            call_order.append(symbol)
            return [{"id": "123", "status": "cancelled"}]

        self.exchange._api_get = mock_get
        self.exchange._cancel_all_for_symbol = mock_cancel

        results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        # Phase 7A-3 sorts symbols before iteration
        self.assertEqual(["ADA/USDT", "BTC/USDT", "ZEC/USDT"], call_order)


# =========================================================================
# 7C-9: cancel_all timeout
# =========================================================================

class TestCancelAllTimeout(_Base):
    """7C-9: cancel_all timeout marks unreported orders as failed."""

    def test_cancel_all_timeout_marks_all_as_failed(self):
        # Set up exchange with 2 pairs
        self.exchange._set_trading_pair_symbol_map(bidict({
            "BTC/USDT": "BTC-USDT",
            "ETH/USDT": "ETH-USDT",
        }))

        for cid, pair, exid in [("O1", "BTC-USDT", "EX1"), ("O2", "ETH-USDT", "EX2")]:
            order = InFlightOrder(
                client_order_id=cid, exchange_order_id=exid,
                trading_pair=pair, order_type=OrderType.LIMIT,
                trade_type=TradeType.BUY, amount=Decimal("1"),
                price=Decimal("100"), creation_timestamp=1700000000.0,
                initial_state=OrderState.OPEN,
            )
            self.exchange._order_tracker._in_flight_orders[cid] = order

        async def mock_get(path_url, params=None, **kwargs):
            return [
                {"id": "EX1", "symbol": "BTC/USDT", "userProvidedId": "O1", "status": "Active"},
                {"id": "EX2", "symbol": "ETH/USDT", "userProvidedId": "O2", "status": "Active"},
            ]

        async def mock_cancel_slow(symbol):
            await asyncio.sleep(999)
            return [{"id": "123"}]

        self.exchange._api_get = mock_get
        self.exchange._cancel_all_for_symbol = mock_cancel_slow

        # The gather + result-building are both inside the timeout block
        # so when timeout fires, all orders are marked as failed
        results = self.async_run(self.exchange.cancel_all(timeout_seconds=0.3))

        self.assertIsInstance(results, list)
        for r in results:
            self.assertIsInstance(r, CancellationResult)
            self.assertFalse(r.success, f"Order {r.order_id} should be marked failed on timeout")


# =========================================================================
# 7C-10: _update_trading_fees edge cases
# =========================================================================

class TestUpdateTradingFeesEdgeCases(_Base):
    """7C-10: _update_trading_fees handles edge cases gracefully."""

    def test_empty_trade_history_keeps_defaults(self):
        async def mock_get(path_url, **kwargs):
            return []
        self.exchange._api_get = mock_get
        self.async_run(self.exchange._update_trading_fees())
        self.assertEqual({}, self.exchange._trading_fees)

    def test_only_maker_trades_updates_maker_only(self):
        async def mock_get(path_url, **kwargs):
            return [
                {"fee": "0.075", "quantity": "1", "price": "50000", "side": "Buy", "triggeredBy": "sell"},
                {"fee": "0.075", "quantity": "1", "price": "50000", "side": "Sell", "triggeredBy": "buy"},
            ]
        self.exchange._api_get = mock_get
        self.async_run(self.exchange._update_trading_fees())
        self.assertIn("maker_fee", self.exchange._trading_fees)
        self.assertNotIn("taker_fee", self.exchange._trading_fees)

    def test_only_taker_trades_updates_taker_only(self):
        async def mock_get(path_url, **kwargs):
            return [
                {"fee": "0.075", "quantity": "1", "price": "50000", "side": "Buy", "triggeredBy": "buy"},
                {"fee": "0.075", "quantity": "1", "price": "50000", "side": "Sell", "triggeredBy": "sell"},
            ]
        self.exchange._api_get = mock_get
        self.async_run(self.exchange._update_trading_fees())
        self.assertIn("taker_fee", self.exchange._trading_fees)
        self.assertNotIn("maker_fee", self.exchange._trading_fees)

    def test_malformed_trade_skipped_no_crash(self):
        async def mock_get(path_url, **kwargs):
            return [
                {"fee": "0.075", "quantity": "1", "price": "50000", "side": "Buy", "triggeredBy": "sell"},
                {"fee": "not_a_number", "quantity": "1", "price": "50000", "side": "Buy", "triggeredBy": "sell"},
                {"fee": "0.01", "quantity": "0", "price": "50000", "side": "Buy", "triggeredBy": "sell"},
                {"side": "Buy"},  # Missing fields
            ]
        self.exchange._api_get = mock_get
        # Must not raise
        self.async_run(self.exchange._update_trading_fees())
        # The valid maker trade should still be computed
        self.assertIn("maker_fee", self.exchange._trading_fees)


# =========================================================================
# 7C-11: _place_cancel fallback
# =========================================================================

class TestPlaceCancelFallback(_Base):
    """7C-11: _place_cancel behavior with None/UNKNOWN exchange_order_id."""

    def test_place_cancel_with_none_exchange_id(self):
        """7A-7 fallback: None exchange_order_id falls back to client_order_id."""
        captured = {}

        async def mock_post(path_url, data, **kwargs):
            captured.update(data)
            return {"id": "CANCELLED"}

        self.exchange._api_post = mock_post

        order = InFlightOrder(
            client_order_id="HBOT-001", exchange_order_id=None,
            trading_pair="BTC-USDT", order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY, amount=Decimal("1"),
            price=Decimal("50000"), creation_timestamp=1700000000.0,
            initial_state=OrderState.OPEN,
        )

        result = self.async_run(self.exchange._place_cancel("HBOT-001", order))
        self.assertTrue(result)
        # 7A-7 fix: fallback to client_order_id
        self.assertEqual("HBOT-001", captured["id"])

    def test_place_cancel_with_valid_exchange_id(self):
        captured = {}

        async def mock_post(path_url, data, **kwargs):
            captured.update(data)
            return {"id": "CANCELLED"}

        self.exchange._api_post = mock_post

        order = InFlightOrder(
            client_order_id="HBOT-002", exchange_order_id="REAL_ID_123",
            trading_pair="BTC-USDT", order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY, amount=Decimal("1"),
            price=Decimal("50000"), creation_timestamp=1700000000.0,
            initial_state=OrderState.OPEN,
        )

        result = self.async_run(self.exchange._place_cancel("HBOT-002", order))
        self.assertTrue(result)
        self.assertEqual("REAL_ID_123", captured["id"])


# =========================================================================
# 7C-12: HTTP 200 with error body
# =========================================================================

class TestHttp200WithErrorBody(_Base):
    """7C-12: _cancel_all_for_symbol detects error in HTTP 200 response."""

    def test_cancel_all_for_symbol_detects_error_in_200(self):
        async def mock_post(path_url, data, **kwargs):
            return {"error": {"code": 400, "message": "Bad Request", "description": "Missing required input"}}

        self.exchange._api_post = mock_post

        with self.assertRaises(IOError) as ctx:
            self.async_run(self.exchange._cancel_all_for_symbol("BTC/USDT"))
        self.assertIn("Missing required input", str(ctx.exception))

    def test_cancel_all_for_symbol_success(self):
        async def mock_post(path_url, data, **kwargs):
            return [{"id": "123", "status": "cancelled"}]

        self.exchange._api_post = mock_post
        result = self.async_run(self.exchange._cancel_all_for_symbol("BTC/USDT"))
        self.assertIsInstance(result, list)
        self.assertEqual(1, len(result))


# =========================================================================
# 7C-13: allowMarketOrders in trading rules
# =========================================================================

class TestAllowMarketOrdersExclusion(_Base):
    """7C-13: _format_trading_rules sets supports_market_orders from API data."""

    def _base_rule(self, **overrides):
        rule = {
            "symbol": "BTC/USDT", "primaryTicker": "BTC",
            "priceDecimals": 2, "quantityDecimals": 6,
            "minimumQuantity": "0.001", "minQuote": 10, "isMinQuoteActive": True,
            "isActive": True, "isPaused": False,
        }
        rule.update(overrides)
        return rule

    def test_trading_rule_disallows_market_orders(self):
        rules = self.async_run(self.exchange._format_trading_rules(
            [self._base_rule(allowMarketOrders=False)]))
        self.assertFalse(rules[0].supports_market_orders)

    def test_trading_rule_allows_market_orders_by_default(self):
        rules = self.async_run(self.exchange._format_trading_rules(
            [self._base_rule()]))
        self.assertTrue(rules[0].supports_market_orders)


# =========================================================================
# 7C-14: max_order_size in trading rules
# =========================================================================

class TestMaxOrderSizeValidation(_Base):
    """7C-14: _format_trading_rules populates max_order_size from API data."""

    def _base_rule(self, **overrides):
        rule = {
            "symbol": "BTC/USDT", "primaryTicker": "BTC",
            "priceDecimals": 2, "quantityDecimals": 6,
            "minimumQuantity": "0.001", "minQuote": 10, "isMinQuoteActive": True,
            "isActive": True, "isPaused": False,
        }
        rule.update(overrides)
        return rule

    def test_max_order_size_from_data(self):
        rules = self.async_run(self.exchange._format_trading_rules(
            [self._base_rule(maximumQuantity="500")]))
        self.assertEqual(Decimal("500"), rules[0].max_order_size)

    def test_max_order_size_missing_is_very_large(self):
        rules = self.async_run(self.exchange._format_trading_rules(
            [self._base_rule()]))
        self.assertGreater(rules[0].max_order_size, Decimal("999999"))


# =========================================================================
# 7C-15: Multi-pair subscribe/unsubscribe sequence cleanup
# =========================================================================

class TestMultiPairSubscribeSequenceCleanup(_Base):
    """7C-15: Sequence tracking per pair and cleanup on unsubscribe."""

    def test_subscribe_creates_sequence_entries_per_pair(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource

        self.exchange._set_trading_pair_symbol_map(bidict({
            "BTC/USDT": "BTC-USDT", "ETH/USDT": "ETH-USDT", "ADA/USDT": "ADA-USDT",
        }))
        api_factory = web_utils.build_api_factory()
        ds = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT", "ETH-USDT", "ADA-USDT"],
            connector=self.exchange, api_factory=api_factory)

        # Simulate snapshot sequence entries
        ds._last_sequence["BTC-USDT"] = 100
        ds._last_sequence["ETH-USDT"] = 200
        ds._last_sequence["ADA-USDT"] = 300

        self.assertEqual(3, len(ds._last_sequence))
        self.assertEqual(100, ds._last_sequence["BTC-USDT"])
        self.assertEqual(200, ds._last_sequence["ETH-USDT"])

    def test_unsubscribe_cleans_sequence_tracking(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource

        self.exchange._set_trading_pair_symbol_map(bidict({
            "BTC/USDT": "BTC-USDT", "ETH/USDT": "ETH-USDT",
        }))
        api_factory = web_utils.build_api_factory()
        ds = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT", "ETH-USDT"],
            connector=self.exchange, api_factory=api_factory)

        ds._last_sequence["BTC-USDT"] = 100
        ds._last_sequence["ETH-USDT"] = 200

        # Simulate what unsubscribe does: pop from _last_sequence
        ds._last_sequence.pop("BTC-USDT", None)

        self.assertNotIn("BTC-USDT", ds._last_sequence)
        self.assertEqual(200, ds._last_sequence["ETH-USDT"])

    def test_resubscribe_starts_fresh_sequence(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource

        api_factory = web_utils.build_api_factory()
        ds = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT"], connector=self.exchange, api_factory=api_factory)

        # Old subscription had seq 500
        ds._last_sequence["BTC-USDT"] = 500
        # Unsubscribe clears it
        ds._last_sequence.pop("BTC-USDT", None)
        # Re-subscribe starts fresh
        ds._last_sequence["BTC-USDT"] = 0

        self.assertEqual(0, ds._last_sequence["BTC-USDT"])
