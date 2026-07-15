import asyncio
import json
import re
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from typing import Awaitable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses
from bidict import bidict

from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS
from hummingbot.connector.exchange.kraken.kraken_api_user_stream_data_source import KrakenAPIUserStreamDataSource
from hummingbot.connector.exchange.kraken.kraken_auth import KrakenAuth
from hummingbot.connector.exchange.kraken.kraken_constants import KrakenAPITier
from hummingbot.connector.exchange.kraken.kraken_exchange import KrakenExchange
from hummingbot.connector.exchange.kraken.kraken_utils import build_rate_limits_by_tier
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler


class KrakenAPIUserStreamDataSourceTest(IsolatedAsyncioWrapperTestCase):
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}{cls.quote_asset}"
        cls.ws_ex_trading_pair = cls.base_asset + "/" + cls.quote_asset
        cls.api_tier = KrakenAPITier.STARTER

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []
        self.listening_task: Optional[asyncio.Task] = None

        self.mock_time_provider = MagicMock()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.mocking_assistant = NetworkMockingAssistant()
        self.throttler = AsyncThrottler(build_rate_limits_by_tier(self.api_tier))

        self.connector = KrakenExchange(
            kraken_api_key="",
            kraken_secret_key="",
            trading_pairs=[self.trading_pair],
            trading_required=False)

        not_a_real_secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
        self.auth = KrakenAuth(api_key="someKey", secret_key=not_a_real_secret, time_provider=self.mock_time_provider)

        self.connector._web_assistants_factory._auth = self.auth
        self.data_source = KrakenAPIUserStreamDataSource(self.connector,
                                                         api_factory=self.connector._web_assistants_factory,
                                                         )

        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)

        self.resume_test_event = asyncio.Event()

        self.connector._set_trading_pair_symbol_map(bidict({self.ex_trading_pair: self.trading_pair}))

    def tearDown(self) -> None:
        self.listening_task and self.listening_task.cancel()
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message
                   for record in self.log_records)

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    @staticmethod
    def get_auth_response_mock() -> Dict:
        auth_resp = {
            "error": [],
            "result": {
                "token": "1Dwc4lzSwNWOAwkMdqhssNNFhs1ed606d1WcF3XfEMw",
                "expires": 900
            }
        }
        return auth_resp

    @staticmethod
    def get_open_orders_mock() -> List:
        open_orders = [
            [
                {
                    "OGTT3Y-C6I3P-XRI6HX": {
                        "status": "closed"
                    }
                },
                {
                    "OGTT3Y-C6I3P-XRI6HX": {
                        "status": "closed"
                    }
                }
            ],
            "openOrders",
            {
                "sequence": 59342
            }
        ]
        return open_orders

    @staticmethod
    def get_own_trades_mock() -> List:
        own_trades = [
            [
                {
                    "TDLH43-DVQXD-2KHVYY": {
                        "cost": "1000000.00000",
                        "fee": "1600.00000",
                        "margin": "0.00000",
                        "ordertxid": "TDLH43-DVQXD-2KHVYY",
                        "ordertype": "limit",
                        "pair": "XBT/EUR",
                        "postxid": "OGTT3Y-C6I3P-XRI6HX",
                        "price": "100000.00000",
                        "time": "1560516023.070651",
                        "type": "sell",
                        "vol": "1000000000.00000000"
                    }
                },
            ],
            "ownTrades",
            {
                "sequence": 2948
            }
        ]
        return own_trades

    @aioresponses()
    async def test_get_auth_token(self, mocked_api):
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.GET_TOKEN_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        resp = self.get_auth_response_mock()
        mocked_api.post(regex_url, body=json.dumps(resp))

        ret = await (self.data_source.get_auth_token())

        self.assertEqual(ret, resp["result"]["token"])

    @aioresponses()
    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    async def test_listen_for_user_stream(self, mocked_api, ws_connect_mock):
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.GET_TOKEN_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        resp = self.get_auth_response_mock()
        mocked_api.post(regex_url, body=json.dumps(resp))
        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()
        output_queue = asyncio.Queue()
        asyncio.create_task(self.data_source.listen_for_user_stream(output_queue))

        resp = self.get_open_orders_mock()
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value, message=json.dumps(resp)
        )
        ret = await output_queue.get()

        self.assertEqual(ret, resp)

        resp = self.get_own_trades_mock()
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value, message=json.dumps(resp)
        )
        ret = await (output_queue.get())

        self.assertEqual(ret, resp)

    async def test_subscribe_channels_refreshes_token_each_call(self):
        # USERSTREAM-1: a fresh WS token is minted on every (re)subscribe, never reused after a reconnect.
        ws = AsyncMock()
        with patch.object(self.data_source, "get_auth_token",
                          new=AsyncMock(side_effect=["token-1", "token-2"])) as mock_get_token:
            await self.data_source._subscribe_channels(ws)
            first_token = self.data_source._current_auth_token
            await self.data_source._subscribe_channels(ws)
            second_token = self.data_source._current_auth_token

        self.assertEqual(2, mock_get_token.call_count)
        self.assertEqual("token-1", first_token)
        self.assertEqual("token-2", second_token)

    async def test_process_event_message_ignores_short_and_malformed_frames(self):
        # USERSTREAM-5: a short/malformed list must not raise IndexError, and a list must not be probed
        # for errorMessage (AttributeError); only dict frames carry errorMessage.
        queue = asyncio.Queue()

        await self.data_source._process_event_message(["only-one"], queue)
        self.assertTrue(queue.empty())

        own = self.get_own_trades_mock()
        await self.data_source._process_event_message(own, queue)
        self.assertEqual(own, await queue.get())

        with self.assertRaises(IOError):
            await self.data_source._process_event_message({"errorMessage": "boom"}, queue)

    # ------------------------------------------------------------------
    # CSF-V1 Phase 2: KRK-12 private-channel sequence validation
    # ------------------------------------------------------------------

    async def test_sequence_gap_raises_and_resets_tracking(self):
        # KRK-12 (would-have-caught): a sequence gap on ownTrades/openOrders means missed
        # fills/status transitions on a connection that still looks healthy. It must raise so the
        # reconnect + subscription snapshot replay recovers the missed events.
        queue = asyncio.Queue()

        base = self.get_open_orders_mock()  # sequence 59342 — first observed value becomes the base
        await self.data_source._process_event_message(base, queue)

        in_sequence = self.get_open_orders_mock()
        in_sequence[-1]["sequence"] = 59343
        await self.data_source._process_event_message(in_sequence, queue)

        gapped = self.get_open_orders_mock()
        gapped[-1]["sequence"] = 59345  # 59344 was lost
        with self.assertRaises(IOError):
            await self.data_source._process_event_message(gapped, queue)

        # The two in-sequence frames were queued; the gapped frame was not (replayed after reconnect).
        self.assertEqual(2, queue.qsize())
        # Tracking is reset so the next frame after the reconnect establishes a fresh base.
        self.assertEqual({}, self.data_source._channel_sequences)

    async def test_sequence_channels_tracked_independently(self):
        queue = asyncio.Queue()

        await self.data_source._process_event_message(self.get_open_orders_mock(), queue)  # openOrders 59342
        await self.data_source._process_event_message(self.get_own_trades_mock(), queue)  # ownTrades 2948

        own_trades_next = self.get_own_trades_mock()
        own_trades_next[-1]["sequence"] = 2949
        await self.data_source._process_event_message(own_trades_next, queue)

        self.assertEqual(3, queue.qsize())
        self.assertEqual(59342, self.data_source._channel_sequences["openOrders"])
        self.assertEqual(2949, self.data_source._channel_sequences["ownTrades"])

    async def test_sequence_duplicate_raises(self):
        # A repeated sequence number is as anomalous as a gap — the stream state is no longer trusted.
        queue = asyncio.Queue()
        await self.data_source._process_event_message(self.get_open_orders_mock(), queue)
        with self.assertRaises(IOError):
            await self.data_source._process_event_message(self.get_open_orders_mock(), queue)

    async def test_subscribe_channels_resets_sequence_tracking(self):
        # A fresh subscription restarts each channel's numbering; stale expectations must be dropped
        # or the first post-reconnect frame would always be misread as a gap.
        ws = AsyncMock()
        self.data_source._channel_sequences["openOrders"] = 10

        with patch.object(self.data_source, "get_auth_token", new=AsyncMock(return_value="token")):
            await self.data_source._subscribe_channels(ws)

        self.assertEqual({}, self.data_source._channel_sequences)

        # Any first value is then accepted as the new base (start-at-1 is not relied upon).
        queue = asyncio.Queue()
        fresh = self.get_open_orders_mock()
        fresh[-1]["sequence"] = 1
        await self.data_source._process_event_message(fresh, queue)
        self.assertEqual(1, self.data_source._channel_sequences["openOrders"])

    async def test_frame_without_sequence_passes_through(self):
        # A frame whose trailing dict carries no sequence number must keep flowing untouched
        # (no tracking, no gap detection) — the validation is strictly opt-in on Kraken's field.
        queue = asyncio.Queue()
        frame = [[{"OGTT3Y-C6I3P-XRI6HX": {"status": "closed"}}], "openOrders", {}]
        await self.data_source._process_event_message(frame, queue)
        self.assertEqual(frame, await queue.get())
        self.assertEqual({}, self.data_source._channel_sequences)
