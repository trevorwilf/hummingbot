"""Phase 7A: Auth & Correctness Hardening — Unit Tests."""
import asyncio
import hashlib
import hmac
import json
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import MagicMock

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class TestPhase7APostAuth(unittest.TestCase):
    """7A-1: POST auth body canonicalization."""

    def setUp(self):
        self._api_key = "testApiKey"
        self._secret = "testSecret"
        self._mock_time = MagicMock()
        self._mock_time.time.return_value = 1234567890.000
        self.auth = NonkycAuth(self._api_key, self._secret, self._mock_time)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coro, 1))

    def test_post_body_is_minified(self):
        """After rest_authenticate, request.data must have no spaces."""
        body = {"symbol": "BTC/USDT", "side": "buy", "quantity": "1.0"}
        req = RESTRequest(method=RESTMethod.POST,
                          url="https://api.nonkyc.io/api/v2/createorder",
                          data=json.dumps(body), is_auth_required=True)
        cfg = self._run(self.auth.rest_authenticate(req))
        self.assertNotIn(": ", cfg.data, "Body must be minified (no space after colon)")
        self.assertNotIn(", ", cfg.data, "Body must be minified (no space after comma)")
        parsed = json.loads(cfg.data)
        self.assertEqual("BTC/USDT", parsed["symbol"])

    def test_post_body_and_signature_use_same_string(self):
        """The body sent and the body signed must be identical strings."""
        body = {"symbol": "BTC/USDT", "side": "buy"}
        url = "https://api.nonkyc.io/api/v2/createorder"
        req = RESTRequest(method=RESTMethod.POST, url=url,
                          data=json.dumps(body), is_auth_required=True)
        cfg = self._run(self.auth.rest_authenticate(req))
        nonce = cfg.headers["X-API-NONCE"]
        # Reconstruct what the signature should cover: apiKey + url + body + nonce
        signed_data = f"{self._api_key}{url}{cfg.data}{nonce}"
        expected_sig = hmac.new(
            self._secret.encode("utf8"), signed_data.encode("utf8"), hashlib.sha256
        ).hexdigest()
        self.assertEqual(expected_sig, cfg.headers["X-API-SIGN"])

    def test_post_preserves_all_fields(self):
        """Minification must not lose any JSON fields."""
        body = {"symbol": "BTC/USDT", "side": "buy", "quantity": "1.0",
                "type": "limit", "price": "50000.00", "userProvidedId": "hbot-123"}
        req = RESTRequest(method=RESTMethod.POST,
                          url="https://api.nonkyc.io/api/v2/createorder",
                          data=json.dumps(body), is_auth_required=True)
        cfg = self._run(self.auth.rest_authenticate(req))
        parsed = json.loads(cfg.data)
        for key in body:
            self.assertIn(key, parsed, f"Field {key} must survive minification")
            self.assertEqual(body[key], parsed[key])


class TestPhase7AGetAuth(unittest.TestCase):
    """7A-2: GET auth param sorting."""

    def setUp(self):
        self._api_key = "testApiKey"
        self._secret = "testSecret"
        self._mock_time = MagicMock()
        self._mock_time.time.return_value = 1234567890.000
        self.auth = NonkycAuth(self._api_key, self._secret, self._mock_time)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coro, 1))

    def test_different_param_order_same_signature(self):
        """Identical params in different insertion order must produce the same signature."""
        url = "https://api.nonkyc.io/api/v2/account/orders"
        req_a = RESTRequest(method=RESTMethod.GET, url=url,
                            params={"status": "active", "symbol": "BTC/USDT"},
                            is_auth_required=True)
        req_b = RESTRequest(method=RESTMethod.GET, url=url,
                            params={"symbol": "BTC/USDT", "status": "active"},
                            is_auth_required=True)
        cfg_a = self._run(self.auth.rest_authenticate(req_a))
        cfg_b = self._run(self.auth.rest_authenticate(req_b))
        self.assertEqual(cfg_a.headers["X-API-SIGN"], cfg_b.headers["X-API-SIGN"],
                         "Signatures must match regardless of param insertion order")

    def test_no_params_still_authenticates(self):
        """GET with no params must still produce valid headers."""
        url = "https://api.nonkyc.io/api/v2/balances"
        req = RESTRequest(method=RESTMethod.GET, url=url, params=None, is_auth_required=True)
        cfg = self._run(self.auth.rest_authenticate(req))
        self.assertIn("X-API-SIGN", cfg.headers)
        self.assertIn("X-API-KEY", cfg.headers)
        self.assertIn("X-API-NONCE", cfg.headers)

    def test_single_param_sorted_unchanged(self):
        """Single param should work identically to before."""
        url = "https://api.nonkyc.io/api/v2/account/orders"
        req = RESTRequest(method=RESTMethod.GET, url=url,
                          params={"status": "active"}, is_auth_required=True)
        cfg = self._run(self.auth.rest_authenticate(req))
        self.assertIn("X-API-SIGN", cfg.headers)


class TestPhase7AOrderTypeSafety(unittest.TestCase):
    """7A-4: to_hb_order_type case safety."""

    def test_lowercase_limit(self):
        self.assertEqual(OrderType.LIMIT, NonkycExchange.to_hb_order_type("limit"))

    def test_lowercase_market(self):
        self.assertEqual(OrderType.MARKET, NonkycExchange.to_hb_order_type("market"))

    def test_uppercase_limit(self):
        self.assertEqual(OrderType.LIMIT, NonkycExchange.to_hb_order_type("LIMIT"))

    def test_uppercase_market(self):
        self.assertEqual(OrderType.MARKET, NonkycExchange.to_hb_order_type("MARKET"))

    def test_mixed_case(self):
        self.assertEqual(OrderType.LIMIT, NonkycExchange.to_hb_order_type("Limit"))
        self.assertEqual(OrderType.MARKET, NonkycExchange.to_hb_order_type("Market"))

    def test_invalid_type_returns_limit(self):
        # Phase 2 Fix 4: unknown types now return LIMIT instead of raising KeyError
        self.assertEqual(OrderType.LIMIT, NonkycExchange.to_hb_order_type("stop_loss"))


