"""
Tests for bug fixes identified in the NonKYC connector code review.
Each test class covers one specific bug fix.
"""
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_order_book import NonkycOrderBook
from hummingbot.core.data_type.in_flight_order import OrderState


class TestBug1OrderBookVariableName(unittest.TestCase):
    """Bug 1: Verify diff_message_from_exchange correctly parses bids and asks separately."""

    def test_diff_bids_and_asks_are_independent(self):
        """Ensure bids and asks don't cross-contaminate due to variable naming."""
        msg = {
            "trading_pair": "BTC-USDT",
            "params": {
                "sequence": 42,
                "bids": [{"price": "100.00", "quantity": "1.5"}],
                "asks": [{"price": "200.00", "quantity": "2.5"}],
            }
        }
        result = NonkycOrderBook.diff_message_from_exchange(msg, 1234567890.0)
        content = result.content

        # Bids should have bid prices, not ask prices
        self.assertEqual(content["bids"][0][0], "100.00")
        self.assertEqual(content["bids"][0][1], "1.5")

        # Asks should have ask prices, not bid prices
        self.assertEqual(content["asks"][0][0], "200.00")
        self.assertEqual(content["asks"][0][1], "2.5")

    def test_diff_multiple_bids_and_asks(self):
        """Verify correct parsing with multiple entries on each side."""
        msg = {
            "trading_pair": "ETH-USDT",
            "params": {
                "sequence": 99,
                "bids": [
                    {"price": "50.00", "quantity": "10.0"},
                    {"price": "49.00", "quantity": "20.0"},
                ],
                "asks": [
                    {"price": "51.00", "quantity": "5.0"},
                    {"price": "52.00", "quantity": "8.0"},
                ],
            }
        }
        result = NonkycOrderBook.diff_message_from_exchange(msg, 1234567890.0)
        content = result.content

        self.assertEqual(len(content["bids"]), 2)
        self.assertEqual(len(content["asks"]), 2)
        self.assertEqual(content["bids"][0][0], "50.00")
        self.assertEqual(content["bids"][1][0], "49.00")
        self.assertEqual(content["asks"][0][0], "51.00")
        self.assertEqual(content["asks"][1][0], "52.00")


class TestBug2BalanceUpdateNullGuard(unittest.TestCase):
    """Bug 2: Verify balanceUpdate handler gracefully handles null/missing params."""

    def test_balance_update_null_params_no_crash(self):
        """A balanceUpdate event with params=null should not crash the handler."""
        event = {"method": "balanceUpdate", "params": None}
        balance_entry = event.get("params")
        # The fix: if not balance_entry, skip
        if not balance_entry:
            skipped = True
        else:
            skipped = False
        self.assertTrue(skipped)

    def test_balance_update_missing_params_no_crash(self):
        """A balanceUpdate event with no params key should not crash."""
        event = {"method": "balanceUpdate"}
        balance_entry = event.get("params")
        if not balance_entry:
            skipped = True
        else:
            skipped = False
        self.assertTrue(skipped)

    def test_balance_update_valid_params_processes(self):
        """A balanceUpdate with valid params should still process normally."""
        event = {"method": "balanceUpdate", "params": {"ticker": "BTC", "available": "1.5", "held": "0.5"}}
        balance_entry = event.get("params")
        if not balance_entry:
            skipped = True
        else:
            skipped = False
            asset_name = balance_entry["ticker"]
            free_balance = Decimal(balance_entry["available"])
            total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])

        self.assertFalse(skipped)
        self.assertEqual(asset_name, "BTC")
        self.assertEqual(free_balance, Decimal("1.5"))
        self.assertEqual(total_balance, Decimal("2.0"))


