"""CSF-V1 Phase 4 — NonKYC streams, order book, symbol map.

NKC-2: subscribeReports is ack-validated (correlated on request id; an error frame raises so
       the reconnect loop retries); unmatched WS error frames log a rate-limited WARNING.
NKC-3: stale duplicates (sequence <= last_seq) are dropped unconditionally — never inserted
       into an active reorder buffer where they jam the buffer and its timeout timer;
       _flush_reorder_buffer purges stale entries before the emptiness check.
NKC-5: symbol-map build survives a duplicate HB pair (keeps the first mapping) and skips
       markets whose base/quote contains a separator character.
External-order holds: read-only accounting of untracked (manual) active orders' held balance,
       refreshed from the reconciliation snapshot; exposed via external_order_holds(pair).
       NO cancellation, NO tracking-adoption.
"""
import asyncio
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import NonkycAPIUserStreamDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType

SYMBOLS = {
    "COINALPHA/HBOT": "COINALPHA-HBOT",
    "BTC/USDT": "BTC-USDT",
}


def _open_order(exchange_id, client_id, side, price, quantity, executed="0",
                symbol="COINALPHA/HBOT", is_active=True):
    """Open-order dict per the live schema (reconciliation and subscribeReports ack share it)."""
    return {
        "id": exchange_id,
        "userProvidedId": client_id,
        "symbol": symbol,
        "side": side,
        "status": "Active" if is_active else "Filled",
        "type": "limit",
        "price": price,
        "quantity": quantity,
        "executedQuantity": executed,
        "remainQuantity": str(Decimal(quantity) - Decimal(executed)),
        "isActive": is_active,
        "createdAt": 1700000000000,
        "updatedAt": 1700000000000,
    }


class _Base(unittest.TestCase):
    level = 0  # required: instances act as logging handlers via self.handle()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.log_records = []
        self.ex = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=list(SYMBOLS.values()), trading_required=False)
        self.ex._set_trading_pair_symbol_map(bidict(SYMBOLS))
        self.ex._set_current_timestamp(1_000_000_000)
        self.ex.logger().setLevel(1)
        self.ex.logger().addHandler(self)

    def tearDown(self):
        self.ex.logger().removeHandler(self)
        self.loop.close()

    def handle(self, record):
        self.log_records.append(record)

    def is_logged(self, log_level: str, message: str) -> bool:
        return any(
            record.levelname == log_level and message in record.getMessage()
            for record in self.log_records
        )

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _track(self, oid, eid, pair="COINALPHA-HBOT", side=TradeType.SELL):
        self.ex.start_tracking_order(
            order_id=oid, exchange_order_id=eid, trading_pair=pair,
            order_type=OrderType.LIMIT, trade_type=side,
            price=Decimal("100"), amount=Decimal("1"))
        return self.ex._order_tracker.active_orders[oid]


# =========================================================================
# NKC-2: subscribeReports ack validation
# =========================================================================

