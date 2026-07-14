"""CSF-V1 Phase 3 — NonKYC order lifecycle & balances.

NKC-1: the "UNKNOWN" exchange-order-id sentinel (ambiguous 503 on createorder) must not poison
       the REST status poll (falls back to the client id — /getorder accepts the userProvidedId
       as a path segment, live-verified 2026-07-14), must be repairable by later WS/REST updates,
       and must never key the bulk fill map (fills for a sentinel order still attach).
NKC-4: a REST /balances snapshot requested BEFORE a local pre-adjust hold was applied must not
       clobber the fresher hold (LOG-3 race).
NKC-6: market BUY orders (price None/NaN) skip the balance pre-adjust instead of raising into
       the non-fatal except.
NKC-7: _bulk_fills_fetched_this_cycle is reset via try/finally even when the cycle raises.
NKC-8: ONE global /account/trades fetch per poll cycle (the symbol filter is ignored server-side).
NKC-9: unknown order status strings coerce to OPEN with rate-limited warnings; repeated unknown
       REST statuses for the same order trigger a reconciliation log.
"""
import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import OrderState

SYMBOLS = {
    "BTC/USDT": "BTC-USDT",
    "ETH/USDT": "ETH-USDT",
}


def _getorder_response(exchange_id, client_id, status="Active"):
    return {
        "id": exchange_id,
        "userProvidedId": client_id,
        "symbol": "BTC/USDT",
        "side": "sell",
        "status": status,
        "type": "limit",
        "quantity": "1",
        "price": "100",
        "executedQuantity": "0",
        "remainQuantity": "1",
        "isActive": status == "Active",
        "createdAt": 1700000000000,
        "updatedAt": 1700000000000,
    }


def _trade(tid, orderid, symbol="BTC/USDT", side="Sell", price="100", qty="1"):
    return {
        "id": tid, "orderid": orderid, "market": {"symbol": symbol}, "side": side,
        "triggeredBy": side, "price": price, "quantity": qty,
        "timestamp": 1700000000000, "fee": "0.1",
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ex = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=list(SYMBOLS.values()), trading_required=False)
        self.ex._set_trading_pair_symbol_map(bidict(SYMBOLS))
        self.ex._set_current_timestamp(1_000_000_000)
        self.ex._last_poll_timestamp = 0.0

    def tearDown(self):
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _track(self, oid, eid, pair="BTC-USDT", side=TradeType.SELL):
        self.ex.start_tracking_order(
            order_id=oid, exchange_order_id=eid, trading_pair=pair,
            order_type=OrderType.LIMIT, trade_type=side,
            price=Decimal("100"), amount=Decimal("1"))
        return self.ex._order_tracker.active_orders[oid]


# =========================================================================
# NKC-1(a): sentinel status poll falls back to the client id
# =========================================================================

class TestSentinelStatusPoll(_Base):

    def test_sentinel_status_poll_queries_by_client_id_and_repairs(self):
        order = self._track("c-1", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)
        api = AsyncMock(return_value=_getorder_response("EX-1", "c-1"))
        with patch.object(self.ex, "_api_get", new=api):
            update = self._run(self.ex._request_order_status(order))

        self.assertEqual(f"{CONSTANTS.ORDER_INFO_PATH_URL}/c-1",
                         api.call_args.kwargs["path_url"])
        self.assertEqual(OrderState.OPEN, update.new_state)  # live "Active" spelling maps
        self.assertEqual("EX-1", update.exchange_order_id)
        # NKC-1(b): the sentinel is repaired from the REST response.
        self.assertEqual("EX-1", order.exchange_order_id)

    def test_sentinel_status_poll_never_hits_getorder_unknown(self):
        # Would-have-caught: pre-fix the poll hit /getorder/UNKNOWN -> 400/20002 -> the
        # not-found counter falsely marked a LIVE order LOST/FAILED.
        order = self._track("c-2", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)

        def fake_api(path_url, **kwargs):
            if path_url.endswith(f"/{NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID}"):
                raise IOError(f"Error: {CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE} "
                              f"{CONSTANTS.ORDER_NOT_EXIST_MESSAGE}")
            return _getorder_response("EX-2", "c-2")

        with patch.object(self.ex, "_api_get", new=AsyncMock(side_effect=fake_api)):
            update = self._run(self.ex._request_order_status(order))
        self.assertEqual(OrderState.OPEN, update.new_state)

    def test_real_exchange_id_still_preferred(self):
        order = self._track("c-3", "EX-3")
        api = AsyncMock(return_value=_getorder_response("EX-3", "c-3", status="Filled"))
        with patch.object(self.ex, "_api_get", new=api):
            update = self._run(self.ex._request_order_status(order))
        self.assertEqual(f"{CONSTANTS.ORDER_INFO_PATH_URL}/EX-3",
                         api.call_args.kwargs["path_url"])
        self.assertEqual(OrderState.FILLED, update.new_state)


