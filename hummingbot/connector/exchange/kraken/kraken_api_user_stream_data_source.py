import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.kraken.kraken_exchange import KrakenExchange


class KrakenAPIUserStreamDataSource(UserStreamTrackerDataSource):
    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 connector: 'KrakenExchange',
                 api_factory: Optional[WebAssistantsFactory] = None):

        super().__init__()
        self._api_factory = api_factory
        self._connector = connector
        self._current_auth_token: Optional[str] = None
        # KRK-12: last seen per-channel sequence number on ownTrades/openOrders frames.
        self._channel_sequences: Dict[str, int] = {}

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=CONSTANTS.WS_AUTH_URL, ping_timeout=CONSTANTS.PING_TIMEOUT)
        return ws

    @property
    def last_recv_time(self):
        if self._ws_assistant is None:
            return 0
        else:
            return self._ws_assistant.last_recv_time

    async def get_auth_token(self) -> str:
        try:
            response_json = await self._connector._api_post(path_url=CONSTANTS.GET_TOKEN_PATH_URL, params={},
                                                            is_auth_required=True)
        except Exception:
            raise
        return response_json["token"]

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        """
        Subscribes to order events and balance events.

        :param websocket_assistant: the websocket assistant used to connect to the exchange
        """
        try:
            # KRK-12: a fresh subscription restarts each private channel's sequence numbering.
            self._channel_sequences.clear()

            # Always mint a fresh WS token on (re)subscribe. Kraken tokens expire ~15 minutes and are only
            # valid to ESTABLISH a connection within that window; reusing a cached token after a reconnect
            # would fail the subscription and trap the private stream in a permanent reconnect loop.
            self._current_auth_token = await self.get_auth_token()

            orders_change_payload = {
                "event": "subscribe",
                "subscription": {
                    "name": "openOrders",
                    "token": self._current_auth_token
                }
            }
            subscribe_order_change_request: WSJSONRequest = WSJSONRequest(payload=orders_change_payload)

            trades_payload = {
                "event": "subscribe",
                "subscription": {
                    "name": "ownTrades",
                    "token": self._current_auth_token
                }
            }
            subscribe_trades_request: WSJSONRequest = WSJSONRequest(payload=trades_payload)

            await websocket_assistant.send(subscribe_order_change_request)
            await websocket_assistant.send(subscribe_trades_request)

            self.logger().info("Subscribed to private order changes and trades updates channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception("Unexpected error occurred subscribing to user streams...")
            raise

    async def _process_event_message(self, event_message: Dict[str, Any], queue: asyncio.Queue):
        if (isinstance(event_message, list) and len(event_message) >= 2 and event_message[-2] in (
                CONSTANTS.USER_TRADES_ENDPOINT_NAME,
                CONSTANTS.USER_ORDERS_ENDPOINT_NAME,
        )):
            self._track_channel_sequence(event_message)
            queue.put_nowait(event_message)
        elif isinstance(event_message, dict) and event_message.get("errorMessage") is not None:
            # Only dict control frames carry errorMessage; a short/unknown list frame is safely ignored
            # (the length check above prevents IndexError, this branch prevents AttributeError on lists).
            err_msg = event_message.get("errorMessage")
            raise IOError({
                "label": "WSS_ERROR",
                "message": f"Error received via websocket - {err_msg}."
            })

    def _track_channel_sequence(self, event_message: list):
        """
        KRK-12: ownTrades/openOrders frames end with {"sequence": N}, incrementing by 1 per channel.
        A gap means missed fills/status transitions on a connection that still looks healthy;
        raising tears the connection down so the reconnect + subscription snapshot replay recovers
        the missed events. The first value observed after a (re)subscribe is accepted as the base —
        the documented start-at-1 behavior is deliberately not relied upon.
        """
        tail = event_message[-1]
        sequence = tail.get("sequence") if isinstance(tail, dict) else None
        if sequence is None:
            return
        channel = event_message[-2]
        sequence = int(sequence)
        last_sequence = self._channel_sequences.get(channel)
        if last_sequence is not None and sequence != last_sequence + 1:
            self._channel_sequences.clear()
            raise IOError(
                f"Sequence gap on Kraken private channel {channel}: expected {last_sequence + 1}, "
                f"received {sequence}. Reconnecting user stream to replay missed events.")
        self._channel_sequences[channel] = sequence
