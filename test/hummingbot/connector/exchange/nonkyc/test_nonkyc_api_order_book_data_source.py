import asyncio
import json
import re
from typing import Awaitable, Optional
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses
from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS, nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


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