class TestSubscribeReportsAck(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.connector = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=["COINALPHA-HBOT"], trading_required=False)
        self.connector._set_trading_pair_symbol_map(bidict(SYMBOLS))
        auth = NonkycAuth(api_key="k", secret_key="s", time_provider=MagicMock())
        self.uds = NonkycAPIUserStreamDataSource(
            auth=auth, trading_pairs=["COINALPHA-HBOT"],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory)

    def tearDown(self):
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _mock_ws(self, sent, frames_for_id):
        """frames_for_id(request_id) -> list of frame dicts to yield."""
        mock_ws = AsyncMock()

        async def capture_send(request):
            sent.append(request.payload)

        mock_ws.send = capture_send

        async def frame_iter():
            for frame in frames_for_id(sent[-1]["id"]):
                msg = MagicMock()
                msg.data = frame
                yield msg

        mock_ws.iter_messages = MagicMock(side_effect=lambda: frame_iter())
        return mock_ws

    def test_rejected_subscription_ack_raises(self):
        # Would-have-caught: pre-fix a rejected subscribeReports was fire-and-forget —
        # no real-time order/fill events with zero diagnostics.
        sent = []
        mock_ws = self._mock_ws(sent, lambda rid: [
            {"id": rid, "jsonrpc": "2.0",
             "error": {"code": 404, "message": "Requested method not found"}},
        ])
        with self.assertRaises(IOError) as ctx:
            self._run(self.uds._subscribe_channels(mock_ws))
        self.assertIn("subscribeReports subscription rejected", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    def test_success_ack_correlates_on_id_and_skips_unrelated_frames(self):
        sent = []
        mock_ws = self._mock_ws(sent, lambda rid: [
            {"jsonrpc": "2.0", "method": "ticker", "params": {"lastPrice": "1"}},
            {"id": rid + 999, "jsonrpc": "2.0", "error": {"code": 1, "message": "other"}},
            {"id": rid, "jsonrpc": "2.0", "method": "subscribeReports", "result": []},
        ])
        self._run(self.uds._subscribe_channels(mock_ws))  # no raise
        methods = [p["method"] for p in sent]
        self.assertIn(CONSTANTS.WS_METHOD_SUBSCRIBE_USER_ORDERS, methods)

    def test_socket_closed_before_ack_raises(self):
        sent = []
        mock_ws = self._mock_ws(sent, lambda rid: [])
        with self.assertRaises(IOError) as ctx:
            self._run(self.uds._subscribe_channels(mock_ws))
        self.assertIn("closed before subscribeReports", str(ctx.exception))

    def test_ack_snapshot_feeds_external_holds(self):
        # The ack result is a live snapshot of ALL open orders (incl. manual ones) —
        # fed to the read-only external-holds accounting, never adopted/tracked.
        sent = []
        snapshot = [_open_order("EX-M1", "manual-1", "buy", "100", "2")]
        mock_ws = self._mock_ws(sent, lambda rid: [
            {"id": rid, "jsonrpc": "2.0", "method": "subscribeReports", "result": snapshot},
        ])
        self._run(self.uds._subscribe_channels(mock_ws))
        holds = self.connector.external_order_holds("COINALPHA-HBOT")
        self.assertEqual(Decimal("200"), holds["quote"])
        self.assertEqual(Decimal("0"), holds["base"])
        # Not adopted into tracking
        self.assertNotIn("manual-1", self.connector._order_tracker.active_orders)


# =========================================================================
# NKC-2: catch-all WS error-frame WARNING in the user stream listener
# =========================================================================

class TestErrorFrameLogged(_Base):

    def _feed_listener(self, events):
        async def run():
            async def mock_iter():
                for e in events:
                    yield e

            with patch.object(self.ex, "_iter_user_event_queue", mock_iter):
                await self.ex._user_stream_event_listener()

        self._run(run())

    def test_error_frame_logged_at_warning(self):
        # Would-have-caught: pre-fix this frame fell through every branch unlogged.
        self._feed_listener([
            {"id": 105, "jsonrpc": "2.0",
             "error": {"code": 20001, "message": "Insufficient funds"}},
        ])
        self.assertTrue(self.is_logged("WARNING", "NonKYC WS error frame"))
        self.assertTrue(self.is_logged("WARNING", "Insufficient funds"))

    def test_error_frame_warning_is_rate_limited(self):
        self._feed_listener([
            {"id": 1, "error": {"code": 1, "message": "first"}},
            {"id": 2, "error": {"code": 2, "message": "second"}},
        ])
        warns = [r for r in self.log_records
                 if r.levelname == "WARNING" and "NonKYC WS error frame" in r.getMessage()]
        self.assertEqual(1, len(warns))

    def test_error_frame_warns_again_after_interval(self):
        self._feed_listener([{"id": 1, "error": {"code": 1, "message": "first"}}])
        self.ex._ws_error_frame_last_warn = time.time() - 31.0
        self._feed_listener([{"id": 2, "error": {"code": 2, "message": "second"}}])
        warns = [r for r in self.log_records
                 if r.levelname == "WARNING" and "NonKYC WS error frame" in r.getMessage()]
        self.assertEqual(2, len(warns))


# =========================================================================
# NKC-3: reorder buffer — stale duplicates with an ACTIVE buffer
# =========================================================================

class TestReorderBufferStaleEntries(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.trading_pair = "COINALPHA-HBOT"
        self.ex_trading_pair = "COINALPHA/HBOT"
        self.connector = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=[self.trading_pair], trading_required=False)
        self.connector._set_trading_pair_symbol_map(
            bidict({self.ex_trading_pair: self.trading_pair}))
        self.data_source = NonkycAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory)

    def tearDown(self):
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _make_diff_message(self, sequence: int) -> dict:
        return {
            "jsonrpc": "2.0",
            "method": "updateOrderbook",
            "params": {
                "asks": [{"price": "67883.06", "quantity": "0.010917"}],
                "bids": [{"price": "67679.55", "quantity": "0.000422"}],
                "symbol": self.ex_trading_pair,
                "timestamp": 1772170410000,
                "sequence": sequence,
            },
        }

    def test_stale_duplicate_with_active_buffer_dropped(self):
        # Would-have-caught: pre-fix a stale duplicate was INSERTED into the active buffer.
        # _flush_reorder_buffer only pops last_seq + 1, so the stale entry kept the buffer
        # non-empty and the timeout timer armed forever -> hair-trigger spurious resyncs.
        self.data_source._last_sequence[self.trading_pair] = 100
        msg_queue = asyncio.Queue()

        # Active buffer with a forward out-of-order message (103), timer running
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(103), msg_queue))
        self.assertIn(103, self.data_source._reorder_buffer[self.trading_pair])

        # Stale duplicate (99) arrives — must be dropped, NOT buffered
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(99), msg_queue))

        self.assertTrue(msg_queue.empty())
        buffer = self.data_source._reorder_buffer[self.trading_pair]
        self.assertNotIn(99, buffer)
        self.assertEqual({103}, set(buffer.keys()))

        # Gap then fills normally: 101, 102 arrive -> everything flushes, timer cleared
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(101), msg_queue))
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(102), msg_queue))
        self.assertEqual(3, msg_queue.qsize())
        self.assertEqual(103, self.data_source._last_sequence[self.trading_pair])
        self.assertNotIn(self.trading_pair, self.data_source._reorder_buffer)
        self.assertNotIn(self.trading_pair, self.data_source._reorder_timer_start)

    def test_flush_purges_stale_entries_and_clears_timer(self):
        # A buffer holding ONLY stale entries (e.g. left behind by a snapshot that advanced
        # last_seq) must be purged so the timeout timer disarms.
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._reorder_buffer[self.trading_pair] = {
            95: (self._make_diff_message(95), time.time()),
            100: (self._make_diff_message(100), time.time()),
        }
        self.data_source._reorder_timer_start[self.trading_pair] = time.time()
        msg_queue = asyncio.Queue()

        self._run(self.data_source._flush_reorder_buffer(self.trading_pair, msg_queue))

        self.assertTrue(msg_queue.empty())  # stale entries are purged, never emitted
        self.assertNotIn(self.trading_pair, self.data_source._reorder_buffer)
        self.assertNotIn(self.trading_pair, self.data_source._reorder_timer_start)

    def test_stale_entry_does_not_arm_timeout_for_next_out_of_order_message(self):
        # End-to-end regression for the jam: stale duplicate while a buffer is active must
        # not keep the timer armed after the buffer resolves; a later out-of-order message
        # gets its own fresh 2s window instead of an instant timeout resync.
        self.data_source._last_sequence[self.trading_pair] = 100
        mock_ws = AsyncMock()
        self.data_source._ws_assistant = mock_ws
        msg_queue = asyncio.Queue()

        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(102), msg_queue))  # buffer {102}, timer armed
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(99), msg_queue))   # stale — dropped
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(101), msg_queue))  # fills gap, flushes 102

        self.assertNotIn(self.trading_pair, self.data_source._reorder_timer_start)

        # Age the (now nonexistent) timer far past the timeout, then send out-of-order 104:
        # it must be buffered with a FRESH timer, not instantly resynced.
        self._run(self.data_source._parse_order_book_diff_message(
            self._make_diff_message(104), msg_queue))
        self.assertIn(104, self.data_source._reorder_buffer[self.trading_pair])
        mock_ws.send.assert_not_called()  # no resubscribe was triggered


