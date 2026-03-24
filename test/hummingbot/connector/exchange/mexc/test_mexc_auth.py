import asyncio
import hashlib
import hmac
import json
from copy import copy
from unittest import TestCase
from unittest.mock import MagicMock

from typing_extensions import Awaitable

from hummingbot.connector.exchange.mexc.mexc_auth import MexcAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class MexcAuthTests(TestCase):

    def setUp(self) -> None:
        self._api_key = "testApiKey"
        self._secret = "testSecret"

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def test_rest_authenticate_get(self):
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        params = {
            "symbol": "LTCBTC",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": 1,
            "price": "0.1",
        }
        full_params = copy(params)

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(method=RESTMethod.GET, params=params, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        full_params.update({"timestamp": 1234567890000})
        encoded_params = "&".join([f"{key}={value}" for key, value in full_params.items()])
        expected_signature = hmac.new(
            self._secret.encode("utf-8"),
            encoded_params.encode("utf-8"),
            hashlib.sha256).hexdigest()
        self.assertEqual(now * 1e3, configured_request.params["timestamp"])
        self.assertEqual(expected_signature, configured_request.params["signature"])
        # All authenticated requests get Content-Type: application/json
        self.assertEqual("application/json", configured_request.headers["Content-Type"])
        self.assertEqual(self._api_key, configured_request.headers["X-MEXC-APIKEY"])

    def test_rest_authenticate_post_moves_body_to_params(self):
        """POST auth should move body params to query string for MEXC/Binance-compatible signing."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT",
                  "timeInForce": "GTC", "quantity": 1, "price": "0.1"}

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/order",
            data=json.dumps(params),
            is_auth_required=True
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        # Body must be cleared — all params moved to query string
        self.assertIsNone(configured_request.data)

        # Params should contain the original body fields plus auth
        self.assertEqual("LTCBTC", configured_request.params["symbol"])
        self.assertEqual("BUY", configured_request.params["side"])
        self.assertIn("timestamp", configured_request.params)
        self.assertIn("signature", configured_request.params)

    def test_rest_authenticate_post_no_body(self):
        """POST with no body should produce signed query params."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/userDataStream",
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        # Body must be None — all params in query string
        self.assertIsNone(configured_request.data)
        self.assertIn("timestamp", configured_request.params)
        self.assertIn("signature", configured_request.params)

    def test_delete_request_has_signature_in_params(self):
        """DELETE requests should have signature in params."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.DELETE,
            url="https://api.mexc.com/api/v3/order",
            params={"symbol": "BTCUSDT", "origClientOrderId": "abc123"},
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertIn("signature", configured_request.params)
        self.assertIn("timestamp", configured_request.params)

    def test_post_empty_body_has_json_content_type(self):
        """POST with empty body should have Content-Type: application/json (aiohttp needs explicit CT)."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/userDataStream",
            headers={"Content-Type": "application/json"},
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertEqual("application/json", configured_request.headers["Content-Type"])
        self.assertIsNone(configured_request.data)

    def test_post_user_data_stream_has_json_content_type(self):
        """Authenticated POST to /userDataStream must have Content-Type: application/json."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/userDataStream",
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertEqual("application/json", configured_request.headers["Content-Type"])
        self.assertIn("timestamp", configured_request.params)
        self.assertIn("signature", configured_request.params)

    def test_put_user_data_stream_has_json_content_type(self):
        """Authenticated PUT to /userDataStream must have Content-Type: application/json."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.PUT,
            url="https://api.mexc.com/api/v3/userDataStream",
            params={"listenKey": "some_listen_key_value"},
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertEqual("application/json", configured_request.headers["Content-Type"])
        self.assertIn("timestamp", configured_request.params)

    def test_get_request_has_json_content_type(self):
        """GET requests should also have Content-Type: application/json (aiohttp needs explicit CT)."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://api.mexc.com/api/v3/account",
            headers={"Content-Type": "application/json"},
            is_auth_required=True,
        )
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertEqual("application/json", configured_request.headers["Content-Type"])

    def test_auth_content_type_always_json(self):
        """All authenticated requests should have Content-Type: application/json."""
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now
        auth = MexcAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)

        cases = [
            (RESTMethod.GET, None),
            (RESTMethod.POST, None),
            (RESTMethod.POST, json.dumps({"symbol": "BTCUSDT"})),
            (RESTMethod.PUT, None),
            (RESTMethod.DELETE, None),
        ]

        for method, data in cases:
            request = RESTRequest(
                method=method,
                url="https://api.mexc.com/api/v3/test",
                data=data,
                is_auth_required=True,
            )
            result = self.async_run_with_timeout(auth.rest_authenticate(request))
            self.assertEqual("application/json", result.headers.get("Content-Type"),
                             f"Method {method.name} (data={data!r}): should be application/json")
