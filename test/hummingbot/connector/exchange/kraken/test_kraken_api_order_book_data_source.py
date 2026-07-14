import asyncio
import json
import re
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses
from bidict import bidict

from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS, kraken_web_utils as web_utils
from hummingbot.connector.exchange.kraken.kraken_api_order_book_data_source import KrakenAPIOrderBookDataSource
from hummingbot.connector.exchange.kraken.kraken_constants import KrakenAPITier
from hummingbot.connector.exchange.kraken.kraken_exchange import KrakenExchange
from hummingbot.connector.exchange.kraken.kraken_utils import build_rate_limits_by_tier
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.order_book import OrderBook, OrderBookMessage


class KrakenAPIOrderBookDataSourceTest(IsolatedAsyncioWrapperTestCase):
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = cls.base_asset + cls.quote_asset
        cls.ws_ex_trading_pairs = cls.base_asset + "/" + cls.quote_asset
        cls.api_tier = KrakenAPITier.STARTER

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.log_records = []
        self.listening_task = None
        self.mocking_assistant = NetworkMockingAssistant(self.local_event_loop)

        self.throttler = AsyncThrottler(build_rate_limits_by_tier(self.api_tier))
        self.connector = KrakenExchange(
            kraken_api_key="",
            kraken_secret_key="",
            trading_pairs=[],
            trading_required=False)
        self.data_source = KrakenAPIOrderBookDataSource(
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory,
            trading_pairs=[self.trading_pair])

        self._original_full_order_book_reset_time = self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = -1
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)

        self.resume_test_event = asyncio.Event()

        self.connector._set_trading_pair_symbol_map(bidict({self.ex_trading_pair: self.trading_pair}))

    def tearDown(self) -> None:
        self.listening_task and self.listening_task.cancel()
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = self._original_full_order_book_reset_time
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message
                   for record in self.log_records)

    def _create_exception_and_unlock_test_with_event(self, exception):
        self.resume_test_event.set()
        raise exception

    def _trade_update_event(self):
        resp = [
            0,
            [
                [
                    "5541.20000",
                    "0.15850568",
                    "1534614057.321597",
                    "s",
                    "l",
                    ""
                ]
            ],
            "trade",
            f"{self.base_asset}/{self.quote_asset}"
        ]
        return resp

    def _order_diff_event(self):
        resp = [
            1234,
            {
                "a": [
                    [
                        "5541.30000",
                        "2.50700000",
                        "1534614248.456738"
                    ],
                    [
                        "5542.50000",
                        "0.40100000",
                        "1534614248.456738"
                    ]
                ],
                "c": "974942666"
            },
            "book-10",
            "XBT/USD"
        ]
        return resp

    def _snapshot_response(self):
        resp = {
            "error": [],
            "result": {
                f"X{self.base_asset}{self.quote_asset}": {
                    "asks": [
                        [
                            "52523.00000",
                            "1.199",
                            1616663113
                        ],
                        [
                            "52536.00000",
                            "0.300",
                            1616663112
                        ]
                    ],
                    "bids": [
                        [
                            "52522.90000",
                            "0.753",
                            1616663112
                        ],
                        [
                            "52522.80000",
                            "0.006",
                            1616663109
                        ]
                    ]
                }
            }
        }
        return resp

    @aioresponses()
    async def test_get_new_order_book_successful(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL)
        regex_url = re.compile(f"^{url}?pair={self.ex_trading_pair}".replace(".", r"\.").replace("?", r"\?"))

        resp = self._snapshot_response()

        mock_api.get(regex_url, body=json.dumps(resp))

        ret = await self.data_source.get_new_order_book(self.trading_pair)

        self.assertTrue(isinstance(ret, OrderBook))

        bids_df, asks_df = ret.snapshot
        pair_data = resp["result"][f"X{self.base_asset}{self.quote_asset}"]
        first_bid_price = float(pair_data["bids"][0][0])
        first_ask_price = float(pair_data["asks"][0][0])

        self.assertEqual(first_bid_price, bids_df.iloc[0]["price"])
        self.assertEqual(first_ask_price, asks_df.iloc[0]["price"])

    @aioresponses()
    async def test_get_new_order_book_raises_exception(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL)
        regex_url = re.compile(f"^{url}?pair={self.ex_trading_pair}".replace(".", r"\.").replace("?", r"\?"))

        mock_api.get(regex_url, status=400)
        with self.assertRaises(IOError):
            await self.data_source.get_new_order_book(self.trading_pair)

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    async def test_listen_for_subscriptions_subscribes_to_trades_and_order_diffs(self, ws_connect_mock):
        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()

        result_subscribe_trades = {
            "code": None,
            "id": 1
        }
        result_subscribe_diffs = {
            "code": None,
            "id": 2
        }

        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message=json.dumps(result_subscribe_trades))
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message=json.dumps(result_subscribe_diffs))

        self.listening_task = self.local_event_loop.create_task(self.data_source.listen_for_subscriptions())

        await self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)

        sent_subscription_messages = self.mocking_assistant.json_messages_sent_through_websocket(
            websocket_mock=ws_connect_mock.return_value)

        self.assertEqual(2, len(sent_subscription_messages))
        expected_trade_subscription = {
            "event": "subscribe",
            "pair": [self.ws_ex_trading_pairs],
            "subscription": {"name": 'trade'},
        }
        self.assertEqual(expected_trade_subscription, sent_subscription_messages[0])
        expected_diff_subscription = {
            "event": "subscribe",
            "pair": [self.ws_ex_trading_pairs],
            "subscription": {"name": 'book', "depth": 1000},
        }
        self.assertEqual(expected_diff_subscription, sent_subscription_messages[1])

        self.assertTrue(self._is_logged(
            "INFO",
            "Subscribed to public order book and trade channels..."
        ))

    @patch("hummingbot.core.data_type.order_book_tracker_data_source.OrderBookTrackerDataSource._sleep")
    @patch("aiohttp.ClientSession.ws_connect")
    async def test_listen_for_subscriptions_raises_cancel_exception(self, mock_ws, _: AsyncMock):
        mock_ws.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_subscriptions()

    @patch("hummingbot.core.data_type.order_book_tracker_data_source.OrderBookTrackerDataSource._sleep")
    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    async def test_listen_for_subscriptions_logs_exception_details(self, mock_ws, sleep_mock):
        mock_ws.side_effect = Exception("TEST ERROR.")
        sleep_mock.side_effect = lambda _: self._create_exception_and_unlock_test_with_event(asyncio.CancelledError())

        self.listening_task = self.local_event_loop.create_task(self.data_source.listen_for_subscriptions())

        await self.resume_test_event.wait()

        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error occurred when listening to order book streams. Retrying in 5 seconds..."))

    async def test_subscribe_channels_raises_cancel_exception(self):
        mock_ws = MagicMock()
        mock_ws.send.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source._subscribe_channels(mock_ws)

    async def test_subscribe_channels_raises_exception_and_logs_error(self):
        mock_ws = MagicMock()
        mock_ws.send.side_effect = Exception("Test Error")

        with self.assertRaises(Exception):
            await self.data_source._subscribe_channels(mock_ws)

        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error occurred subscribing to order book data streams.")
        )

    async def test_listen_for_trades_cancelled_when_listening(self):
        mock_queue = MagicMock()
        mock_queue.get.side_effect = asyncio.CancelledError()
        self.data_source._message_queue[self.data_source._trade_messages_queue_key] = mock_queue

        msg_queue: asyncio.Queue = asyncio.Queue()

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_trades(self.local_event_loop, msg_queue)

    async def test_listen_for_trades_logs_exception(self):
        incomplete_resp = {
            "m": 1,
            "i": 2,
        }

        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [incomplete_resp, asyncio.CancelledError()]
        self.data_source._message_queue[self.data_source._trade_messages_queue_key] = mock_queue

        msg_queue: asyncio.Queue = asyncio.Queue()

        try:
            await self.data_source.listen_for_trades(self.local_event_loop, msg_queue)
        except asyncio.CancelledError:
            pass

        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error when processing public trade updates from exchange"))

    async def test_listen_for_trades_successful(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [self._trade_update_event(), asyncio.CancelledError()]
        self.data_source._message_queue[self.data_source._trade_messages_queue_key] = mock_queue

        msg_queue: asyncio.Queue = asyncio.Queue()

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_trades(self.local_event_loop, msg_queue))

        msg: OrderBookMessage = await msg_queue.get()

        self.assertEqual(1534614057.321597, msg.trade_id)

    async def test_listen_for_order_book_diffs_cancelled(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = asyncio.CancelledError()
        self.data_source._message_queue[self.data_source._diff_messages_queue_key] = mock_queue

        msg_queue: asyncio.Queue = asyncio.Queue()

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue)

    async def test_listen_for_order_book_diffs_logs_exception(self):
        incomplete_resp = {
            "m": 1,
            "i": 2,
        }

        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [incomplete_resp, asyncio.CancelledError()]
        self.data_source._message_queue[self.data_source._diff_messages_queue_key] = mock_queue

        msg_queue: asyncio.Queue = asyncio.Queue()

        try:
            await self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue)
        except asyncio.CancelledError:
            pass

        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error when processing public order book updates from exchange"))

    async def test_listen_for_order_book_diffs_successful(self):
        mock_queue = AsyncMock()
        diff_event = self._order_diff_event()
        mock_queue.get.side_effect = [diff_event, asyncio.CancelledError()]
        self.data_source._message_queue[self.data_source._diff_messages_queue_key] = mock_queue

        msg_queue: asyncio.Queue = asyncio.Queue()

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue))

        msg: OrderBookMessage = await msg_queue.get()

        self.assertEqual(diff_event[1]["a"][0][2], str(msg.update_id))

    async def test_parse_order_book_diff_message_update_id_is_monotonic(self):
        # OB-1: update_id derives from per-level timestamps, which can move backwards across messages;
        # the data source must clamp it to a monotonic non-decreasing value.
        queue: asyncio.Queue = asyncio.Queue()
        first = [1234, {"a": [["5541.30000", "2.50700000", "1534614248.456738"]], "c": "1"}, "book-10", "XBT/USD"]
        second = [1234, {"a": [["5541.30000", "2.50700000", "1534614200.000000"]], "c": "2"}, "book-10", "XBT/USD"]

        await self.data_source._parse_order_book_diff_message(first, queue)
        await self.data_source._parse_order_book_diff_message(second, queue)

        msg1 = await queue.get()
        msg2 = await queue.get()
        self.assertEqual(1534614248.456738, msg1.update_id)
        # The older-timestamped second message must not regress the update_id.
        self.assertEqual(msg1.update_id, msg2.update_id)

    @aioresponses()
    async def test_listen_for_order_book_snapshots_cancelled_when_fetching_snapshot(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL)
        regex_url = re.compile(f"^{url}?pair={self.ex_trading_pair}".replace(".", r"\.").replace("?", r"\?"))

        mock_api.get(regex_url, exception=asyncio.CancelledError, repeat=True)

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_order_book_snapshots(self.local_event_loop, asyncio.Queue())

    @aioresponses()
    @patch("hummingbot.connector.exchange.kraken.kraken_api_order_book_data_source"
           ".KrakenAPIOrderBookDataSource._sleep")
    async def test_listen_for_order_book_snapshots_log_exception(self, mock_api, sleep_mock):
        msg_queue: asyncio.Queue = asyncio.Queue()
        sleep_mock.side_effect = lambda _: self._create_exception_and_unlock_test_with_event(asyncio.CancelledError())

        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL)
        regex_url = re.compile(f"^{url}?pair={self.ex_trading_pair}".replace(".", r"\.").replace("?", r"\?"))

        mock_api.get(regex_url, exception=Exception, repeat=True)

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_snapshots(self.local_event_loop, msg_queue)
        )
        await self.resume_test_event.wait()

        self.assertTrue(
            self._is_logged("ERROR", f"Unexpected error fetching order book snapshot for {self.trading_pair}."))

    @aioresponses()
    async def test_listen_for_order_book_snapshots_successful(self, mock_api, ):
        msg_queue: asyncio.Queue = asyncio.Queue()
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL)
        regex_url = re.compile(f"^{url}?pair={self.ex_trading_pair}".replace(".", r"\.").replace("?", r"\?"))

        mock_api.get(regex_url, body=json.dumps(self._snapshot_response()))

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_snapshots(self.local_event_loop, msg_queue)
        )

        msg: OrderBookMessage = await msg_queue.get()

        self.assertEqual(1616663113, msg.update_id)

    # Dynamic subscription tests
    async def test_subscribe_to_trading_pair_successful(self):
        """Test successful subscription to a new trading pair."""
        mock_ws = AsyncMock()
        self.data_source._ws_assistant = mock_ws

        result = await self.data_source.subscribe_to_trading_pair(self.trading_pair)

        self.assertTrue(result)
        self.assertIn(self.trading_pair, self.data_source._trading_pairs)
        self.assertEqual(2, mock_ws.send.call_count)  # 2 channels: orderbook, trades
        self.assertTrue(
            self._is_logged("INFO", f"Subscribed to {self.trading_pair} order book and trade channels")
        )

    async def test_subscribe_to_trading_pair_websocket_not_connected(self):
        """Test subscription when websocket is not connected."""
        new_pair = "ETH-USDT"
        self.data_source._ws_assistant = None

        result = await self.data_source.subscribe_to_trading_pair(new_pair)

        self.assertFalse(result)
        self.assertTrue(
            self._is_logged("WARNING", f"Cannot subscribe to {new_pair}: WebSocket not connected")
        )

    async def test_subscribe_to_trading_pair_raises_cancel_exception(self):
        """Test that CancelledError is properly propagated."""
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = asyncio.CancelledError
        self.data_source._ws_assistant = mock_ws

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.subscribe_to_trading_pair(self.trading_pair)

    async def test_subscribe_to_trading_pair_raises_exception_and_logs_error(self):
        """Test that other exceptions are caught and logged."""
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = Exception("Test Error")
        self.data_source._ws_assistant = mock_ws

        result = await self.data_source.subscribe_to_trading_pair(self.trading_pair)

        self.assertFalse(result)
        self.assertTrue(
            self._is_logged("ERROR", f"Error subscribing to {self.trading_pair}")
        )

    async def test_unsubscribe_from_trading_pair_successful(self):
        """Test successful unsubscription from a trading pair."""
        mock_ws = AsyncMock()
        self.data_source._ws_assistant = mock_ws

        result = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)

        self.assertTrue(result)
        self.assertNotIn(self.trading_pair, self.data_source._trading_pairs)
        self.assertEqual(2, mock_ws.send.call_count)  # 2 channels: orderbook, trades
        self.assertTrue(
            self._is_logged("INFO", f"Unsubscribed from {self.trading_pair} order book and trade channels")
        )

    async def test_unsubscribe_from_trading_pair_websocket_not_connected(self):
        """Test unsubscription when websocket is not connected."""
        self.data_source._ws_assistant = None

        result = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)

        self.assertFalse(result)
        self.assertTrue(
            self._is_logged("WARNING", f"Cannot unsubscribe from {self.trading_pair}: WebSocket not connected")
        )

    async def test_unsubscribe_from_trading_pair_raises_cancel_exception(self):
        """Test that CancelledError is properly propagated during unsubscription."""
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = asyncio.CancelledError
        self.data_source._ws_assistant = mock_ws

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)

    async def test_unsubscribe_from_trading_pair_raises_exception_and_logs_error(self):
        """Test that other exceptions are caught and logged during unsubscription."""
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = Exception("Test Error")
        self.data_source._ws_assistant = mock_ws

        result = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)

        self.assertFalse(result)
        self.assertTrue(
            self._is_logged("ERROR", f"Error unsubscribing from {self.trading_pair}")
        )

    # ------------------------------------------------------------------
    # CSF-V1 Phase 2: KRK-1 dual-dict diffs + KRK-12 checksum validation
    # ------------------------------------------------------------------

    # 10x10 book fixture using the feed's own string formats. The expected CRC32 values below were
    # computed offline with zlib over the live-verified transformation (decimal point removed,
    # leading zeros stripped, top-10 asks ascending then top-10 bids descending).
    CHECKSUM_ASKS_10 = [
        ["100.00000", "1.00000000", "1534614200.000000"],
        ["100.10000", "2.00000000", "1534614200.000000"],
        ["100.20000", "3.00000000", "1534614200.000000"],
        ["100.30000", "4.00000000", "1534614200.000000"],
        ["100.40000", "5.00000000", "1534614200.000000"],
        ["100.50000", "6.00000000", "1534614200.000000"],
        ["100.60000", "7.00000000", "1534614200.000000"],
        ["100.70000", "8.00000000", "1534614200.000000"],
        ["100.80000", "9.00000000", "1534614200.000000"],
        ["100.90000", "10.00000000", "1534614200.000000"],
    ]
    CHECKSUM_BIDS_10 = [
        ["99.90000", "2.00000000", "1534614200.000000"],
        ["99.80000", "3.00000000", "1534614200.000000"],
        ["99.70000", "4.00000000", "1534614200.000000"],
        ["99.60000", "5.00000000", "1534614200.000000"],
        ["99.50000", "6.00000000", "1534614200.000000"],
        ["99.40000", "7.00000000", "1534614200.000000"],
        ["99.30000", "8.00000000", "1534614200.000000"],
        ["99.20000", "9.00000000", "1534614200.000000"],
        ["99.10000", "10.00000000", "1534614200.000000"],
        ["99.00000", "11.00000000", "1534614200.000000"],
    ]
    # Checksum of the untouched 10x10 snapshot book above.
    CHECKSUM_SNAPSHOT_EXPECTED = 945700655
    # Checksum after the consistent diff below (best-ask volume replaced, new best bid inserted,
    # which pushes the 99.00000 bid out of the hashed top 10).
    CHECKSUM_AFTER_DIFF_EXPECTED = 2977597905

    def _ws_snapshot_event_10x10(self):
        return [
            1234,
            {"as": [list(level) for level in self.CHECKSUM_ASKS_10],
             "bs": [list(level) for level in self.CHECKSUM_BIDS_10]},
            "book-1000",
            f"{self.base_asset}/{self.quote_asset}",
        ]

    def _consistent_diff_event(self, checksum: str = None):
        return [
            1234,
            {"a": [["100.00000", "5.00000000", "1534614250.000000"]]},
            {"b": [["99.95000", "0.50000000", "1534614250.100000"]],
             "c": checksum if checksum is not None else str(self.CHECKSUM_AFTER_DIFF_EXPECTED)},
            "book-1000",
            f"{self.base_asset}/{self.quote_asset}",
        ]

    async def test_parse_order_book_diff_message_merges_dual_dict_payload(self):
        # KRK-1 (would-have-caught): live-observed dual-dict diffs [ch, {"a": ...}, {"b": ..., "c":
        # ...}, "book-10", pair] carry asks and bids in SEPARATE payload dicts; reading only
        # raw_message[1] silently dropped the bid side.
        queue: asyncio.Queue = asyncio.Queue()
        raw = [
            1234,
            {"a": [["5541.30000", "2.50700000", "1534614248.456738"]]},
            {"b": [["5541.20000", "1.00000000", "1534614248.456739"]], "c": "974942666"},
            "book-10",
            f"{self.base_asset}/{self.quote_asset}",
        ]

        await self.data_source._parse_order_book_diff_message(raw, queue)

        msg: OrderBookMessage = await queue.get()
        self.assertEqual(1, len(msg.asks))
        self.assertEqual(1, len(msg.bids))
        self.assertEqual(5541.3, msg.asks[0].price)
        self.assertEqual(5541.2, msg.bids[0].price)
        # update_id must derive from the MERGED level set (the bid carries the newest timestamp).
        self.assertEqual(1534614248.456739, msg.update_id)

    def test_checksum_field_matches_live_examples(self):
        # Live-verified 2026-07-14 examples of the checksum string transformation.
        self.assertEqual("6448790000", self.data_source._checksum_field("64487.90000"))
        self.assertEqual("420000", self.data_source._checksum_field("0.00420000"))

    async def test_checksum_match_keeps_connection_and_resets_counter(self):
        queue: asyncio.Queue = asyncio.Queue()
        ws_mock = AsyncMock()
        self.data_source._ws_assistant = ws_mock

        await self.data_source._parse_order_book_diff_message(self._ws_snapshot_event_10x10(), queue)
        book = self.data_source._checksum_books[self.trading_pair]
        self.assertEqual(self.CHECKSUM_SNAPSHOT_EXPECTED, self.data_source._compute_book_checksum(book))

        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(), queue)

        ws_mock.disconnect.assert_not_awaited()
        self.assertEqual(0, self.data_source._checksum_mismatch_counts.get(self.trading_pair, 0))

    async def test_single_checksum_mismatch_does_not_disconnect(self):
        # KRK-12 false-positive guard: a single mismatch (warm-up/edge transient in the live
        # capture) must NOT tear the connection down.
        queue: asyncio.Queue = asyncio.Queue()
        ws_mock = AsyncMock()
        self.data_source._ws_assistant = ws_mock

        await self.data_source._parse_order_book_diff_message(self._ws_snapshot_event_10x10(), queue)
        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(checksum="1"), queue)

        ws_mock.disconnect.assert_not_awaited()
        self.assertEqual(1, self.data_source._checksum_mismatch_counts[self.trading_pair])

    async def test_two_consecutive_checksum_mismatches_disconnect(self):
        # KRK-12: confirmed drift (2 consecutive mismatches) forces a clean WS reconnect and resets
        # the local replica (mirrors the NonKYC gap-disconnect pattern; NOT a REST resync).
        queue: asyncio.Queue = asyncio.Queue()
        ws_mock = AsyncMock()
        self.data_source._ws_assistant = ws_mock

        await self.data_source._parse_order_book_diff_message(self._ws_snapshot_event_10x10(), queue)
        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(checksum="1"), queue)
        ws_mock.disconnect.assert_not_awaited()
        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(checksum="1"), queue)

        ws_mock.disconnect.assert_awaited_once()
        self.assertNotIn(self.trading_pair, self.data_source._checksum_books)
        self.assertEqual(0, self.data_source._checksum_mismatch_counts.get(self.trading_pair, 0))

    async def test_checksum_match_between_mismatches_resets_counter(self):
        # Non-consecutive mismatches must never accumulate into a disconnect.
        queue: asyncio.Queue = asyncio.Queue()
        ws_mock = AsyncMock()
        self.data_source._ws_assistant = ws_mock

        await self.data_source._parse_order_book_diff_message(self._ws_snapshot_event_10x10(), queue)
        # Mismatch, then a matching diff, then another mismatch: counter never reaches 2.
        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(checksum="1"), queue)
        self.assertEqual(1, self.data_source._checksum_mismatch_counts[self.trading_pair])
        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(), queue)
        self.assertEqual(0, self.data_source._checksum_mismatch_counts.get(self.trading_pair, 0))
        await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(checksum="1"), queue)

        ws_mock.disconnect.assert_not_awaited()

    async def test_checksum_validation_skipped_during_warmup(self):
        # KRK-12 warm-up guard: no validation (and no disconnect) until the local book holds a full
        # 10x10 — the live capture's false mismatches all occurred on partially built books.
        queue: asyncio.Queue = asyncio.Queue()
        ws_mock = AsyncMock()
        self.data_source._ws_assistant = ws_mock

        shallow_snapshot = [
            1234,
            {"as": [list(level) for level in self.CHECKSUM_ASKS_10[:5]],
             "bs": [list(level) for level in self.CHECKSUM_BIDS_10[:5]]},
            "book-1000",
            f"{self.base_asset}/{self.quote_asset}",
        ]
        await self.data_source._parse_order_book_diff_message(shallow_snapshot, queue)
        for _ in range(3):
            await self.data_source._parse_order_book_diff_message(self._consistent_diff_event(checksum="1"), queue)

        ws_mock.disconnect.assert_not_awaited()
        self.assertEqual(0, self.data_source._checksum_mismatch_counts.get(self.trading_pair, 0))

    async def test_checksum_book_applies_deletions(self):
        # Volume 0 deletes the level from the checksum replica.
        queue: asyncio.Queue = asyncio.Queue()
        await self.data_source._parse_order_book_diff_message(self._ws_snapshot_event_10x10(), queue)

        delete_diff = [
            1234,
            {"a": [["100.00000", "0.00000000", "1534614251.000000"]]},
            "book-1000",
            f"{self.base_asset}/{self.quote_asset}",
        ]
        await self.data_source._parse_order_book_diff_message(delete_diff, queue)

        book = self.data_source._checksum_books[self.trading_pair]
        self.assertNotIn(Decimal("100.00000"), book["asks"])
        self.assertEqual(9, len(book["asks"]))

    async def test_checksum_state_cleared_on_stream_interruption(self):
        queue: asyncio.Queue = asyncio.Queue()
        await self.data_source._parse_order_book_diff_message(self._ws_snapshot_event_10x10(), queue)
        self.data_source._checksum_mismatch_counts[self.trading_pair] = 1

        await self.data_source._on_order_stream_interruption(websocket_assistant=None)

        self.assertEqual({}, self.data_source._checksum_books)
        self.assertEqual(0, len(self.data_source._checksum_mismatch_counts))
