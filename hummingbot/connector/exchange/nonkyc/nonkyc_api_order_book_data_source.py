import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS, nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_order_book import NonkycOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class NonkycAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = 30.0
    TRADE_STREAM_ID = 1
    DIFF_STREAM_ID = 2
    ONE_HOUR = 60 * 60

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'NonkycExchange',
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.TRADE_EVENT_TYPE
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._snapshot_messages_queue_key = CONSTANTS.SNAPSHOT_EVENT_TYPE
        self._domain = domain
        self._api_factory = api_factory
        self._last_sequence: Dict[str, int] = {}

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        """
        Retrieves a copy of the full order book from the exchange, for a particular trading pair.

        :param trading_pair: the trading pair for which the order book will be retrieved

        :return: the response from the exchange (JSON dictionary)
        """
        symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        params = {
            "symbol": symbol,
            "limit": "1000"
        }

        rest_assistant = await self._api_factory.get_rest_assistant()
        data = await rest_assistant.execute_request(
            url=web_utils.public_rest_url(path_url=CONSTANTS.MARKET_ORDERBOOK_PATH_URL),
            params=params,
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.MARKET_ORDERBOOK_PATH_URL,
        )

        return data

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

                trade_payload = {
                    "method": CONSTANTS.WS_METHOD_SUBSCRIBE_TRADES,
                    "params": {"symbol": symbol}
                }
                subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trade_payload)
                await ws.send(subscribe_trade_request)

                ob_payload = {
                    "method": CONSTANTS.WS_METHOD_SUBSCRIBE_ORDERBOOK,
                    "params": {"symbol": symbol, "limit": 100}
                }
                subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=ob_payload)
                await ws.send(subscribe_orderbook_request)

            self.logger().info("Subscribed to public order book and trade channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to order book trading and delta streams...",
                exc_info=True
            )
            raise

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=CONSTANTS.WS_URL,
                         ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL)
        return ws

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = NonkycOrderBook.snapshot_message_from_exchange(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair}
        )
        return snapshot_msg

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "result" not in raw_message:
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
                symbol=raw_message.get("params", {}).get("symbol"))
            trade_messages = NonkycOrderBook.trade_messages_from_exchange(
                raw_message, {"trading_pair": trading_pair})
            for trade_message in trade_messages:
                message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "result" not in raw_message:
            params = raw_message.get("params", {})
            symbol = params.get("symbol")
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
            sequence = int(params.get("sequence", 0))
            last_seq = self._last_sequence.get(trading_pair, 0)

            if sequence <= last_seq:
                # Duplicate or stale message, skip
                return

            if last_seq > 0 and sequence > last_seq + 1:
                # Gap detected — trigger REST snapshot resync
                self.logger().warning(
                    f"Orderbook sequence gap for {trading_pair}: expected {last_seq + 1}, got {sequence}. "
                    f"Requesting REST snapshot resync."
                )
                snapshot_msg = await self._order_book_snapshot(trading_pair)
                snapshot_queue = self._message_queue[self._snapshot_messages_queue_key]
                snapshot_queue.put_nowait(snapshot_msg)
                self._last_sequence[trading_pair] = int(snapshot_msg.update_id)
                return

            self._last_sequence[trading_pair] = sequence
            order_book_message: OrderBookMessage = NonkycOrderBook.diff_message_from_exchange(
                raw_message, time.time(), {"trading_pair": trading_pair})
            message_queue.put_nowait(order_book_message)

    async def _parse_order_book_snapshot_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "result" not in raw_message:
            params = raw_message.get("params", {})
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
                symbol=params.get("symbol"))
            sequence = int(params.get("sequence", 0))
            self._last_sequence[trading_pair] = sequence
            snapshot_msg: OrderBookMessage = NonkycOrderBook.snapshot_message_from_exchange(
                params, time.time(), metadata={"trading_pair": trading_pair})
            message_queue.put_nowait(snapshot_msg)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = ""
        if "result" not in event_message:
            event_type = event_message.get("method")
            if event_type == CONSTANTS.TRADE_EVENT_TYPE or event_type == CONSTANTS.SNAPSHOT_TRADES_EVENT_TYPE:
                channel = self._trade_messages_queue_key
            elif event_type == CONSTANTS.DIFF_EVENT_TYPE:
                channel = self._diff_messages_queue_key
            elif event_type == CONSTANTS.SNAPSHOT_EVENT_TYPE:
                channel = self._snapshot_messages_queue_key
        return channel

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        """
        Subscribes to order book and trade channels for a single trading pair on the
        existing WebSocket connection.

        :param trading_pair: the trading pair to subscribe to
        :return: True if subscription was successful, False otherwise
        """
        if self._ws_assistant is None:
            self.logger().warning(
                f"Cannot subscribe to {trading_pair}: WebSocket not connected"
            )
            return False

        try:
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

            trade_payload = {
                "method": CONSTANTS.WS_METHOD_SUBSCRIBE_TRADES,
                "params": {"symbol": symbol}
            }
            await self._ws_assistant.send(WSJSONRequest(payload=trade_payload))

            ob_payload = {
                "method": CONSTANTS.WS_METHOD_SUBSCRIBE_ORDERBOOK,
                "params": {"symbol": symbol, "limit": 100}
            }
            await self._ws_assistant.send(WSJSONRequest(payload=ob_payload))

            self.add_trading_pair(trading_pair)
            self.logger().info(f"Subscribed to {trading_pair} order book and trade channels")
            return True

        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error subscribing to {trading_pair} channels")
            return False

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        """
        Unsubscribes from order book and trade channels for a single trading pair.
        Sends explicit unsubscribe messages to the NonKYC WebSocket API.

        :param trading_pair: the trading pair to unsubscribe from
        :return: True if successfully unsubscribed, False otherwise
        """
        if self._ws_assistant is None:
            self.logger().warning(
                f"Cannot unsubscribe from {trading_pair}: WebSocket not connected"
            )
            return False

        try:
            symbol = await self._connector.exchange_symbol_associated_to_pair(
                trading_pair=trading_pair)

            # Send unsubscribe for trades
            unsub_trades_payload = {
                "method": CONSTANTS.WS_METHOD_UNSUBSCRIBE_TRADES,
                "params": {"symbol": symbol}
            }
            await self._ws_assistant.send(WSJSONRequest(payload=unsub_trades_payload))

            # Send unsubscribe for orderbook
            unsub_ob_payload = {
                "method": CONSTANTS.WS_METHOD_UNSUBSCRIBE_ORDERBOOK,
                "params": {"symbol": symbol}
            }
            await self._ws_assistant.send(WSJSONRequest(payload=unsub_ob_payload))

            # Remove from internal tracking
            self.remove_trading_pair(trading_pair)
            # Clean up sequence tracking
            self._last_sequence.pop(trading_pair, None)

            self.logger().info(f"Unsubscribed from {trading_pair} order book and trade channels")
            return True

        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(
                f"Unexpected error unsubscribing from {trading_pair} channels")
            return False
