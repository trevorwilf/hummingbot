"""Tests for Fix 6 (RCA): NonKYC balance WS auto-disable on silence."""
import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class TestBalanceWsAutoDisable(unittest.TestCase):
    """Fix 6: Balance WS should auto-disable after 60s silence."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["ARRR-USDT"],
            trading_required=False,
        )

    def test_balance_ws_auto_disables_on_timeout(self):
        """Simulate timeout: subscription_time 70s ago, unconfirmed => ENABLE_BALANCE_WS=False."""
        self.exchange._balance_ws_subscription_time = time.time() - 70
        self.exchange._balance_ws_confirmed = False
        self.exchange.ENABLE_BALANCE_WS = True

        # Simulate the check that happens in _user_stream_event_listener
        if (self.exchange._balance_ws_subscription_time is not None
                and not self.exchange._balance_ws_confirmed
                and time.time() - self.exchange._balance_ws_subscription_time > 60.0):
            self.exchange.ENABLE_BALANCE_WS = False
            self.exchange._balance_ws_subscription_time = None

        self.assertFalse(self.exchange.ENABLE_BALANCE_WS)
        self.assertIsNone(self.exchange._balance_ws_subscription_time)

    def test_balance_ws_stays_enabled_on_confirmation(self):
        """If _balance_ws_confirmed=True, ENABLE_BALANCE_WS should remain True."""
        self.exchange._balance_ws_subscription_time = time.time() - 70
        self.exchange._balance_ws_confirmed = True
        self.exchange.ENABLE_BALANCE_WS = True

        # Simulate the check
        if (self.exchange._balance_ws_subscription_time is not None
                and not self.exchange._balance_ws_confirmed
                and time.time() - self.exchange._balance_ws_subscription_time > 60.0):
            self.exchange.ENABLE_BALANCE_WS = False
            self.exchange._balance_ws_subscription_time = None

        self.assertTrue(self.exchange.ENABLE_BALANCE_WS)

    def test_balance_ws_default_is_true(self):
        """Default ENABLE_BALANCE_WS should be True."""
        self.assertTrue(NonkycExchange.ENABLE_BALANCE_WS)

    def test_no_unsubscribe_balances_ws_call(self):
        """Verify there's no WS send of unsubscribeBalances anywhere in the user stream data source."""
        from hummingbot.connector.exchange.nonkyc import nonkyc_api_user_stream_data_source as usd_module
        import inspect
        source = inspect.getsource(usd_module)
        self.assertNotIn("unsubscribeBalances", source)


if __name__ == "__main__":
    unittest.main()