# =========================================================================
# NKC-1(b): sentinel repair — both directions (REST above, WS here) + guards
# =========================================================================

class TestSentinelRepair(_Base):

    def test_ws_report_repairs_sentinel(self):
        order = self._track("c-9", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)
        event = {"method": "report", "params": {
            "id": "EX-REAL-9", "userProvidedId": "c-9", "symbol": "BTC/USDT",
            "side": "sell", "status": "Active", "type": "limit", "quantity": "1",
            "price": "100", "executedQuantity": "0", "reportType": "new",
            "createdAt": 1700000000000, "updatedAt": 1700000000000}}

        async def run():
            queue = asyncio.Queue()
            queue.put_nowait(event)

            async def mock_iter():
                while not queue.empty():
                    yield queue.get_nowait()

            with patch.object(self.ex, "_iter_user_event_queue", mock_iter):
                await self.ex._user_stream_event_listener()

        self._run(run())
        self.assertEqual("EX-REAL-9", order.exchange_order_id)

    def test_repair_helper_never_touches_a_real_id(self):
        order = self._track("c-10", "EX-10")
        self.ex._repair_unknown_exchange_order_id(order, "EX-OTHER")
        self.assertEqual("EX-10", order.exchange_order_id)

    def test_repair_helper_ignores_none_empty_and_sentinel(self):
        order = self._track("c-11", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)
        for bogus in (None, "", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID):
            self.ex._repair_unknown_exchange_order_id(order, bogus)
            self.assertEqual(NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID, order.exchange_order_id)


# =========================================================================
# NKC-1(c) + NKC-8: bulk fill map and the single global trades fetch
# =========================================================================

class TestBulkFillPoll(_Base):

    def test_fills_for_sentinel_order_still_attach(self):
        order = self._track("c-1", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)

        def fake_api(path_url, params=None, **kwargs):
            if path_url.startswith(f"{CONSTANTS.ORDER_INFO_PATH_URL}/"):
                self.assertTrue(path_url.endswith("/c-1"))  # resolved by CLIENT id
                return _getorder_response("EX-77", "c-1")
            if path_url == CONSTANTS.ACCOUNT_TRADES_PATH_URL:
                return [_trade("t-77", "EX-77")]
            raise AssertionError(f"unexpected path {path_url}")

        with patch.object(self.ex, "_api_get", new=AsyncMock(side_effect=fake_api)):
            self._run(self.ex._update_order_fills_from_trades())
        self._run(asyncio.sleep(0.05))

        self.assertEqual("EX-77", order.exchange_order_id)  # repaired
        self.assertIn("t-77", order.order_fills)  # fill attached to the tracked order
        self.assertTrue(self.ex._is_trade_processed("t-77"))

    def test_unresolvable_sentinel_never_keys_the_map(self):
        # /getorder fails (placement outcome still unknown): the cycle must complete, the
        # sentinel order must not swallow any trade, and nothing may crash.
        order = self._track("c-2", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)

        def fake_api(path_url, params=None, **kwargs):
            if path_url.startswith(f"{CONSTANTS.ORDER_INFO_PATH_URL}/"):
                raise IOError("Error: 20002 Order not found")
            return [_trade("t-88", "EX-88")]

        with patch.object(self.ex, "_api_get", new=AsyncMock(side_effect=fake_api)):
            self._run(self.ex._update_order_fills_from_trades())
        self._run(asyncio.sleep(0.05))

        self.assertEqual(NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID, order.exchange_order_id)
        self.assertEqual(0, len(order.order_fills))

    def test_single_global_trades_fetch_per_cycle(self):
        # Would-have-caught (NKC-8): pre-fix this issued one identical global fetch PER PAIR.
        self._track("c-3", "EX-3", pair="BTC-USDT")
        self._track("c-4", "EX-4", pair="ETH-USDT")
        api = AsyncMock(return_value=[])
        with patch.object(self.ex, "_api_get", new=api):
            self._run(self.ex._update_order_fills_from_trades())

        self.assertEqual(1, api.await_count)
        params = api.call_args.kwargs["params"]
        self.assertIn("since", params)
        self.assertNotIn("symbol", params)

    def test_fetch_error_is_non_fatal(self):
        self._track("c-5", "EX-5")
        with patch.object(self.ex, "_api_get", new=AsyncMock(side_effect=IOError("boom"))):
            self._run(self.ex._update_order_fills_from_trades())  # must not raise


