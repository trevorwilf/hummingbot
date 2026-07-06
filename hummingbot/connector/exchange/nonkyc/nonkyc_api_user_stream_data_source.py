import asyncio
from typing import TYPE_CHECKING, List, Optional

from async_timeout import timeout

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
        self._connector = connector
        self._domain = domain
        self._api_factory = api_factory
        self._ws_request_id: int = 100  # Start at 100 to distinguish from order book ids in logs

    def _next_ws_id(self) -> int:
        """Returns the next JSON-RPC 2.0 request id."""
        self._ws_request_id += 1
        return self._ws_request_id

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
            "params": {},
            "id": self._next_ws_id()
        })
        await websocket_assistant.send(subscribe_user_orders_request)
        self.logger().info("Subscribed to user orders")

        # Balance updates -- undocumented API, subscribe but don't fail if rejected
        enable_balance_ws = getattr(self._connector, 'ENABLE_BALANCE_WS', True)
        if not enable_balance_ws:
            self.logger().info(
                "NonKYC private balance WebSocket: DISABLED by ENABLE_BALANCE_WS flag. "
                "Using REST polling for balance updates."
            )
        else:
            try:
                subscribe_user_balance_request: WSJSONRequest = WSJSONRequest(payload={
                    "method": CONSTANTS.WS_METHOD_SUBSCRIBE_USER_BALANCE,
                    "params": {},
                    "id": self._next_ws_id()
                })
                await websocket_assistant.send(subscribe_user_balance_request)
                self.logger().info(
                    "NonKYC private balance WebSocket: subscription REQUESTED "
                    "(undocumented method — will confirm on first balance event, "
                    "REST polling active as fallback)"
                )
            except Exception as e:
                self.logger().warning(
                    f"NonKYC private balance WebSocket: UNAVAILABLE (subscription request failed: {e}). "
                    f"Using REST polling for balance updates."
                )

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    async def _authenticate_ws_connection(self, ws: WSAssistant):
        """
        Sends the authentication message and validates the response.
        Includes timeout and retry with exponential backoff.
        Correlates the response on the request ID to avoid accepting
        unrelated messages as auth confirmations.
        :param ws: the websocket assistant used to connect to the exchange
        """
        max_retries = 3
        base_timeout = 10.0

        for attempt in range(1, max_retries + 1):
            try:
                auth_payload = self._auth.generate_ws_authentication_message()
                auth_request_id = auth_payload.get("id")  # Currently 99
                auth_message: WSJSONRequest = WSJSONRequest(payload=auth_payload)
                await ws.send(auth_message)

                # Wait for auth response with hard timeout on silent sockets.
                # async_timeout (not asyncio.timeout): asyncio.timeout needs Python >= 3.11
                # while the env pin allows resolving lower; async_timeout raises
                # asyncio.TimeoutError so the except clause below is unchanged.
                try:
                    async with timeout(base_timeout * attempt):
                        async for ws_response in ws.iter_messages():
                            data = ws_response.data
                            if not isinstance(data, dict):
                                continue  # skip non-dict messages

                            # Only accept responses matching our auth request ID
                            response_id = data.get("id")
                            if auth_request_id is not None and response_id != auth_request_id:
                                continue  # Not our auth response

                            if data.get("result") is True:
                                self.logger().info("WebSocket authentication successful")
                                return
                            elif "error" in data:
                                error_msg = data.get("error", {}).get("message", "Unknown error")
                                raise IOError(f"WebSocket authentication failed: {error_msg}")

                        # iter_messages exhausted without auth response
                        raise IOError("WebSocket closed before authentication completed")
                except asyncio.TimeoutError:
                    raise asyncio.TimeoutError(
                        f"WS auth response not received within {base_timeout * attempt}s")

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
        # Increment reconnect counter on the exchange connector
        try:
            self._connector._ws_reconnect_count += 1
            self._connector._ws_reconnect_count_since_log += 1
            self._connector._increment_error("ws_reconnect")
            self.logger().info(
                f"WS user stream interrupted (reconnect #{self._connector._ws_reconnect_count})"
            )
            # Reset balance WS confirmation state so the watchdog can
            # re-evaluate after reconnect
            if hasattr(self._connector, '_reset_balance_ws_state'):
                self._connector._reset_balance_ws_state()
            # Mark orders as not yet reconciled (prevents premature settling exit)
            if hasattr(self._connector, '_orders_reconciled_after_reconnect'):
                self._connector._orders_reconciled_after_reconnect = False
            # Force an immediate REST balance refresh to ensure consistency
            if hasattr(self._connector, '_update_balances'):
                asyncio.ensure_future(self._connector._update_balances())
                self.logger().info("Forced REST balance refresh after WS reconnect")
            # Force active orders reconciliation after reconnect
            if hasattr(self._connector, '_reconcile_active_orders_after_reconnect'):
                asyncio.ensure_future(self._connector._reconcile_active_orders_after_reconnect())
                self.logger().info("Forced active orders reconciliation after WS reconnect")
        except AttributeError:
            pass
        except Exception as e:
            self.logger().warning(f"Post-reconnect cleanup failed: {repr(e)}")
