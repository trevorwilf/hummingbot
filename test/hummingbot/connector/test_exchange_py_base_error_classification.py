"""Tests for error classification in ExchangePyBase._on_order_failure (RCA Fix 5)."""
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange
from hummingbot.core.data_type.common import OrderType, TradeType


class TestOrderFailureErrorClassification(unittest.TestCase):
    """Verify _on_order_failure classifies errors for helpful operator messages."""

    def setUp(self):
        self.exchange = MexcExchange(
            mexc_api_key="test",
            mexc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self._captured_warning = None

    def _call_on_order_failure(self, error_message: str) -> str:
        """Call _on_order_failure with the given error and return the app_warning_msg."""
        captured = {}

        def mock_network(msg, exc_info=False, app_warning_msg=""):
            captured["app_warning_msg"] = app_warning_msg

        with patch.object(self.exchange.logger(), "network", side_effect=mock_network):
            with patch.object(self.exchange, "_update_order_after_failure"):
                self.exchange._on_order_failure(
                    order_id="test-001",
                    trading_pair="BTC-USDT",
                    amount=Decimal("0.01"),
                    trade_type=TradeType.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal("50000"),
                    exception=Exception(error_message),
                )

        return captured.get("app_warning_msg", "")

    def test_insufficient_funds_classified(self):
        msg = self._call_on_order_failure("Insufficient funds for order creation (error code 20001)")
        self.assertIn("Insufficient funds", msg)

    def test_rate_limit_classified(self):
        msg = self._call_on_order_failure("429 Too Many Requests")
        self.assertIn("Rate limited", msg)

    def test_auth_failure_classified(self):
        msg = self._call_on_order_failure("401 Unauthorized")
        self.assertIn("Authentication failure", msg)

    def test_auth_failure_invalid_key(self):
        msg = self._call_on_order_failure("Invalid API key provided")
        self.assertIn("Authentication failure", msg)

    def test_server_error_classified(self):
        msg = self._call_on_order_failure("502 Bad Gateway")
        self.assertIn("server error", msg.lower())

    def test_timeout_classified(self):
        msg = self._call_on_order_failure("Connection timed out after 30s")
        self.assertIn("timeout", msg.lower())

    def test_unknown_error_falls_back(self):
        msg = self._call_on_order_failure("Something weird happened")
        self.assertIn("Check API key and network connection", msg)

    def test_content_type_mismatch_classified(self):
        msg = self._call_on_order_failure('{"code":700013,"msg":"Invalid content Type."}')
        self.assertIn("content-type", msg.lower())


if __name__ == "__main__":
    unittest.main()
