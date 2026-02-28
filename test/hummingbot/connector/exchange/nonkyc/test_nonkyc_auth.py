import asyncio
import hashlib
import hmac
import json
from unittest import TestCase
from unittest.mock import MagicMock
from urllib.parse import urlencode

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
        sorted_params = sorted(params.items())
        full_url = f"{url}?{urlencode(sorted_params)}"
        expected_message = f"{self._api_key}{full_url}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(str(expected_nonce), configured_request.headers["X-API-NONCE"])
        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        self.assertEqual(self._api_key, configured_request.headers["X-API-KEY"])

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
        json_str = json.dumps(json.loads(json.dumps(body))).replace(" ", "")
        expected_message = f"{self._api_key}{url}{json_str}{expected_nonce}"
        expected_signature = self._generate_signature(expected_message)

        self.assertEqual(str(expected_nonce), configured_request.headers["X-API-NONCE"])
        self.assertEqual(expected_signature, configured_request.headers["X-API-SIGN"])
        self.assertEqual(self._api_key, configured_request.headers["X-API-KEY"])

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