# =========================================================================
# NKC-7: bulk-fills flag reset via try/finally
# =========================================================================

class TestBulkFillsFlag(_Base):

    def test_flag_reset_when_cycle_raises(self):
        # Would-have-caught: pre-fix an exception left the flag stuck True, permanently
        # disabling per-order fill recovery in _all_trade_updates_for_order.
        with patch.object(self.ex, "_update_order_fills_from_trades",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                self._run(self.ex._status_polling_loop_fetch_updates())
        self.assertFalse(self.ex._bulk_fills_fetched_this_cycle)

    def test_flag_true_during_cycle_false_after(self):
        seen = {}

        async def record_flag():
            seen["during"] = self.ex._bulk_fills_fetched_this_cycle

        with patch.object(self.ex, "_update_order_fills_from_trades", new=AsyncMock()), \
                patch("hummingbot.connector.exchange_py_base.ExchangePyBase."
                      "_status_polling_loop_fetch_updates",
                      new=AsyncMock(side_effect=record_flag)):
            self._run(self.ex._status_polling_loop_fetch_updates())

        self.assertTrue(seen["during"])
        self.assertFalse(self.ex._bulk_fills_fetched_this_cycle)


# =========================================================================
# NKC-4: REST balance snapshot vs fresher local pre-adjust
# =========================================================================

class TestBalanceSnapshotRace(_Base):

    def test_fresh_pre_adjust_survives_stale_rest_snapshot(self):
        # Would-have-caught (LOG-3): the REST snapshot was requested BEFORE the order was
        # placed; the local pre-adjust hold lands during the round trip and pre-fix was
        # clobbered back to the inflated value.
        self.ex._account_available_balances["USDT"] = Decimal("100")
        self.ex._account_balances["USDT"] = Decimal("100")

        async def fake_api(path_url, **kwargs):
            # Order placement happens while the REST request is in flight.
            self.ex._account_available_balances["USDT"] = Decimal("40")
            self.ex._pre_adjusted_assets["USDT"] = time.time() + 1.0
            return [{"asset": "USDT", "available": "100", "held": "0"}]

        with patch.object(self.ex, "_api_get", new=fake_api):
            self._run(self.ex._update_balances())

        self.assertEqual(Decimal("40"), self.ex._account_available_balances["USDT"])
        self.assertEqual(Decimal("100"), self.ex._account_balances["USDT"])  # total still applies
        self.assertIn("USDT", self.ex._account_balances)  # not deleted as a stale asset

    def test_stale_pre_adjust_is_overwritten_normally(self):
        self.ex._account_available_balances["USDT"] = Decimal("40")
        self.ex._account_balances["USDT"] = Decimal("100")
        self.ex._pre_adjusted_assets["USDT"] = time.time() - 10.0  # before the snapshot start

        api = AsyncMock(return_value=[{"asset": "USDT", "available": "100", "held": "0"}])
        with patch.object(self.ex, "_api_get", new=api):
            self._run(self.ex._update_balances())

        self.assertEqual(Decimal("100"), self.ex._account_available_balances["USDT"])


# =========================================================================
# NKC-6: market BUY pre-adjust guard
# =========================================================================

class TestMarketBuyPreAdjust(_Base):

    def _make_bound_exchange(self):
        exchange = MagicMock(spec=NonkycExchange)
        exchange._account_available_balances = {"USDT": Decimal("100")}
        exchange._account_balances = {"USDT": Decimal("100")}
        exchange._trading_fees = {}
        exchange._pre_adjusted_assets = {}
        exchange._balance_settling = False
        exchange._balance_settle_start = 0.0
        exchange._BALANCE_SETTLE_TIMEOUT = 15.0
        exchange._nonce_error_cooldown_until = 0.0
        exchange._last_server_disconnect_time = 0.0
        exchange._SERVER_DISCONNECT_BACKOFF = 10.0
        exchange.estimate_fee_pct = MagicMock(return_value=0.0)
        exchange.logger = MagicMock(return_value=MagicMock())
        exchange._place_order_and_process_update = (
            NonkycExchange._place_order_and_process_update.__get__(exchange, NonkycExchange))
        return exchange

    def _market_buy_order(self, price):
        order = MagicMock()
        order.trading_pair = "BTC-USDT"
        order.trade_type = TradeType.BUY
        order.amount = Decimal("10")
        order.price = price
        order.client_order_id = "mkt-1"
        order.order_type = OrderType.MARKET
        return order

    def _run_placement(self, price):
        exchange = self._make_bound_exchange()
        order = self._market_buy_order(price)
        with patch("hummingbot.connector.exchange_py_base.ExchangePyBase."
                   "_place_order_and_process_update",
                   new_callable=AsyncMock, return_value="ex-mkt-1"):
            result = self._run(exchange._place_order_and_process_update(order))
        return exchange, result

    def test_market_buy_nan_price_skips_pre_adjust_without_raising(self):
        # Would-have-caught: pre-fix `amount * NaN-price` raised into the non-fatal except
        # and logged "Local balance pre-adjust failed" on EVERY market buy.
        exchange, result = self._run_placement(Decimal("NaN"))
        self.assertEqual("ex-mkt-1", result)
        self.assertEqual(Decimal("100"), exchange._account_available_balances["USDT"])
        self.assertEqual({}, exchange._pre_adjusted_assets)
        exchange.logger.return_value.warning.assert_not_called()

    def test_market_buy_none_price_skips_pre_adjust_without_raising(self):
        exchange, result = self._run_placement(None)
        self.assertEqual("ex-mkt-1", result)
        self.assertEqual({}, exchange._pre_adjusted_assets)
        exchange.logger.return_value.warning.assert_not_called()

    def test_limit_buy_pre_adjust_still_applies(self):
        exchange = self._make_bound_exchange()
        order = self._market_buy_order(Decimal("5"))
        order.order_type = OrderType.LIMIT
        with patch("hummingbot.connector.exchange_py_base.ExchangePyBase."
                   "_place_order_and_process_update",
                   new_callable=AsyncMock, return_value="ex-lmt-1"):
            self._run(exchange._place_order_and_process_update(order))
        # 10 * 5 = 50 held -> 100 - 50 = 50 available
        self.assertEqual(Decimal("50"), exchange._account_available_balances["USDT"])
        self.assertIn("USDT", exchange._pre_adjusted_assets)


# =========================================================================
# NKC-9: unknown-status logging
# =========================================================================

class TestUnknownStatusLogging(_Base):

    def test_unknown_status_coerces_to_open(self):
        state = self.ex._order_state_for_status("someNewStatus", "c-1", "ws")
        self.assertEqual(OrderState.OPEN, state)

    def test_known_status_still_maps(self):
        self.assertEqual(OrderState.FILLED, self.ex._order_state_for_status("Filled", "c-1", "ws"))
        self.assertEqual(OrderState.OPEN, self.ex._order_state_for_status("Active", "c-1", "rest"))

    def test_ws_unknown_status_warns_rate_limited(self):
        with patch.object(NonkycExchange, "logger") as mock_logger:
            self.ex._order_state_for_status("weird", "c-1", "ws")
            self.ex._order_state_for_status("weird", "c-1", "ws")  # inside the 30s window
        warnings = mock_logger.return_value.warning.call_args_list
        self.assertEqual(1, len(warnings))
        self.assertIn("weird", warnings[0].args[0])

    def test_repeated_unknown_rest_status_triggers_reconciliation_log(self):
        with patch.object(NonkycExchange, "logger") as mock_logger:
            self.ex._order_state_for_status("limbo", "c-2", "rest")
            # Bypass the 30s unknown-status rate limit but not the count logic.
            self.ex._unknown_status_last_warn.pop("rest:limbo", None)
            self.ex._order_state_for_status("limbo", "c-2", "rest")
        messages = [c.args[0] for c in mock_logger.return_value.warning.call_args_list]
        self.assertTrue(any("consecutive" in m and "reconcile" in m for m in messages),
                        f"no reconciliation log in {messages}")

    def test_known_status_resets_consecutive_unknown_count(self):
        self.ex._order_state_for_status("limbo", "c-3", "rest")
        self.assertEqual(1, self.ex._unknown_status_counts["c-3"])
        self.ex._order_state_for_status("Active", "c-3", "rest")
        self.assertNotIn("c-3", self.ex._unknown_status_counts)

    def test_rest_status_poll_unknown_status_returns_open(self):
        order = self._track("c-4", "EX-4")
        api = AsyncMock(return_value=_getorder_response("EX-4", "c-4", status="mysteryState"))
        with patch.object(self.ex, "_api_get", new=api):
            update = self._run(self.ex._request_order_status(order))
        self.assertEqual(OrderState.OPEN, update.new_state)


if __name__ == "__main__":
    unittest.main()