class TestPhase7AFeeExtraction(unittest.TestCase):
    """7A-6: _extract_fee_token_and_amount helper."""

    def test_alternate_fee_asset_present(self):
        trade = {"fee": "0.1", "alternateFeeAsset": "NKC", "alternateFee": "0.5"}
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("NKC", token)
        self.assertEqual(Decimal("0.5"), amount)

    def test_alternate_fee_asset_null(self):
        trade = {"fee": "0.1", "alternateFeeAsset": None, "alternateFee": None}
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("USDT", token)
        self.assertEqual(Decimal("0.1"), amount)

    def test_alternate_fee_asset_absent(self):
        trade = {"fee": "0.25"}
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "BTC")
        self.assertEqual("BTC", token)
        self.assertEqual(Decimal("0.25"), amount)

    def test_ws_trade_fee_field(self):
        """WS reports use 'tradeFee' instead of 'fee'."""
        trade = {"tradeFee": "0.03", "alternateFeeAsset": None}
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("USDT", token)
        self.assertEqual(Decimal("0.03"), amount)

    def test_zero_fee(self):
        trade = {"fee": "0"}
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("USDT", token)
        self.assertEqual(Decimal("0"), amount)

    def test_alternate_with_zero_regular(self):
        """alternateFeeAsset takes priority even if regular fee is nonzero."""
        trade = {"fee": "0.1", "alternateFeeAsset": "NKC", "alternateFee": "2.0"}
        token, amount = NonkycExchange._extract_fee_token_and_amount(trade, "USDT")
        self.assertEqual("NKC", token)
        self.assertEqual(Decimal("2.0"), amount)


class TestPhase7ACancelFallback(unittest.TestCase):
    """7A-7: _place_cancel fallback ID logic."""

    def _get_cancel_id(self, exchange_order_id, client_order_id):
        """Mimics the cancel_id selection logic from the fix."""
        cancel_id = exchange_order_id
        if not cancel_id or cancel_id == "UNKNOWN":
            cancel_id = client_order_id
        return cancel_id

    def test_normal_uses_exchange_id(self):
        self.assertEqual("abc123", self._get_cancel_id("abc123", "hbot-xxx"))

    def test_none_falls_back(self):
        self.assertEqual("hbot-fallback", self._get_cancel_id(None, "hbot-fallback"))

    def test_unknown_falls_back(self):
        self.assertEqual("hbot-503", self._get_cancel_id("UNKNOWN", "hbot-503"))

    def test_empty_string_falls_back(self):
        self.assertEqual("hbot-empty", self._get_cancel_id("", "hbot-empty"))


class TestPhase7ATradingRules(IsolatedAsyncioWrapperTestCase):
    """7A-8 and 7A-9: allowMarketOrders and max_order_size."""

    def setUp(self):
        super().setUp()
        self.exchange = NonkycExchange(
            nonkyc_api_key="test", nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"], trading_required=False)
        from bidict import bidict
        self.exchange._set_trading_pair_symbol_map(bidict({"BTC/USDT": "BTC-USDT"}))

    def _base_rule(self, **overrides):
        rule = {
            "symbol": "BTC/USDT", "primaryTicker": "BTC",
            "priceDecimals": 2, "quantityDecimals": 6,
            "minimumQuantity": "0.001", "minQuote": 10, "isMinQuoteActive": True,
            "isActive": True, "isPaused": False,
        }
        rule.update(overrides)
        return rule

    async def test_allow_market_orders_true(self):
        rules = await self.exchange._format_trading_rules([self._base_rule(allowMarketOrders=True)])
        self.assertTrue(rules[0].supports_market_orders)

    async def test_allow_market_orders_false(self):
        rules = await self.exchange._format_trading_rules([self._base_rule(allowMarketOrders=False)])
        self.assertFalse(rules[0].supports_market_orders)

    async def test_allow_market_orders_missing_defaults_true(self):
        rules = await self.exchange._format_trading_rules([self._base_rule()])
        self.assertTrue(rules[0].supports_market_orders)

    async def test_max_order_size_from_data(self):
        rules = await self.exchange._format_trading_rules([self._base_rule(maximumQuantity="500")])
        self.assertEqual(Decimal("500"), rules[0].max_order_size)

    async def test_max_order_size_missing_is_large(self):
        rules = await self.exchange._format_trading_rules([self._base_rule()])
        self.assertGreater(rules[0].max_order_size, Decimal("999999"))

    async def test_min_order_size_still_works(self):
        """Existing min_order_size must still be populated correctly."""
        rules = await self.exchange._format_trading_rules([self._base_rule(minimumQuantity="0.01")])
        self.assertEqual(Decimal("0.01"), rules[0].min_order_size)

    async def test_min_notional_still_works(self):
        """Existing min_notional_size must still be populated correctly."""
        rules = await self.exchange._format_trading_rules(
            [self._base_rule(minQuote=5, isMinQuoteActive=True)])
        self.assertEqual(Decimal("5"), rules[0].min_notional_size)