# =========================================================================
# NKC-5: symbol-map crash-proofing
# =========================================================================

class TestSymbolMapHardening(_Base):

    def test_duplicate_market_keeps_first_mapping_without_crash(self):
        # Would-have-caught: pre-fix bidict raised ValueDuplicationError -> symbol map
        # unbuildable -> connector dead.
        exchange_info = [
            {"symbol": "AAA/USDT", "primaryTicker": "AAA", "isActive": True},
            {"symbol": "AAA2/USDT", "primaryTicker": "AAA",
             "secondaryTicker": "USDT", "isActive": True},  # same HB pair AAA-USDT
            {"symbol": "BBB/USDT", "isActive": True},
        ]
        self.ex._initialize_trading_pair_symbols_from_exchange_info(exchange_info)
        mapping = self._run(self.ex.trading_pair_symbol_map())
        self.assertEqual("AAA-USDT", mapping["AAA/USDT"])  # first mapping kept
        self.assertNotIn("AAA2/USDT", mapping)
        self.assertEqual("BBB-USDT", mapping["BBB/USDT"])  # build continued past the dup
        self.assertTrue(self.is_logged("WARNING", "Duplicate trading pair"))

    def test_separator_ticker_skipped(self):
        exchange_info = [
            {"symbol": "FOO-BAR/USDT", "primaryTicker": "FOO-BAR", "isActive": True},
            {"symbol": "BAZ/USDT", "primaryTicker": "BAZ",
             "secondaryTicker": "US_DT", "isActive": True},
            {"symbol": "OK/USDT", "isActive": True},
        ]
        self.ex._initialize_trading_pair_symbols_from_exchange_info(exchange_info)
        mapping = self._run(self.ex.trading_pair_symbol_map())
        self.assertNotIn("FOO-BAR/USDT", mapping)
        self.assertNotIn("BAZ/USDT", mapping)
        self.assertEqual("OK-USDT", mapping["OK/USDT"])
        self.assertTrue(self.is_logged("WARNING", "contains a separator character"))


