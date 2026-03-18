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

    def test_rest_authenticate(self):
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
        self.assertEqual({"X-MEXC-APIKEY": self._api_key, "Content-Type": "application/json"}, configured_request.headers)

    def test_rest_authenticate_post(self):
        """POST auth re-serializes data as JSON string, not OrderedDict."""
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

        # Data must be a string (JSON-serialized), not a dict/OrderedDict
        self.assertIsInstance(configured_request.data, str)

        # Should be valid JSON
        parsed = json.loads(configured_request.data)
        self.assertIn("timestamp", parsed)
        self.assertIn("signature", parsed)

        # Content-Type must be application/json
        self.assertEqual("application/json", configured_request.headers["Content-Type"])
