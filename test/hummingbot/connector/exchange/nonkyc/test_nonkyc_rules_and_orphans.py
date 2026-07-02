"""Tests for two NONKYC_LADDER_v1 log-driven fixes (2026-06-29..07-01 instance):

1. NEW-LISTING RULE PARSE. `_update_trading_rules` formatted rules against the PREVIOUS
   symbol map and refreshed the map afterwards. A pair listed mid-session (ANM/USDT in the
   logs, 344 -> 345 pairs) raised KeyError inside `_format_trading_rules`, dumping the full
   rule payload at ERROR, and its trading rule was delayed by one poll cycle. The fix
   refreshes the map FIRST and downgrades a residual unmapped-symbol skip to DEBUG.

2. ORPHAN WARNING DEDUPE. Post-reconnect reconciliation warned about the SAME orphan
   exchange order (6a3ed97c85e97619a4f0a001 in the logs) at every reconnect — 22 times over
   three days. The fix warns once per orphan id and logs repeats at DEBUG; an orphan that
   resolves and reappears warns again.
"""
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from bidict import bidict

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


def _make_exchange(trading_pairs=None):
    exchange = NonkycExchange(
        nonkyc_api_key="test_key",
        nonkyc_api_secret="test_secret",
        trading_pairs=trading_pairs or ["XMR-USDT"],
        trading_required=True,
    )
    return exchange


def _rule_payload(symbol="NEW/USDT", base="NEW", quote="USDT", **overrides):
    payload = {
        "symbol": symbol,
        "primaryTicker": base,
        "secondaryTicker": quote,
        "isActive": True,
        "isPaused": False,
        "priceDecimals": 6,
        "quantityDecimals": 2,
        "minimumQuantity": 0.01,
        "isMinQuoteActive": True,
        "minQuote": 1,
        "allowMarketOrders": True,
    }
    payload.update(overrides)
    return payload


class TestNewListingRuleParse(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.exchange = _make_exchange()
        # The PREVIOUS poll's symbol map: does NOT contain the new listing yet.
        self.exchange._set_trading_pair_symbol_map(bidict({"XMR/USDT": "XMR-USDT"}))

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def test_new_listing_gets_trading_rule_same_cycle(self):
        """REGRESSION: before the fix, a newly listed pair raised KeyError against the stale
        symbol map (logged at ERROR with the full payload) and its rule was missing this cycle."""
        exchange_info = [
            _rule_payload(symbol="XMR/USDT", base="XMR", quote="USDT"),
            _rule_payload(symbol="ANM/USDT", base="ANM", quote="USDT"),  # the new listing
        ]
        with patch.object(self.exchange, "_make_trading_rules_request",
                          new=AsyncMock(return_value=exchange_info)):
            with self.assertLogs(self.exchange.logger(), level="DEBUG") as cm:
                self._run(self.exchange._update_trading_rules())

        self.assertIn("ANM-USDT", self.exchange._trading_rules)
        self.assertEqual(Decimal("0.01"), self.exchange._trading_rules["ANM-USDT"].min_order_size)
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual([], error_records)

    def test_unmapped_symbol_skipped_at_debug_not_error(self):
        """A rule whose symbol is absent from the map (e.g. rejected during map init) must be
        skipped with a DEBUG line, not an ERROR dump of the full payload."""
        unmapped = _rule_payload(symbol="GHOST/USDT", base="GHOST", quote="USDT")
        with self.assertLogs(self.exchange.logger(), level="DEBUG") as cm:
            rules = self._run(self.exchange._format_trading_rules([unmapped]))

        self.assertEqual([], rules)
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual([], error_records)
        self.assertTrue(any(
            "Skipping trading rule for unmapped symbol GHOST/USDT" in r.getMessage()
            for r in cm.records if r.levelname == "DEBUG"))

    def test_malformed_rule_still_logged_as_error(self):
        """Genuinely malformed rules (bad decimals) must keep the loud ERROR path."""
        bad = _rule_payload(symbol="XMR/USDT", base="XMR", quote="USDT", priceDecimals=None)
        with self.assertLogs(self.exchange.logger(), level="DEBUG") as cm:
            rules = self._run(self.exchange._format_trading_rules([bad]))

        self.assertEqual([], rules)
        self.assertTrue(any(r.levelname == "ERROR" for r in cm.records))


class TestOrphanWarningDedupe(unittest.TestCase):

    ORPHAN_ID = "6a3ed97c85e97619a4f0a001"  # the actual orphan from the production logs

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.exchange = _make_exchange()
        self.exchange._set_trading_pair_symbol_map(bidict({"XMR/USDT": "XMR-USDT"}))

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _reconcile_with_exchange_orders(self, order_ids):
        response = [{"id": oid} for oid in order_ids]
        with patch.object(self.exchange, "_api_get", new=AsyncMock(return_value=response)):
            with self.assertLogs(self.exchange.logger(), level="DEBUG") as cm:
                self._run(self.exchange._reconcile_active_orders_after_reconnect())
        return cm.records

    @staticmethod
    def _orphan_warnings(records):
        return [r for r in records
                if r.levelname == "WARNING" and "not tracked locally" in r.getMessage()]

    def test_first_sighting_warns_repeat_sightings_do_not(self):
        """REGRESSION: the production instance warned 22 times for the same orphan id."""
        first = self._reconcile_with_exchange_orders([self.ORPHAN_ID])
        self.assertEqual(1, len(self._orphan_warnings(first)))

        second = self._reconcile_with_exchange_orders([self.ORPHAN_ID])
        self.assertEqual(0, len(self._orphan_warnings(second)))
        # The repeat is still observable at DEBUG for forensics.
        self.assertTrue(any(
            r.levelname == "DEBUG" and "known orphan" in r.getMessage() for r in second))

    def test_resolved_then_reappearing_orphan_warns_again(self):
        self._reconcile_with_exchange_orders([self.ORPHAN_ID])
        self._reconcile_with_exchange_orders([])  # orphan cancelled/filled -> forgotten
        third = self._reconcile_with_exchange_orders([self.ORPHAN_ID])
        self.assertEqual(1, len(self._orphan_warnings(third)))

    def test_new_orphan_alongside_known_orphan_warns(self):
        self._reconcile_with_exchange_orders([self.ORPHAN_ID])
        mixed = self._reconcile_with_exchange_orders([self.ORPHAN_ID, "brand-new-orphan"])
        warnings = self._orphan_warnings(mixed)
        self.assertEqual(1, len(warnings))
        self.assertIn("brand-new-orphan", warnings[0].getMessage())

    def test_structured_event_emitted_every_reconciliation(self):
        """Telemetry must not be deduped — only the log line is."""
        with patch.object(self.exchange, "_emit_structured_event", new=MagicMock()) as emit:
            response = [{"id": self.ORPHAN_ID}]
            with patch.object(self.exchange, "_api_get", new=AsyncMock(return_value=response)):
                self._run(self.exchange._reconcile_active_orders_after_reconnect())
                self._run(self.exchange._reconcile_active_orders_after_reconnect())
        recon_calls = [c for c in emit.call_args_list
                       if c.args and c.args[0] == "post_reconnect_order_reconciliation"]
        self.assertEqual(2, len(recon_calls))
        self.assertEqual(1, recon_calls[0].args[1]["orphans"])


if __name__ == "__main__":
    unittest.main()