# =========================================================================
# External-order holds (feeds Phase 5 understatement exclusion)
# =========================================================================

class TestExternalOrderHolds(_Base):

    def test_holds_computed_from_reconciliation_fixture(self):
        # One manual buy + one manual sell + one TRACKED order (must be excluded).
        self._track("mine-1", "EX-MINE-1")
        snapshot = [
            _open_order("EX-MINE-1", "mine-1", "sell", "100", "1"),          # tracked — excluded
            _open_order("EX-MAN-B", "manual-b", "buy", "0.05", "300", executed="100"),
            _open_order("EX-MAN-S", "manual-s", "sell", "0.06", "50", executed="10"),
        ]
        api = AsyncMock(return_value=snapshot)
        with patch.object(self.ex, "_api_get", new=api):
            self._run(self.ex._reconcile_active_orders_after_reconnect())

        holds = self.ex.external_order_holds("COINALPHA-HBOT")
        # buy: 0.05 * (300 - 100) = 10 quote; sell: 50 - 10 = 40 base
        self.assertEqual(Decimal("10"), holds["quote"])
        self.assertEqual(Decimal("40"), holds["base"])

    def test_holds_cleared_when_orphans_resolve(self):
        self.ex._external_order_holds = {
            "COINALPHA-HBOT": {"quote": Decimal("10"), "base": Decimal("40")}}
        api = AsyncMock(return_value=[])
        with patch.object(self.ex, "_api_get", new=api):
            self._run(self.ex._reconcile_active_orders_after_reconnect())
        holds = self.ex.external_order_holds("COINALPHA-HBOT")
        self.assertEqual(Decimal("0"), holds["quote"])
        self.assertEqual(Decimal("0"), holds["base"])

    def test_unknown_pair_returns_zeros(self):
        holds = self.ex.external_order_holds("NOPE-PAIR")
        self.assertEqual({"quote": Decimal("0"), "base": Decimal("0")}, holds)

    def test_sentinel_tracked_order_excluded_by_client_id(self):
        # An UNKNOWN-sentinel tracked order matches by userProvidedId, not exchange id.
        self._track("mine-2", NonkycExchange.UNKNOWN_EXCHANGE_ORDER_ID)
        snapshot = [_open_order("EX-REAL-2", "mine-2", "buy", "100", "1")]
        self._run(self.ex._update_external_holds_from_order_snapshot(snapshot))
        holds = self.ex.external_order_holds("COINALPHA-HBOT")
        self.assertEqual(Decimal("0"), holds["quote"])

    def test_inactive_and_malformed_orders_skipped(self):
        snapshot = [
            _open_order("EX-1", "m-1", "buy", "100", "1", is_active=False),
            {"id": "EX-2", "userProvidedId": "m-2", "side": "buy",
             "symbol": "COINALPHA/HBOT", "price": "not-a-number", "quantity": "1"},
            "not-a-dict",
            _open_order("EX-3", "m-3", "buy", "100", "1", symbol="UNKNOWN/PAIR"),
            _open_order("EX-4", "m-4", "buy", "2", "3"),
        ]
        self._run(self.ex._update_external_holds_from_order_snapshot(snapshot))
        holds = self.ex.external_order_holds("COINALPHA-HBOT")
        self.assertEqual(Decimal("6"), holds["quote"])  # only EX-4 counts

    def test_fully_executed_order_contributes_nothing(self):
        snapshot = [_open_order("EX-5", "m-5", "sell", "100", "1", executed="1")]
        self._run(self.ex._update_external_holds_from_order_snapshot(snapshot))
        self.assertEqual(Decimal("0"),
                         self.ex.external_order_holds("COINALPHA-HBOT")["base"])


if __name__ == "__main__":
    unittest.main()
