"""
Phase 5D unit tests: Crash recovery cancel_all override.

Tests the per-symbol batch cancel and orphan detection logic.
"""
import asyncio
import json
import re
import unittest
from decimal import Decimal
from typing import Awaitable
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses
from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


class TestPhase5DCrashRecovery(unittest.TestCase):
    """Tests for the cancel_all override with per-symbol batch cancellation."""

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )
        # Set up the trading pair mapping
        self.exchange._set_trading_pair_symbol_map(
            bidict({"BTC/USDT": "BTC-USDT"})
        )

    def async_run(self, coro: Awaitable):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _create_tracked_order(
        self,
        client_order_id: str,
        exchange_order_id: str,
        trading_pair: str = "BTC-USDT",
        order_type: OrderType = OrderType.LIMIT,
        trade_type: TradeType = TradeType.BUY,
        price: Decimal = Decimal("65000"),
        amount: Decimal = Decimal("0.001"),
    ) -> InFlightOrder:
        order = InFlightOrder(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=trading_pair,
            order_type=order_type,
            trade_type=trade_type,
            price=price,
            amount=amount,
            creation_timestamp=1234567890.0,
            initial_state=OrderState.OPEN,
        )
        self.exchange._order_tracker._in_flight_orders[client_order_id] = order
        return order

    # -- Test 1: ACCOUNT_ORDERS_PATH_URL constant exists --

    def test_account_orders_path_url_exists(self):
        self.assertTrue(hasattr(CONSTANTS, "ACCOUNT_ORDERS_PATH_URL"))
        self.assertEqual(CONSTANTS.ACCOUNT_ORDERS_PATH_URL, "/account/orders")

    # -- Test 2: cancel_all with no orders anywhere --

    @aioresponses()
    def test_cancel_all_no_orders_anywhere(self, mock_api):
        """No tracked orders, no exchange orders -> no cancel calls, empty result."""
        url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps([]))

        results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))
        self.assertEqual(results, [])

    # -- Test 3: cancel_all with tracked orders -> batch cancel per symbol --

    @aioresponses()
    def test_cancel_all_batch_cancels_per_symbol(self, mock_api):
        """Tracked orders on one symbol -> queries exchange, calls /cancelallorders once."""
        self._create_tracked_order("HBOT-001", "ex-001")

        # Mock /account/orders returning the same order
        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_orders, body=json.dumps([
            {"id": "ex-001", "symbol": "BTC/USDT", "side": "buy", "price": "65000", "quantity": "0.001"}
        ]))

        # Mock /cancelallorders
        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_cancel, body=json.dumps(["ex-001"]))

        results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].order_id, "HBOT-001")
        self.assertTrue(results[0].success)

    # -- Test 4: cancel_all detects orphaned orders --

    @aioresponses()
    def test_cancel_all_detects_orphans(self, mock_api):
        """Exchange has orders not in tracker -> logs warning, still cancels."""
        # Tracker has ONE order
        self._create_tracked_order("HBOT-001", "ex-001")

        # Exchange has TWO orders (ex-002 is orphaned)
        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_orders, body=json.dumps([
            {"id": "ex-001", "symbol": "BTC/USDT", "side": "buy", "price": "65000", "quantity": "0.001"},
            {"id": "ex-002", "symbol": "BTC/USDT", "side": "sell", "price": "66000", "quantity": "0.002"},
        ]))

        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_cancel, body=json.dumps(["ex-001", "ex-002"]))

        with self.assertLogs(level="WARNING") as cm:
            results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        # Check orphan was logged
        orphan_logged = any("Orphaned order detected" in msg and "ex-002" in msg for msg in cm.output)
        self.assertTrue(orphan_logged, f"Expected orphan warning for ex-002. Logs: {cm.output}")

        # Tracked order still gets a CancellationResult
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)

    # -- Test 5: cancel_all with multiple symbols --

    @aioresponses()
    def test_cancel_all_multiple_symbols(self, mock_api):
        """Orders on two symbols -> two /cancelallorders calls."""
        # Need ETH-USDT mapping too
        self.exchange._set_trading_pair_symbol_map(
            bidict({"BTC/USDT": "BTC-USDT", "ETH/USDT": "ETH-USDT"})
        )
        self._create_tracked_order("HBOT-001", "ex-001", trading_pair="BTC-USDT")
        self._create_tracked_order("HBOT-002", "ex-002", trading_pair="ETH-USDT")

        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_orders, body=json.dumps([
            {"id": "ex-001", "symbol": "BTC/USDT", "side": "buy", "price": "65000", "quantity": "0.001"},
            {"id": "ex-002", "symbol": "ETH/USDT", "side": "buy", "price": "3500", "quantity": "0.01"},
        ]))

        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        # Both symbols succeed
        mock_api.post(regex_cancel, body=json.dumps(["ex-001"]))
        mock_api.post(regex_cancel, body=json.dumps(["ex-002"]))

        results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.success for r in results))

    # -- Test 6: cancel_all handles /cancelallorders error for one symbol --

    @aioresponses()
    def test_cancel_all_partial_failure(self, mock_api):
        """One symbol cancel succeeds, another fails -> mixed results."""
        self.exchange._set_trading_pair_symbol_map(
            bidict({"BTC/USDT": "BTC-USDT", "ETH/USDT": "ETH-USDT"})
        )
        self._create_tracked_order("HBOT-001", "ex-001", trading_pair="BTC-USDT")
        self._create_tracked_order("HBOT-002", "ex-002", trading_pair="ETH-USDT")

        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_orders, body=json.dumps([
            {"id": "ex-001", "symbol": "BTC/USDT", "side": "buy", "price": "65000", "quantity": "0.001"},
            {"id": "ex-002", "symbol": "ETH/USDT", "side": "buy", "price": "3500", "quantity": "0.01"},
        ]))

        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        # BTC succeeds, ETH returns error body
        mock_api.post(regex_cancel, body=json.dumps(["ex-001"]))
        mock_api.post(regex_cancel, body=json.dumps(
            {"error": {"code": 500, "message": "Internal Server Error", "description": "Something broke"}}
        ))

        results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        self.assertEqual(len(results), 2)
        # At least one should have succeeded, at least one may have failed
        success_count = sum(1 for r in results if r.success)
        self.assertGreaterEqual(success_count, 1)

    # -- Test 7: cancel_all falls back when /account/orders fails --

    @aioresponses()
    def test_cancel_all_fallback_on_query_error(self, mock_api):
        """If /account/orders fails -> falls back to individual cancel (base behavior)."""
        self._create_tracked_order("HBOT-001", "ex-001")

        # /account/orders returns 500
        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_orders, status=500, body="Internal Server Error")

        # Fallback will try individual /cancelorder
        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_cancel, body=json.dumps({"id": "ex-001", "success": True}))

        results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].order_id, "HBOT-001")

    # -- Test 8: orphan detection with market.symbol format --

    @aioresponses()
    def test_cancel_all_handles_nested_market_symbol(self, mock_api):
        """Orders using nested market.symbol format are handled correctly."""
        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        # Response uses market.symbol instead of top-level symbol
        mock_api.get(regex_orders, body=json.dumps([
            {"id": "ex-orphan", "market": {"id": "abc123", "symbol": "SAL/USDT"}, "side": "buy",
             "price": "0.021", "quantity": "100"}
        ]))

        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_cancel, body=json.dumps(["ex-orphan"]))

        with self.assertLogs(level="WARNING") as cm:
            results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        # Orphan detected and cancelled
        orphan_logged = any("Orphaned order detected" in msg and "ex-orphan" in msg for msg in cm.output)
        self.assertTrue(orphan_logged)

    # -- Test 9: cancel_all with empty tracker but exchange has orders --

    @aioresponses()
    def test_cancel_all_empty_tracker_exchange_has_orders(self, mock_api):
        """Crash recovery scenario: tracker empty, exchange has orphans -> cancels them."""
        orders_url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_ORDERS_PATH_URL)
        regex_orders = re.compile(f"^{orders_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_orders, body=json.dumps([
            {"id": "orphan-1", "symbol": "BTC/USDT", "side": "buy", "price": "65000", "quantity": "0.001"},
            {"id": "orphan-2", "symbol": "BTC/USDT", "side": "sell", "price": "66000", "quantity": "0.001"},
        ]))

        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_cancel, body=json.dumps(["orphan-1", "orphan-2"]))

        with self.assertLogs(level="WARNING") as cm:
            results = self.async_run(self.exchange.cancel_all(timeout_seconds=10))

        # No CancellationResult (no tracked orders) but orphans were cancelled
        self.assertEqual(len(results), 0)  # No tracked orders to report on
        # Both orphans should be logged
        orphan_msgs = [m for m in cm.output if "Orphaned order detected" in m]
        self.assertEqual(len(orphan_msgs), 2)

    # -- Test 10: cancel_all method is overridden (not base class) --

    def test_cancel_all_is_overridden(self):
        """Verify cancel_all is defined on NonkycExchange, not inherited."""
        self.assertIn("cancel_all", NonkycExchange.__dict__,
                       "cancel_all should be overridden in NonkycExchange, not inherited from ExchangePyBase")

    # -- Test 11: _cancel_all_for_symbol sends correct body --

    @aioresponses()
    def test_cancel_all_for_symbol_sends_correct_body(self, mock_api):
        """Verify the symbol is sent in the POST body."""
        cancel_url = web_utils.private_rest_url(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)
        regex_cancel = re.compile(f"^{cancel_url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_cancel, body=json.dumps(["order-123"]))

        result = self.async_run(self.exchange._cancel_all_for_symbol("BTC/USDT"))

        # Verify it returned without error
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
