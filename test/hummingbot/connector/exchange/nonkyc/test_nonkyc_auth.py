import asyncio
import hashlib
import hmac
import json
from unittest import TestCase
from unittest.mock import MagicMock

from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class NonkycAuthTests(TestCase):

    def setUp(self) -> None:
        self._api_key = "testApiKey"
        self._secret = "testSecret"

    def async_run_with_timeout(self, coroutine, timeout: float = 1):
        ret = asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def _generate_signature(self, data: str) -> str:
        return hmac.new(
            self._secret.encode("utf8"),
            data.encode("utf8"),
            hashlib.sha256
        ).hexdigest()

    def test_rest_authenticate_get(self):
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        params = {"symbol": "BTC/USDT", "side": "buy"}
        url = "https://api.nonkyc.io/api/v2/createorder"
        request = RESTRequest(method=RESTMethod.GET, url=url, params=params, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        expected_nonce = int(now * 1e3)  # 1234567890000
        # Params are baked in raw (no urlencode), preserving insertion order
        full_url = f"{url}?symbol=BTC/USDT&side=buy"
        expected_message = f"{self._api_key}{full_url}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(str(expected_nonce), configured_request.headers["X-API-NONCE"])
        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        self.assertEqual(self._api_key, configured_request.headers["X-API-KEY"])
        # Params should be baked into URL and cleared
        self.assertEqual(full_url, configured_request.url)
        self.assertIsNone(configured_request.params)

    def test_rest_authenticate_get_with_trade_params(self):
        """Verify GET signing includes query params for /account/trades."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        params = {"symbol": "ARRR/USDT"}
        url = "https://api.nonkyc.io/api/v2/account/trades"
        request = RESTRequest(method=RESTMethod.GET, url=url, params=params, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        expected_nonce = int(now * 1e3)
        # The '/' in ARRR/USDT must NOT be encoded as %2F
        full_url = f"{url}?symbol=ARRR/USDT"
        expected_message = f"{self._api_key}{full_url}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(str(expected_nonce), configured_request.headers["X-API-NONCE"])
        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        self.assertEqual(self._api_key, configured_request.headers["X-API-KEY"])
        # Verify '/' is NOT encoded
        self.assertIn("ARRR/USDT", configured_request.url)
        self.assertNotIn("%2F", configured_request.url)
        self.assertIsNone(configured_request.params)

    def test_rest_authenticate_get_no_params(self):
        """Verify GET signing works without query params."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        url = "https://api.nonkyc.io/api/v2/balances"
        request = RESTRequest(method=RESTMethod.GET, url=url, params=None, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        expected_nonce = int(now * 1e3)
        expected_message = f"{self._api_key}{url}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(str(expected_nonce), configured_request.headers["X-API-NONCE"])
        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        self.assertEqual(self._api_key, configured_request.headers["X-API-KEY"])
        # URL unchanged, params still None
        self.assertEqual(url, configured_request.url)

    def test_rest_authenticate_get_preserves_insertion_order(self):
        """Verify params are NOT sorted — insertion order is preserved to match aiohttp."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        # 'symbol' comes before 'since' in insertion order but after in alphabetical
        params = {"symbol": "ARRR/USDT", "since": "1000"}
        url = "https://api.nonkyc.io/api/v2/account/trades"
        request = RESTRequest(method=RESTMethod.GET, url=url, params=params, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        # Must be insertion order, NOT alphabetical
        expected_url = f"{url}?symbol=ARRR/USDT&since=1000"
        self.assertEqual(expected_url, configured_request.url)

    def test_rest_authenticate_post(self):
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        body = {"symbol": "BTC/USDT", "side": "buy", "quantity": "1.0"}
        url = "https://api.nonkyc.io/api/v2/createorder"
        request = RESTRequest(
            method=RESTMethod.POST,
            url=url,
            data=json.dumps(body),
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        expected_nonce = int(now * 1e3)
        json_str = json.dumps(body, separators=(',', ':'))
        expected_message = f"{self._api_key}{url}{json_str}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(str(expected_nonce), configured_request.headers["X-API-NONCE"])
        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        self.assertEqual(self._api_key, configured_request.headers["X-API-KEY"])

    def test_rest_authenticate_post_with_spaces_in_values(self):
        """Verify POST signing preserves spaces within JSON values (uses separators, not replace)."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        body = {"note": "hello world", "symbol": "BTC/USDT"}
        url = "https://api.nonkyc.io/api/v2/createorder"
        request = RESTRequest(
            method=RESTMethod.POST,
            url=url,
            data=json.dumps(body),
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        expected_nonce = int(now * 1e3)
        json_str = json.dumps(body, separators=(',', ':'))
        # Spaces in "hello world" must be preserved
        self.assertIn("hello world", json_str)
        expected_message = f"{self._api_key}{url}{json_str}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        # The signed body must also preserve spaces in values
        self.assertIn("hello world", configured_request.data)

    def test_ws_authenticate_message(self):
        mock_time_provider = MagicMock()
        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        payload = auth.generate_ws_authentication_message()

        self.assertEqual("login", payload["method"])
        self.assertEqual(self._api_key, payload["params"]["pKey"])

        nonce = payload["params"]["nonce"]
        self.assertIsInstance(nonce, str)
        self.assertEqual(14, len(nonce))
        self.assertTrue(nonce.isalnum())

        expected_signature = self._generate_signature(nonce)
        self.assertEqual(expected_signature, payload["params"]["signature"])

    def test_ws_nonce_is_string_not_list_repr(self):
        """Regression test for Fix 1: WS nonce must be a plain string, not a list repr."""
        mock_time_provider = MagicMock()
        auth = NonkycAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        for _ in range(20):
            payload = auth.generate_ws_authentication_message()
            nonce = payload["params"]["nonce"]
            self.assertNotIn("[", nonce)
            self.assertNotIn("]", nonce)
            self.assertNotIn("'", nonce)
            self.assertNotIn(",", nonce)
            self.assertEqual(14, len(nonce))
