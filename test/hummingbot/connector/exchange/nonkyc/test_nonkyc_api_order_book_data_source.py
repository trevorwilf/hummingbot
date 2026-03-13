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

    @aioresponses()
    async def test_sequence_gap_triggers_resync(self, mock_api):
        # Set up a known last sequence
        self.data_source._last_sequence[self.trading_pair] = 100

        # Mock the REST snapshot for resync
        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        snapshot_response = {
            "marketid": "643bfeeb5e07bba23a98a981",
            "symbol": self.ex_trading_pair,
            "timestamp": 1772169899391,
            "sequence": "110",
            "bids": [{"price": "67679.55", "quantity": "0.000422"}],
            "asks": [{"price": "67883.06", "quantity": "0.010917"}],
        }
        mock_api.get(regex_url, body=json.dumps(snapshot_response))

        # Create the snapshot queue
        snapshot_queue = asyncio.Queue()
        self.data_source._message_queue[CONSTANTS.SNAPSHOT_EVENT_TYPE] = snapshot_queue

        # Send a message with sequence gap (expected 101, got 105)
        msg_queue = asyncio.Queue()
        raw_message = {
            "jsonrpc": "2.0",
            "method": "updateOrderbook",
            "params": {
                "asks": [{"price": "67883.06", "quantity": "0.010917"}],
                "bids": [{"price": "67679.55", "quantity": "0.000422"}],
                "symbol": self.ex_trading_pair,
                "timestamp": 1772170410000,
                "sequence": 105,
            },
        }

        await self.data_source._parse_order_book_diff_message(raw_message, msg_queue)

        # The diff queue should be empty (gap means we resync, not process the diff)
        self.assertTrue(msg_queue.empty())
        # The snapshot queue should have a message
        self.assertFalse(snapshot_queue.empty())
        # Verify warning was logged
        self.assertTrue(self.is_logged("WARNING", "Orderbook sequence gap"))

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

    async def test_snapshot_502_does_not_crash_diff_loop(self):
        """A REST 502 during resync should not crash _parse_order_book_diff_message."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self._setup_snapshot_queue()

        with patch.object(
            self.data_source, "_request_order_book_snapshot",
            new_callable=AsyncMock, side_effect=OSError("HTTP status is 502")
        ):
            msg_queue = asyncio.Queue()
            # Trigger a gap (expected 101, got 105)
            await self.data_source._parse_order_book_diff_message(
                self._make_diff_message(105), msg_queue
            )

        # No exception propagated — diff queue is empty
        self.assertTrue(msg_queue.empty())
        # Warning about failed resync was logged
        self.assertTrue(self.is_logged("WARNING", "resync"))
        self.assertTrue(self.is_logged("WARNING", "failed"))
        # Failure count incremented
        self.assertEqual(1, self.data_source._resync_failure_count[self.trading_pair])
        # Next allowed time is in the future
        self.assertGreater(
            self.data_source._resync_next_allowed_time[self.trading_pair], time.time()
        )

    async def test_backoff_delay_increases_on_repeated_failures(self):
        """Exponential backoff: delay should grow with each consecutive failure."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self._setup_snapshot_queue()

        recorded_times = []

        with patch.object(
            self.data_source, "_request_order_book_snapshot",
            new_callable=AsyncMock, side_effect=OSError("HTTP status is 502")
        ):
            for i in range(3):
                # Reset backoff timer to allow immediate retry
                self.data_source._resync_next_allowed_time[self.trading_pair] = 0
                # Reset resync pending
                self.data_source._resync_pending[self.trading_pair] = False
                await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)
                recorded_times.append(
                    self.data_source._resync_next_allowed_time[self.trading_pair]
                )

        self.assertEqual(3, self.data_source._resync_failure_count[self.trading_pair])
        # Each successive next_allowed_time should be further in the future
        # (recorded at roughly the same wall time, so larger delay = larger value)
        self.assertGreater(recorded_times[1], recorded_times[0])
        self.assertGreater(recorded_times[2], recorded_times[1])

    async def test_backoff_respects_cooldown_period(self):
        """During backoff, no snapshot request should be made."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_failure_count[self.trading_pair] = 1
        # Set next allowed time far in the future
        self.data_source._resync_next_allowed_time[self.trading_pair] = time.time() + 1000
        self._setup_snapshot_queue()

        with patch.object(
            self.data_source, "_request_order_book_snapshot",
            new_callable=AsyncMock
        ) as mock_snapshot:
            msg_queue = asyncio.Queue()
            await self.data_source._parse_order_book_diff_message(
                self._make_diff_message(105), msg_queue
            )
            # Snapshot should NOT have been called
            mock_snapshot.assert_not_called()

        # Failure count should not have changed
        self.assertEqual(1, self.data_source._resync_failure_count[self.trading_pair])

    @aioresponses()
    async def test_successful_resync_resets_failure_state(self, mock_api):
        """After a successful resync, failure count and next_allowed should reset."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_failure_count[self.trading_pair] = 5
        self.data_source._resync_next_allowed_time[self.trading_pair] = 0
        snapshot_queue = self._setup_snapshot_queue()

        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        snapshot_response = {
            "marketid": "643bfeeb5e07bba23a98a981",
            "symbol": self.ex_trading_pair,
            "timestamp": 1772169899391,
            "sequence": "200",
            "bids": [{"price": "67679.55", "quantity": "0.000422"}],
            "asks": [{"price": "67883.06", "quantity": "0.010917"}],
        }
        mock_api.get(regex_url, body=json.dumps(snapshot_response))

        msg_queue = asyncio.Queue()
        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(105), msg_queue
        )

        # Failure state reset
        self.assertEqual(0, self.data_source._resync_failure_count[self.trading_pair])
        self.assertEqual(0, self.data_source._resync_next_allowed_time[self.trading_pair])
        # Snapshot was placed in the queue
        self.assertFalse(snapshot_queue.empty())
        msg = snapshot_queue.get_nowait()
        self.assertEqual(OrderBookMessageType.SNAPSHOT, msg.type)

    async def test_resync_pending_drops_diffs(self):
        """While resync is pending, diffs should be silently dropped."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_pending[self.trading_pair] = True

        msg_queue = asyncio.Queue()
        # Send a valid diff (sequence = 101, next expected)
        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(101), msg_queue
        )

        # Diff should have been dropped
        self.assertTrue(msg_queue.empty())
        # Sequence should NOT have been updated
        self.assertEqual(100, self.data_source._last_sequence[self.trading_pair])

    @aioresponses()
    async def test_resync_pending_cleared_on_success(self, mock_api):
        """After successful resync, pending flag is cleared and diffs process normally."""
        self.data_source._last_sequence[self.trading_pair] = 100
        snapshot_queue = self._setup_snapshot_queue()

        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        snapshot_response = {
            "marketid": "643bfeeb5e07bba23a98a981",
            "symbol": self.ex_trading_pair,
            "timestamp": 1772169899391,
            "sequence": "200",
            "bids": [{"price": "67679.55", "quantity": "0.000422"}],
            "asks": [{"price": "67883.06", "quantity": "0.010917"}],
        }
        mock_api.get(regex_url, body=json.dumps(snapshot_response))

        msg_queue = asyncio.Queue()
        # Trigger a gap to start resync
        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(105), msg_queue
        )

        # Pending should be cleared
        self.assertFalse(self.data_source._resync_pending.get(self.trading_pair, False))

        # Now send a diff with next expected sequence after snapshot (201)
        await self.data_source._parse_order_book_diff_message(
            self._make_diff_message(201), msg_queue
        )
        # Diff should be processed normally
        self.assertFalse(msg_queue.empty())

    async def test_resync_pending_cleared_on_failure(self):
        """After a failed resync, pending flag should be cleared so diffs can flow."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self._setup_snapshot_queue()

        with patch.object(
            self.data_source, "_request_order_book_snapshot",
            new_callable=AsyncMock, side_effect=OSError("HTTP status is 502")
        ):
            await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)

        # Pending should be cleared even after failure
        self.assertFalse(self.data_source._resync_pending.get(self.trading_pair, False))

    async def test_max_failures_triggers_reconnect(self):
        """After SNAPSHOT_RESYNC_MAX_FAILURES, CancelledError should be raised."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_failure_count[self.trading_pair] = (
            NonkycAPIOrderBookDataSource.SNAPSHOT_RESYNC_MAX_FAILURES
        )
        self.data_source._resync_next_allowed_time[self.trading_pair] = 0
        self._setup_snapshot_queue()

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)

        # Failure count was reset
        self.assertEqual(0, self.data_source._resync_failure_count[self.trading_pair])
        # Warning was logged
        self.assertTrue(self.is_logged("WARNING", "Triggering WebSocket reconnect"))

    async def test_ws_interruption_clears_all_resync_state(self):
        """WebSocket interruption should clear all resync tracking dicts."""
        # Set various resync state
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_pending[self.trading_pair] = True
        self.data_source._resync_failure_count[self.trading_pair] = 5
        self.data_source._resync_next_allowed_time[self.trading_pair] = time.time() + 1000

        await self.data_source._on_order_book_ws_interruption(None)

        self.assertEqual({}, self.data_source._last_sequence)
        self.assertEqual({}, self.data_source._resync_pending)
        self.assertEqual({}, self.data_source._resync_failure_count)
        self.assertEqual({}, self.data_source._resync_next_allowed_time)

    async def test_backoff_delay_capped_at_max(self):
        """Backoff delay should never exceed SNAPSHOT_RESYNC_MAX_DELAY."""
        self.data_source._last_sequence[self.trading_pair] = 100
        self.data_source._resync_failure_count[self.trading_pair] = 100  # absurdly high
        self.data_source._resync_next_allowed_time[self.trading_pair] = 0
        self._setup_snapshot_queue()

        now = time.time()
        with patch.object(
            self.data_source, "_request_order_book_snapshot",
            new_callable=AsyncMock, side_effect=OSError("HTTP status is 502")
        ):
            # Reset failure count below max to avoid CancelledError
            self.data_source._resync_failure_count[self.trading_pair] = 5
            await self.data_source._handle_sequence_gap(self.trading_pair, 100, 105)

        next_allowed = self.data_source._resync_next_allowed_time[self.trading_pair]
        max_delay = NonkycAPIOrderBookDataSource.SNAPSHOT_RESYNC_MAX_DELAY
        # next_allowed should be at most now + max_delay + 25% jitter
        self.assertLessEqual(next_allowed, now + max_delay * 1.25 + 1.0)  # +1s tolerance
        self.assertGreater(next_allowed, now)