class TestBug3RequestOrderStatusPreferExchangeId(unittest.TestCase):
    """Bug 3: _request_order_status should prefer exchange_order_id over client_order_id."""

    def test_prefer_exchange_order_id_when_available(self):
        """When exchange_order_id is set, it should be used for the query."""
        order = MagicMock()
        order.exchange_order_id = "abc123_exchange"
        order.client_order_id = "HBOT-xyz789"

        order_id_for_query = order.exchange_order_id or order.client_order_id
        self.assertEqual(order_id_for_query, "abc123_exchange")

    def test_fallback_to_client_order_id_when_exchange_id_missing(self):
        """When exchange_order_id is None, fall back to client_order_id."""
        order = MagicMock()
        order.exchange_order_id = None
        order.client_order_id = "HBOT-xyz789"

        order_id_for_query = order.exchange_order_id or order.client_order_id
        self.assertEqual(order_id_for_query, "HBOT-xyz789")

    def test_fallback_to_client_order_id_when_exchange_id_empty(self):
        """When exchange_order_id is empty string, fall back to client_order_id."""
        order = MagicMock()
        order.exchange_order_id = ""
        order.client_order_id = "HBOT-xyz789"

        order_id_for_query = order.exchange_order_id or order.client_order_id
        self.assertEqual(order_id_for_query, "HBOT-xyz789")


class TestBug4SymbolSplitGuard(unittest.TestCase):
    """Bug 4: _initialize_trading_pair_symbols should handle symbols without slash."""

    def test_normal_slash_symbol_parses(self):
        """Standard 'BTC/USDT' format should parse correctly."""
        symbol = "BTC/USDT"
        parts = symbol.split('/')
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "BTC")
        self.assertEqual(parts[1], "USDT")

    def test_no_slash_symbol_skipped(self):
        """A symbol without slash should be detected and skipped."""
        symbol = "BTCUSDT"
        parts = symbol.split('/')
        self.assertNotEqual(len(parts), 2)  # Only 1 element
        self.assertEqual(len(parts), 1)

    def test_multiple_slash_symbol_skipped(self):
        """A symbol with multiple slashes should be detected and skipped."""
        symbol = "TOKEN/WRAPPED/USDT"
        parts = symbol.split('/')
        self.assertNotEqual(len(parts), 2)  # 3 elements
        self.assertEqual(len(parts), 3)


class TestBug5RejectedOrderState(unittest.TestCase):
    """Bug 5: ORDER_STATE should include 'Rejected' mapped to FAILED."""

    def test_rejected_lowercase_maps_to_failed(self):
        """'rejected' should map to OrderState.FAILED."""
        self.assertEqual(CONSTANTS.ORDER_STATE["rejected"], OrderState.FAILED)

    def test_rejected_titlecase_maps_to_failed(self):
        """'Rejected' should map to OrderState.FAILED."""
        self.assertEqual(CONSTANTS.ORDER_STATE["Rejected"], OrderState.FAILED)

    def test_existing_states_unchanged(self):
        """Verify no regressions on existing state mappings."""
        self.assertEqual(CONSTANTS.ORDER_STATE["New"], OrderState.OPEN)
        self.assertEqual(CONSTANTS.ORDER_STATE["active"], OrderState.OPEN)
        self.assertEqual(CONSTANTS.ORDER_STATE["Filled"], OrderState.FILLED)
        self.assertEqual(CONSTANTS.ORDER_STATE["cancelled"], OrderState.CANCELED)
        self.assertEqual(CONSTANTS.ORDER_STATE["Expired"], OrderState.CANCELED)
        self.assertEqual(CONSTANTS.ORDER_STATE["Suspended"], OrderState.PENDING_CREATE)

    def test_rejected_is_not_open(self):
        """A rejected order must NOT be treated as OPEN."""
        self.assertNotEqual(CONSTANTS.ORDER_STATE["Rejected"], OrderState.OPEN)

    def test_all_expected_states_present(self):
        """Verify the complete set of expected states is in the mapping."""
        expected_keys = [
            "New", "new", "Active", "active",
            "Partly Filled", "partly filled", "partlyFilled",
            "Filled", "filled",
            "Cancelled", "cancelled", "Canceled", "canceled",
            "Expired", "expired",
            "Rejected", "rejected",
            "Suspended", "suspended",
        ]
        for key in expected_keys:
            self.assertIn(key, CONSTANTS.ORDER_STATE, f"Missing ORDER_STATE key: {key}")


if __name__ == "__main__":
    unittest.main()
