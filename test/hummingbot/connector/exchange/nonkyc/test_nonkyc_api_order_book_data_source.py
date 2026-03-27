import asyncio
import json
import re
import time
from typing import Awaitable, Optional
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses
from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS, nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.web_assistant.ws_assistant import WSAssistant


class NonkycAPIOrderBookDataSourceTests(IsolatedAsyncioWrapperTestCase):

    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}/{cls.quote_asset}"

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []
        self.async_tasks = []

        self.connector = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=[self.trading_pair],
            trading_required=False,
        )
        self.connector._set_trading_pair_symbol_map(bidict({self.ex_trading_pair: self.trading_pair}))

        self.data_source = NonkycAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory,
        )
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)

    def tearDown(self) -> None:
        for task in self.async_tasks:
            task.cancel()
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def is_logged(self, log_level: str, message: str) -> bool:
        return any(
            record.levelname == log_level and message in record.getMessage()
            for record in self.log_records
        )

    @aioresponses()
    async def test_get_new_order_book_successful(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {
            "marketid": "643bfeeb5e07bba23a98a981",
            "symbol": self.ex_trading_pair,
            "timestamp": 1772169899391,
            "sequence": "6064",
            "bids": [{"price": "67679.55", "quantity": "0.000422"}],
            "asks": [{"price": "67883.06", "quantity": "0.010917"}],
        }
        mock_api.get(regex_url, body=json.dumps(response))

        order_book = await self.data_source.get_new_order_book(self.trading_pair)

        self.assertEqual(6064, order_book.snapshot_uid)

    @aioresponses()
    async def test_listen_for_order_book_diffs(self, mock_api):
        msg_queue = asyncio.Queue()
        raw_message = {
            "jsonrpc": "2.0",
            "method": "updateOrderbook",
            "params": {
                "asks": [{"price": "67883.06", "quantity": "0.010917"}],
                "bids": [{"price": "67679.55", "quantity": "0.000422"}],
                "symbol": self.ex_trading_pair,
                "timestamp": 1772170410000,
                "sequence": 1215882,
            },
        }

        # Set up last sequence so we don't trigger gap detection
        self.data_source._last_sequence[self.trading_pair] = 1215881

        await self.data_source._parse_order_book_diff_message(raw_message, msg_queue)

        self.assertFalse(msg_queue.empty())
        msg: OrderBookMessage = msg_queue.get_nowait()
        self.assertEqual(OrderBookMessageType.DIFF, msg.type)
        self.assertEqual(1215882, msg.update_id)

    @aioresponses()
    async def test_listen_for_order_book_snapshots_from_ws(self, mock_api):
        msg_queue = asyncio.Queue()
        raw_message = {
            "jsonrpc": "2.0",
            "method": "snapshotOrderbook",
            "params": {
                "symbol": self.ex_trading_pair,
                "sequence": 1215881,
                "asks": [{"price": "67883.06", "quantity": "0.010917"}],
                "bids": [{"price": "67679.55", "quantity": "0.000422"}],
            },
        }

        await self.data_source._parse_order_book_snapshot_message(raw_message, msg_queue)

        self.assertFalse(msg_queue.empty())
        msg: OrderBookMessage = msg_queue.get_nowait()
        self.assertEqual(OrderBookMessageType.SNAPSHOT, msg.type)
        self.assertEqual(1215881, msg.update_id)
        # Verify sequence was stored
        self.assertEqual(1215881, self.data_source._last_sequence[self.trading_pair])

    async def test_listen_for_trades_logs_trade_messages(self):
        msg_queue = asyncio.Queue()
        raw_message = {
            "jsonrpc": "2.0",
            "method": "updateTrades",
            "params": {
                "symbol": self.ex_trading_pair,
                "data": [
                    {
                        "id": "69a12a99f65594545010e592",
                        "price": "67799.74",
                        "quantity": "0.020323",
                        "side": "sell",
                        "timestamp": "2026-02-27T05:24:41.018Z",
                        "timestampms": 1772169881018,
                    }
                ],
            },
        }

        await self.data_source._parse_trade_message(raw_message, msg_queue)

        self.assertFalse(msg_queue.empty())
        msg: OrderBookMessage = msg_queue.get_nowait()
        self.assertEqual(OrderBookMessageType.TRADE, msg.type)

    async def test_snapshot_trades_handled(self):
        # snapshotTrades should route to trade queue
        event_message = {
            "jsonrpc": "2.0",
            "method": "snapshotTrades",
            "params": {
                "symbol": self.ex_trading_pair,
                "sequence": "6064",
                "data": [
                    {
                        "id": "69a12a99f65594545010e592",
                        "price": "67799.74",
                        "quantity": "0.020323",
                        "side": "sell",
                        "timestamp": "2026-02-27T05:24:41.018Z",
                        "timestampms": 1772169881018,
                    }
                ],
            },
        }

        channel = self.data_source._channel_originating_message(event_message)
        self.assertEqual(CONSTANTS.TRADE_EVENT_TYPE, channel)

    async def test_sequence_gap_triggers_ws_resubscribe(self):
        """Sequence gap should send WS unsubscribe + resubscribe, not REST snapshot."""
        self.data_source._last_sequence[self.trading_pair] = 100
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws

        msg_queue = asyncio.Queue()
        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(105), msg_queue
        )

        # Verify WS send was called (unsubscribe + resubscribe = 2 calls)
        self.assertEqual(2, mock_ws.send.call_count)
        # Verify warning was logged
        self.assertTrue(self.is_logged("WARNING", "sequence gap"))
        # Verify WS resubscribe info logged
        self.assertTrue(self.is_logged("INFO", "WS resubscribe sent"))
        # WS should NOT have been disconnected
        mock_ws.disconnect.assert_not_called()
        # Resync should be pending (waiting for snapshot)
        self.assertTrue(self.data_source._resync_pending.get(self.trading_pair, False))
        # Sequence should be cleared
        self.assertNotIn(self.trading_pair, self.data_source._last_sequence)

    async def test_duplicate_sequence_skipped(self):
        self.data_source._last_sequence[self.trading_pair] = 100

        msg_queue = asyncio.Queue()
        raw_message = {
            "jsonrpc": "2.0",
            "method": "updateOrderbook",
            "params": {
                "asks": [],
                "bids": [],
                "symbol": self.ex_trading_pair,
                "timestamp": 1772170410000,
                "sequence": 99,
            },
        }

        await self.data_source._parse_order_book_diff_message(raw_message, msg_queue)

        # Should be skipped (duplicate)
        self.assertTrue(msg_queue.empty())

    async def test_unsubscribe_sends_ws_messages(self):
        """Phase 5B: Unsubscribe should send proper WS unsubscribe messages."""
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws

        sent_messages = []
        async def capture_send(request):
            sent_messages.append(request.payload)
        mock_ws.send.side_effect = capture_send

        # Add the trading pair first so remove works
        self.data_source._trading_pairs = [self.trading_pair]

        success = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)

        self.assertTrue(success)
        self.assertEqual(2, len(sent_messages))
        # Check both unsubscribe messages were sent
        methods = [m["method"] for m in sent_messages]
        self.assertIn(CONSTANTS.WS_METHOD_UNSUBSCRIBE_TRADES, methods)
        self.assertIn(CONSTANTS.WS_METHOD_UNSUBSCRIBE_ORDERBOOK, methods)
        # Check symbol was included
        for msg in sent_messages:
            self.assertEqual(self.ex_trading_pair, msg["params"]["symbol"])

    async def test_parse_trade_message_processes_all_trades(self):
        """Phase 5B: Multiple trades in data array should all be queued."""
        msg_queue = asyncio.Queue()
        raw_message = {
            "jsonrpc": "2.0",
            "method": "snapshotTrades",
            "params": {
                "symbol": self.ex_trading_pair,
                "data": [
                    {
                        "id": "trade1",
                        "price": "67799.74",
                        "quantity": "0.020323",
                        "side": "sell",
                        "timestampms": 1772169881018,
                    },
                    {
                        "id": "trade2",
                        "price": "67800.00",
                        "quantity": "0.010000",
                        "side": "buy",
                        "timestampms": 1772169880000,
                    },
                    {
                        "id": "trade3",
                        "price": "67801.50",
                        "quantity": "0.005000",
                        "side": "sell",
                        "timestampms": 1772169879000,
                    },
                ],
            },
        }

        await self.data_source._parse_trade_message(raw_message, msg_queue)

        # All 3 trades should be in the queue, not just the first one
        self.assertEqual(3, msg_queue.qsize())
        trade_ids = []
        while not msg_queue.empty():
            msg = msg_queue.get_nowait()
            trade_ids.append(msg.content["trade_id"])
        self.assertEqual(["trade1", "trade2", "trade3"], trade_ids)

    # --- Issue 3: Resync robustness tests ---

    def _make_diff_message(self, sequence: int) -> dict:
        """Helper to create a diff message with a given sequence."""
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

    def _setup_snapshot_queue(self) -> asyncio.Queue:
        """Helper to create and register the snapshot message queue."""
        snapshot_queue = asyncio.Queue()
        self.data_source._message_queue[CONSTANTS.SNAPSHOT_EVENT_TYPE] = snapshot_queue
        return snapshot_queue

    async def test_ws_snapshot_clears_resync_pending(self):
        """When a WS snapshot arrives during resync, it should clear the pending flag."""
        self.data_source._resync_pending[self.trading_pair] = True
        self.data_source._resync_pending_since[self.trading_pair] = time.time()
        self.data_source._resync_failure_count[self.trading_pair] = 1

        msg_queue = asyncio.Queue()
        raw_snapshot = {
            "jsonrpc": "2.0",
            "method": "snapshotOrderbook",
            "params": {
                "symbol": self.ex_trading_pair,
                "sequence": 76500,
                "asks": [{"price": "100", "quantity": "1"}],
                "bids": [{"price": "99", "quantity": "1"}],
            },
        }
        await self.data_source._parse_order_book_snapshot_message(raw_snapshot, msg_queue)

        # Resync should be cleared
        self.assertFalse(self.data_source._resync_pending.get(self.trading_pair, False))
        # Sequence should be set from WS snapshot
        self.assertEqual(76500, self.data_source._last_sequence[self.trading_pair])
        # Snapshot should be in queue
        self.assertFalse(msg_queue.empty())

    async def test_ws_resubscribe_sends_unsub_then_resub(self):
        """Gap handler should send WS unsubscribe then resubscribe messages."""
        self.data_source._last_sequence[self.trading_pair] = 100
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws

        await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)

        # Should have sent 2 WS messages (unsub + resub)
        self.assertEqual(2, mock_ws.send.call_count)
        # Resync should be pending
        self.assertTrue(self.data_source._resync_pending.get(self.trading_pair, False))
        self.assertIn(self.trading_pair, self.data_source._resync_pending_since)

    async def test_duplicate_sequence_is_dropped(self):
        """Messages with sequence <= last seen should be silently dropped."""
        self.data_source._last_sequence[self.trading_pair] = 100
        queue = asyncio.Queue()

        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(99), queue
        )
        self.assertTrue(queue.empty())

    async def test_consecutive_sequence_is_accepted(self):
        """Messages with sequence == last + 1 should be accepted normally."""
        self.data_source._last_sequence[self.trading_pair] = 100
        queue = asyncio.Queue()

        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(101), queue
        )
        self.assertFalse(queue.empty())
        self.assertEqual(101, self.data_source._last_sequence[self.trading_pair])

    async def test_replay_rest_vs_ws_sequence_mismatch(self):
        """
        Replay the exact production failure pattern:
        - last WS diff seq = 3441
        - incoming diff seq = 3443 (gap of 1)
        - Should trigger WS resubscribe, NOT REST snapshot or WS disconnect
        """
        self.data_source._last_sequence[self.trading_pair] = 3441
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws

        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(3443), asyncio.Queue()
        )

        # WS resubscribe should have been sent (2 messages)
        self.assertEqual(2, mock_ws.send.call_count)
        # WS should NOT be disconnected
        mock_ws.disconnect.assert_not_called()
        # Resync pending, awaiting fresh WS snapshot
        self.assertTrue(self.data_source._resync_pending.get(self.trading_pair, False))

    async def test_ws_interruption_clears_sequence_state(self):
        """WebSocket interruption should clear sequence tracking and increment generation."""
        self.data_source._last_sequence[self.trading_pair] = 100
        gen_before = self.data_source._stream_generation

        await self.data_source._on_order_stream_interruption(None)

        self.assertEqual({}, self.data_source._last_sequence)
        self.assertEqual(gen_before + 1, self.data_source._stream_generation)

    async def test_listen_for_subscriptions_invokes_interruption_cleanup(self):
        """Integration test: base class listen_for_subscriptions() should invoke _on_order_stream_interruption."""
        self.data_source._last_sequence[self.trading_pair] = 100

        mock_ws = AsyncMock(spec=WSAssistant)
        mock_ws.disconnect = AsyncMock()

        with patch.object(self.data_source, "_connected_websocket_assistant",
                         new_callable=AsyncMock, return_value=mock_ws), \
             patch.object(self.data_source, "_subscribe_channels",
                         new_callable=AsyncMock), \
             patch.object(self.data_source, "_process_websocket_messages",
                         new_callable=AsyncMock, side_effect=Exception("Connection lost")):

            task = asyncio.create_task(
                self.data_source.listen_for_subscriptions()
            )
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertEqual({}, self.data_source._last_sequence)

    async def test_stream_generation_stable_on_ws_resubscribe(self):
        """Stream generation should NOT increment on successful WS resubscribe."""
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws

        self.assertEqual(0, self.data_source._stream_generation)

        # Gap triggers WS resubscribe (not WS disconnect)
        self.data_source._last_sequence[self.trading_pair] = 100
        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(105), asyncio.Queue()
        )
        # Generation should NOT increment — WS stays connected
        self.assertEqual(0, self.data_source._stream_generation)
        # WS should NOT be disconnected
        mock_ws.disconnect.assert_not_called()

    async def test_queue_drained_on_interruption(self):
        """Stale messages in diff and snapshot queues should be drained on stream interruption."""
        diff_queue = asyncio.Queue()
        snapshot_queue = asyncio.Queue()
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = diff_queue
        self.data_source._message_queue[CONSTANTS.SNAPSHOT_EVENT_TYPE] = snapshot_queue

        # Put some stale messages
        for i in range(5):
            diff_queue.put_nowait(f"stale_diff_{i}")
        for i in range(3):
            snapshot_queue.put_nowait(f"stale_snapshot_{i}")

        await self.data_source._on_order_stream_interruption(None)

        self.assertTrue(diff_queue.empty())
        self.assertTrue(snapshot_queue.empty())
        self.assertTrue(self.is_logged("INFO", "Drained 5 stale messages"))
        self.assertTrue(self.is_logged("INFO", "Drained 3 stale messages"))

    async def test_stale_diff_rejected_after_generation_change(self):
        """A diff message should be rejected if generation changes (WS resync failure path)."""
        self.data_source._last_sequence[self.trading_pair] = 100
        msg_queue = asyncio.Queue()

        mock_ws = AsyncMock(spec=WSAssistant)
        mock_ws.send = AsyncMock(side_effect=Exception("WS send failed"))
        self.data_source._ws_assistant = mock_ws

        # Force WS resync to fail enough times to trigger WS disconnect
        self.data_source._resync_failure_count[self.trading_pair] = 2  # at max-1

        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(105), msg_queue
        )

        # Queue should be empty — the diff after gap detection is not enqueued
        self.assertTrue(msg_queue.empty())
        # Generation was incremented (because resync failed 3 times)
        self.assertEqual(1, self.data_source._stream_generation)

    async def test_generation_tracked_across_interruptions(self):
        """Stream generation should monotonically increase across interruptions."""
        self.assertEqual(0, self.data_source._stream_generation)

        await self.data_source._on_order_stream_interruption(None)
        self.assertEqual(1, self.data_source._stream_generation)

        await self.data_source._on_order_stream_interruption(None)
        self.assertEqual(2, self.data_source._stream_generation)

        # Successful WS resubscribe does NOT increment generation
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws
        self.data_source._last_sequence[self.trading_pair] = 100
        await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)
        self.assertEqual(2, self.data_source._stream_generation)

    async def test_repeated_resync_failures_disconnect_ws(self):
        """After RESYNC_MAX_FAILURES failed WS resubscribes, WS should be disconnected."""
        mock_ws = AsyncMock(spec=WSAssistant)
        mock_ws.send = AsyncMock(side_effect=Exception("WS send failed"))
        self.data_source._ws_assistant = mock_ws

        # First failure (attempt 1/3) - should NOT disconnect WS
        self.data_source._last_sequence[self.trading_pair] = 100
        await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)
        mock_ws.disconnect.assert_not_called()
        self.assertEqual(0, self.data_source._stream_generation)
        self.assertEqual(1, self.data_source._resync_failure_count[self.trading_pair])

        # Second failure (attempt 2/3) - should NOT disconnect WS
        self.data_source._last_sequence[self.trading_pair] = 100
        await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)
        mock_ws.disconnect.assert_not_called()
        self.assertEqual(0, self.data_source._stream_generation)
        self.assertEqual(2, self.data_source._resync_failure_count[self.trading_pair])

        # Third failure (attempt 3/3) - SHOULD disconnect WS
        self.data_source._last_sequence[self.trading_pair] = 100
        await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)
        mock_ws.disconnect.assert_called_once()
        self.assertEqual(1, self.data_source._stream_generation)
        self.assertEqual(0, self.data_source._resync_failure_count[self.trading_pair])

    async def test_resync_pending_drops_diffs(self):
        """While a REST resync is pending, incoming diffs should be silently dropped."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_pending[self.trading_pair] = True
        self.data_source._resync_pending_since[self.trading_pair] = time.time()  # recent — no timeout
        msg_queue = asyncio.Queue()

        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(101), msg_queue
        )

        # Message should be dropped while resync is pending
        self.assertTrue(msg_queue.empty())

    async def test_interruption_clears_resync_state(self):
        """WS interruption should clear resync pending, pending_since, and failure count."""
        self.data_source._resync_pending[self.trading_pair] = True
        self.data_source._resync_pending_since[self.trading_pair] = time.time()
        self.data_source._resync_failure_count[self.trading_pair] = 2

        await self.data_source._on_order_stream_interruption(None)

        self.assertEqual({}, self.data_source._resync_pending)
        self.assertEqual({}, self.data_source._resync_pending_since)
        self.assertEqual({}, self.data_source._resync_failure_count)
