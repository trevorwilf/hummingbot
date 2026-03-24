"""Tests for the MEXC listen key Content-Type fix (RCA Fix 1).

Verified against live MEXC API 2026-03-24: userDataStream endpoints
reject Content-Type: application/x-www-form-urlencoded but accept
omitted Content-Type or application/json.
"""
import asyncio
import json
from unittest import TestCase
from unittest.mock import MagicMock

from hummingbot.connector.exchange.mexc.mexc_auth import MexcAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class TestMexcListenKeyFix(TestCase):

    def setUp(self):
        self._api_key = "testApiKey"
        self._secret = "testSecret"
        self._mock_time = MagicMock()
        self._mock_time.time.return_value = 1234567890.0
        self.auth = MexcAuth(
            api_key=self._api_key,
            secret_key=self._secret,
            time_provider=self._mock_time,
        )

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_auth_omits_content_type_for_empty_body_post(self):
        """POST /userDataStream with data=None must NOT have Content-Type."""
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/userDataStream",
            is_auth_required=True,
        )
        result = self._run(self.auth.rest_authenticate(request))

        self.assertIn("X-MEXC-APIKEY", result.headers)
        self.assertIn("timestamp", result.params)
        self.assertIn("signature", result.params)
        self.assertNotIn("Content-Type", result.headers)

    def test_auth_omits_content_type_for_empty_body_put(self):
        """PUT /userDataStream with listenKey in params must NOT have Content-Type."""
        request = RESTRequest(
            method=RESTMethod.PUT,
            url="https://api.mexc.com/api/v3/userDataStream",
            params={"listenKey": "test_listen_key"},
            is_auth_required=True,
        )
        result = self._run(self.auth.rest_authenticate(request))

        self.assertIn("X-MEXC-APIKEY", result.headers)
        self.assertIn("timestamp", result.params)
        self.assertIn("signature", result.params)
        self.assertNotIn("Content-Type", result.headers)

    def test_auth_omits_content_type_for_empty_body_delete(self):
        """DELETE /userDataStream must NOT have Content-Type."""
        request = RESTRequest(
            method=RESTMethod.DELETE,
            url="https://api.mexc.com/api/v3/userDataStream",
            params={"listenKey": "test_listen_key"},
            is_auth_required=True,
        )
        result = self._run(self.auth.rest_authenticate(request))

        self.assertIn("X-MEXC-APIKEY", result.headers)
        self.assertIn("timestamp", result.params)
        self.assertIn("signature", result.params)
        self.assertNotIn("Content-Type", result.headers)

    def test_auth_omits_content_type_for_order_post(self):
        """POST /order with body params moved to QS must NOT have Content-Type."""
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.mexc.com/api/v3/order",
            data=json.dumps({"symbol": "BTCUSDT", "side": "BUY"}),
            is_auth_required=True,
        )
        result = self._run(self.auth.rest_authenticate(request))

        # Body params moved to QS, data is now None
        self.assertIsNone(result.data)
        self.assertEqual("BTCUSDT", result.params["symbol"])
        self.assertIn("timestamp", result.params)
        self.assertIn("signature", result.params)
        self.assertNotIn("Content-Type", result.headers)

    def test_auth_preserves_signing_for_all_endpoints(self):
        """All authenticated endpoints must have timestamp and signature in params."""
        for url in [
            "https://api.mexc.com/api/v3/account",
            "https://api.mexc.com/api/v3/order",
            "https://api.mexc.com/api/v3/userDataStream",
        ]:
            request = RESTRequest(
                method=RESTMethod.GET,
                url=url,
                is_auth_required=True,
            )
            result = self._run(self.auth.rest_authenticate(request))
            self.assertIn("timestamp", result.params, f"Missing timestamp for {url}")
            self.assertIn("signature", result.params, f"Missing signature for {url}")

    def test_auth_no_content_type_for_get(self):
        """GET request must NOT have Content-Type (regression)."""
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://api.mexc.com/api/v3/account",
            headers={"Content-Type": "application/json"},
            is_auth_required=True,
        )
        result = self._run(self.auth.rest_authenticate(request))
        self.assertNotIn("Content-Type", result.headers)
