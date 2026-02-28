import asyncio
from typing import TYPE_CHECKING, List, Optional

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class NonkycAPIUserStreamDataSource(UserStreamTrackerDataSource):

    HEARTBEAT_TIME_INTERVAL = 30.0

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 auth: NonkycAuth,
                 trading_pairs: List[str],
                 connector: 'NonkycExchange',
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__()
        self._auth: NonkycAuth = auth
        self._domain = domain
        self._api_factory = api_factory

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Creates an instance of WSAssistant connected to the exchange and authenticates it.
        """
        ws: WSAssistant = await self._get_ws_assistant()
        await ws.connect(ws_url=CONSTANTS.WS_URL, ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL)
        await self._authenticate_ws_connection(ws)
        return ws

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        """
        Subscribes to user order reports and balance updates.
        :param websocket_assistant: the websocket assistant used to connect to the exchange
        """
        subscribe_user_orders_request: WSJSONRequest = WSJSONRequest(payload={
            "method": CONSTANTS.WS_METHOD_SUBSCRIBE_USER_ORDERS,
            "params": {}
        })
        await websocket_assistant.send(subscribe_user_orders_request)
        self.logger().info("Subscribed to user orders")

        subscribe_user_balance_request: WSJSONRequest = WSJSONRequest(payload={
            "method": CONSTANTS.WS_METHOD_SUBSCRIBE_USER_BALANCE,
            "params": {}
        })
        await websocket_assistant.send(subscribe_user_balance_request)
        self.logger().info("Subscribed to user balance")

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    async def _authenticate_ws_connection(self, ws: WSAssistant):
        """
        Sends the authentication message and validates the response.
        Includes timeout and retry with exponential backoff.
        :param ws: the websocket assistant used to connect to the exchange
        """
        max_retries = 3
        base_timeout = 10.0

        for attempt in range(1, max_retries + 1):
            try:
                auth_message: WSJSONRequest = WSJSONRequest(
                    payload=self._auth.generate_ws_authentication_message())
                await ws.send(auth_message)

                # Wait for auth response with timeout, skipping non-auth messages
                deadline = asyncio.get_event_loop().time() + base_timeout * attempt
                async for ws_response in ws.iter_messages():
                    if asyncio.get_event_loop().time() > deadline:
                        raise asyncio.TimeoutError(
                            f"WS auth response not received within {base_timeout * attempt}s")

                    data = ws_response.data
                    if not isinstance(data, dict):
                        continue  # skip non-dict messages

                    if data.get("result") is True:
                        self.logger().info("WebSocket authentication successful")
                        return
                    elif "error" in data:
                        error_msg = data.get("error", {}).get("message", "Unknown error")
                        raise IOError(f"WebSocket authentication failed: {error_msg}")

                    # Not an auth response (e.g., ticker update) — keep waiting
                    continue

                # iter_messages exhausted without auth response
                raise IOError("WebSocket closed before authentication completed")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Auth explicitly failed (wrong credentials) — don't retry
                if isinstance(e, IOError) and "authentication failed" in str(e).lower():
                    raise
                if attempt < max_retries:
                    backoff = 2 ** (attempt - 1)
                    self.logger().warning(
                        f"WS auth attempt {attempt}/{max_retries} failed: {repr(e)}. "
                        f"Retrying in {backoff}s...")
                    await self._sleep(backoff)
                else:
                    raise IOError(
                        f"WebSocket authentication failed after {max_retries} attempts: {repr(e)}")

    async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
        websocket_assistant and await websocket_assistant.disconnect()
        self._ws_assistant = None
