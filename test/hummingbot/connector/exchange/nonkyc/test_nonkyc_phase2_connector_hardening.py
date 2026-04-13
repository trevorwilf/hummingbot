"""
Tests for NonKYC connector hardening Phase 2:
  - Post-reconnect order reconciliation
  - Balance settling waits for orders
  - Large balance mismatch recheck
  - Nonce cooldown
  - Cancel timeout
  - Server disconnect backoff
  - Normal operation unaffected
"""
import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class _BaseHardeningTest(unittest.TestCase):
    """Shared setup for hardening tests."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )


class TestPostReconnectOrderReconciliation(unittest.TestCase):
    """Fix 1: Active orders reconciliation after reconnect."""

    def test_reconcile_method_exists(self):
        exchange = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=["BTC-USDT"], trading_required=False,
        )
        self.assertTrue(hasattr(exchange, '_reconcile_active_orders_after_reconnect'))
        self.assertTrue(asyncio.iscoroutinefunction(exchange._reconcile_active_orders_after_reconnect))

    def test_orders_reconciled_flag_default_true(self):
        exchange = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=["BTC-USDT"], trading_required=False,
        )
        self.assertTrue(exchange._orders_reconciled_after_reconnect)


class TestBalanceSettlingWaitsForOrders(_BaseHardeningTest):
    """Fix 2: Balance settling waits for both balances and orders."""

    def test_exit_settling_blocked_when_orders_not_reconciled(self):
        """Balance settling should NOT exit if orders haven't been reconciled."""
        self.exchange._balance_settling = True
        self.exchange._balance_settle_start = time.time()
        self.exchange._orders_reconciled_after_reconnect = False
        self.exchange._exit_balance_settling()
        # Should still be settling
        self.assertTrue(self.exchange._balance_settling)

    def test_exit_settling_allowed_when_orders_reconciled(self):
        """Balance settling should exit when orders are reconciled."""
        self.exchange._balance_settling = True
        self.exchange._balance_settle_start = time.time()
        self.exchange._orders_reconciled_after_reconnect = True
        self.exchange._exit_balance_settling()
        # Should have exited
        self.assertFalse(self.exchange._balance_settling)


class TestNonceCooldown(_BaseHardeningTest):
    """Fix 4: Nonce error triggers REST cooldown."""

    def test_nonce_error_sets_cooldown(self):
        self.exchange._on_nonce_error_detected()
        self.assertGreater(self.exchange._nonce_error_cooldown_until, time.time())

    def test_cooldown_expires_after_2s(self):
        self.exchange._on_nonce_error_detected()
        # Should expire within ~2.5 seconds from now
        self.assertLess(self.exchange._nonce_error_cooldown_until, time.time() + 3.0)

    def test_time_sync_error_triggers_nonce_cooldown(self):
        """_is_request_exception_related_to_time_synchronizer should trigger nonce cooldown."""
        before = self.exchange._nonce_error_cooldown_until
        result = self.exchange._is_request_exception_related_to_time_synchronizer(
            Exception("HTTP 401: Bad Nonce")
        )
        self.assertTrue(result)
        self.assertGreater(self.exchange._nonce_error_cooldown_until, before)


class TestCancelTimeout(_BaseHardeningTest):
    """Fix 5: Cancel requests have a timeout."""

    def test_place_cancel_method_exists(self):
        self.assertTrue(hasattr(self.exchange, '_place_cancel'))
        self.assertTrue(asyncio.iscoroutinefunction(self.exchange._place_cancel))


class TestServerDisconnectBackoff(_BaseHardeningTest):
    """Fix 6: Server disconnect triggers order submission backoff."""

    def test_disconnect_backoff_field_exists(self):
        self.assertEqual(self.exchange._last_server_disconnect_time, 0.0)
        self.assertEqual(self.exchange._SERVER_DISCONNECT_BACKOFF, 10.0)

    def test_disconnect_recorded_in_on_order_failure(self):
        """ServerDisconnectedError in _on_order_failure should record timestamp."""
        from hummingbot.core.data_type.common import OrderType, TradeType
        before = self.exchange._last_server_disconnect_time
        self.exchange._on_order_failure(
            order_id="test_order",
            trading_pair="BTC-USDT",
            amount=Decimal("0.1"),
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("50000"),
            exception=Exception("ServerDisconnectedError: connection lost"),
        )
        self.assertGreater(self.exchange._last_server_disconnect_time, before)

    def test_non_disconnect_error_does_not_set_backoff(self):
        """Non-disconnect errors should NOT set the backoff timer."""
        from hummingbot.core.data_type.common import OrderType, TradeType
        self.exchange._last_server_disconnect_time = 0.0
        self.exchange._on_order_failure(
            order_id="test_order",
            trading_pair="BTC-USDT",
            amount=Decimal("0.1"),
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("50000"),
            exception=Exception("Insufficient funds"),
        )
        self.assertEqual(self.exchange._last_server_disconnect_time, 0.0)


class TestNormalOperationUnaffected(_BaseHardeningTest):
    """Verify that all new gates are no-ops during normal operation."""

    def test_no_nonce_cooldown_normally(self):
        self.assertEqual(self.exchange._nonce_error_cooldown_until, 0.0)
        self.assertFalse(time.time() < self.exchange._nonce_error_cooldown_until)

    def test_no_server_disconnect_backoff_normally(self):
        self.assertEqual(self.exchange._last_server_disconnect_time, 0.0)
        age = time.time() - self.exchange._last_server_disconnect_time
        self.assertGreater(age, self.exchange._SERVER_DISCONNECT_BACKOFF)

    def test_orders_reconciled_by_default(self):
        self.assertTrue(self.exchange._orders_reconciled_after_reconnect)

    def test_balance_recheck_not_in_progress(self):
        self.assertFalse(self.exchange._balance_recheck_in_progress)

    def test_exit_settling_works_normally(self):
        """Without balance settling, _exit_balance_settling is a no-op."""
        self.exchange._balance_settling = False
        self.exchange._exit_balance_settling()
        self.assertFalse(self.exchange._balance_settling)


if __name__ == "__main__":
    unittest.main()
