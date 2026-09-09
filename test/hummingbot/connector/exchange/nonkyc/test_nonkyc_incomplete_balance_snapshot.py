import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class TestNonkycIncompleteBalanceSnapshot(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.exchange = NonkycExchange(
            nonkyc_api_key="key",
            nonkyc_api_secret="secret",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self.exchange._account_available_balances.update({
            "BTC": Decimal("1"),
            "USDT": Decimal("48"),
        })
        self.exchange._account_balances.update({
            "BTC": Decimal("1"),
            "USDT": Decimal("1176"),
        })

    def tearDown(self):
        self.loop.close()

    def run_async(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def test_omitted_nonzero_asset_is_preserved_and_quarantined(self):
        incomplete = [{"asset": "BTC", "available": "1", "held": "0"}]
        api = AsyncMock(side_effect=[incomplete, incomplete])

        with patch.object(self.exchange, "_api_get", new=api), \
                patch("hummingbot.connector.exchange.nonkyc.nonkyc_exchange.asyncio.sleep", new=AsyncMock()):
            self.run_async(self.exchange._update_balances())

        self.assertEqual(2, api.await_count)
        self.assertEqual(Decimal("48"), self.exchange._account_available_balances["USDT"])
        self.assertEqual(Decimal("1176"), self.exchange._account_balances["USDT"])
        self.assertTrue(self.exchange.is_balance_settling)
        self.assertTrue(self.exchange._balance_snapshot_incomplete)
        self.assertEqual({"USDT"}, self.exchange._missing_nonzero_balance_assets)

    def test_confirmation_poll_recovers_and_applies_authoritative_balance(self):
        incomplete = [{"asset": "BTC", "available": "1", "held": "0"}]
        complete = [
            {"asset": "BTC", "available": "1", "held": "0"},
            {"asset": "USDT", "available": "1176", "held": "0"},
        ]
        api = AsyncMock(side_effect=[incomplete, complete])

        with patch.object(self.exchange, "_api_get", new=api), \
                patch("hummingbot.connector.exchange.nonkyc.nonkyc_exchange.asyncio.sleep", new=AsyncMock()):
            self.run_async(self.exchange._update_balances())

        self.assertEqual(Decimal("1176"), self.exchange._account_available_balances["USDT"])
        self.assertEqual(Decimal("1176"), self.exchange._account_balances["USDT"])
        self.assertFalse(self.exchange.is_balance_settling)
        self.assertFalse(self.exchange._balance_snapshot_incomplete)
        self.assertEqual(set(), self.exchange._missing_nonzero_balance_assets)

    def test_omitted_zero_asset_is_pruned_without_quarantine(self):
        self.exchange._account_available_balances["ZERO"] = Decimal("0")
        self.exchange._account_balances["ZERO"] = Decimal("0")
        response = [
            {"asset": "BTC", "available": "1", "held": "0"},
            {"asset": "USDT", "available": "48", "held": "1128"},
        ]

        with patch.object(self.exchange, "_api_get", new=AsyncMock(return_value=response)):
            self.run_async(self.exchange._update_balances())

        self.assertNotIn("ZERO", self.exchange._account_balances)
        self.assertNotIn("ZERO", self.exchange._account_available_balances)
        self.assertFalse(self.exchange.is_balance_settling)

    def test_incomplete_snapshot_never_fails_open_after_reconnect_timeout(self):
        self.exchange._balance_settling = True
        self.exchange._balance_settle_start = time.time() - 60
        self.exchange._balance_snapshot_incomplete = True
        self.exchange._missing_nonzero_balance_assets = {"USDT"}
        order = MagicMock()

        with self.assertRaisesRegex(Exception, "incomplete REST balance snapshot"):
            self.run_async(self.exchange._place_order_and_process_update(order))

        self.assertTrue(self.exchange.is_balance_settling)

    def test_other_balance_event_cannot_exit_incomplete_rest_quarantine(self):
        self.exchange._balance_settling = True
        self.exchange._balance_settle_start = time.time() - 1
        self.exchange._balance_snapshot_incomplete = True
        self.exchange._missing_nonzero_balance_assets = {"USDT"}

        self.exchange._exit_balance_settling()

        self.assertTrue(self.exchange.is_balance_settling)


if __name__ == "__main__":
    unittest.main()
