import asyncio
import json
from typing import Optional
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import NonkycAPIUserStreamDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.ws_assistant import WSAssistant


class NonkycAPIUserStreamDataSourceTests(IsolatedAsyncioWrapperTestCase):

    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []

        self.time_synchronizer = TimeSynchronizer()
        self.time_synchronizer.add_time_offset_ms_sample(0)

        self.auth = NonkycAuth(
            api_key="testAPIKey",
            secret_key="testSecret",
            time_provider=self.time_synchronizer,
        )

        self.connector = NonkycExchange(
            nonkyc_api_key="testAPIKey",
            nonkyc_api_secret="testSecret",
            trading_pairs=[self.trading_pair],
            trading_required=False,
        )

        self.data_source = NonkycAPIUserStreamDataSource(
            auth=self.auth,
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory,
        )
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)

    def handle(self, record):
        self.log_records.append(record)

    def is_logged(self, log_level: str, message: str) -> bool:
        return any(
            record.levelname == log_level and message in record.getMessage()
            for record in self.log_records
        )

    async def test_subscribe_channels(self):
        mock_ws = AsyncMock(spec=WSAssistant)
        sent_messages = []

        async def capture_send(request):
            sent_messages.append(request.payload)

        mock_ws.send.side_effect = capture_send

        await self.data_source._subscribe_channels(mock_ws)

        self.assertEqual(2, len(sent_messages))
        self.assertEqual(CONSTANTS.WS_METHOD_SUBSCRIBE_USER_ORDERS, sent_messages[0]["method"])
        self.assertEqual(CONSTANTS.WS_METHOD_SUBSCRIBE_USER_BALANCE, sent_messages[1]["method"])

    async def test_auth_response_validated_success(self):
        mock_ws = AsyncMock(spec=WSAssistant)

        # Mock the auth message generation
        auth_payload = self.auth.generate_ws_authentication_message()

        # Create a successful auth response
        success_response = MagicMock()
        success_response.data = {"jsonrpc": "2.0", "result": True, "id": 99}

        async def mock_iter():
            yield success_response

        mock_ws.iter_messages.return_value = mock_iter()
        mock_ws.send = AsyncMock()

        await self.data_source._authenticate_ws_connection(mock_ws)

        self.assertTrue(self.is_logged("INFO", "WebSocket authentication successful"))

    async def test_auth_failure_raises(self):
        mock_ws = AsyncMock(spec=WSAssistant)

        error_response = MagicMock()
        error_response.data = {
            "jsonrpc": "2.0",
            "error": {"code": 1002, "message": "Authorization failed"},
        }

        async def mock_iter():
            yield error_response

        mock_ws.iter_messages.return_value = mock_iter()
        mock_ws.send = AsyncMock()

        with self.assertRaises(IOError) as context:
            await self.data_source._authenticate_ws_connection(mock_ws)

        self.assertIn("Authorization failed", str(context.exception))

    async def test_user_stream_interruption_cleanup(self):
        mock_ws = AsyncMock(spec=WSAssistant)
        self.data_source._ws_assistant = mock_ws

        await self.data_source._on_user_stream_interruption(mock_ws)

        mock_ws.disconnect.assert_called_once()
        self.assertIsNone(self.data_source._ws_assistant)
