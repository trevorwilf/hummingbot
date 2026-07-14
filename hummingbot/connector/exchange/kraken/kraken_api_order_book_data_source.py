import asyncio
import time
import zlib
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS, kraken_web_utils as web_utils
from hummingbot.connector.exchange.kraken.kraken_order_book import KrakenOrderBook
from hummingbot.connector.exchange.kraken.kraken_utils import (
    convert_from_exchange_trading_pair,
    convert_to_exchange_trading_pair,
)
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.rest_assistant import RESTAssistant
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.kraken.kraken_exchange import KrakenExchange


class KrakenAPIOrderBookDataSource(OrderBookTrackerDataSource):
    MESSAGE_TIMEOUT = 30.0
    # Depth requested on the WS book subscription. The checksum replica must be maintained at this
    # depth (a deletion inside the top 10 promotes a deeper level into the hashed window).
    SUBSCRIPTION_DEPTH = 1000
    # KRK-12: Kraken's book checksum always covers the top 10 price levels of each side,
    # regardless of the subscribed depth.
    CHECKSUM_LEVEL_DEPTH = 10
    # A single mismatch must NOT disconnect: the live capture (2026-07-14, XBT/USD) matched 12/15
    # messages with the 3 misses being warm-up/edge transients. Only consecutive mismatches indicate
    # real book drift.
    CHECKSUM_MISMATCHES_BEFORE_RECONNECT = 2

    # PING_TIMEOUT = 10.0

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'KrakenExchange',
                 api_factory: WebAssistantsFactory,
                 # throttler: Optional[AsyncThrottler] = None
                 ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._rest_assistant = None
        self._ws_assistant = None
        self._order_book_create_function = lambda: OrderBook()
        # Highest update_id emitted so far on the diff/snapshot WS path, used to keep update_id monotonic.
        self._last_diff_update_id: float = 0.0
        # KRK-12: per-pair book replica built from the feed's OWN price/volume strings
        # (pair -> side -> {Decimal(price): (price_str, volume_str)}), used only to validate the
        # CRC32 checksum Kraken attaches to every diff. The strings must be kept verbatim because
        # the checksum is defined over the feed's string representation.
        self._checksum_books: Dict[str, Dict[str, Dict[Decimal, Tuple[str, str]]]] = {}
        self._checksum_mismatch_counts: Dict[str, int] = defaultdict(int)

    _kraobds_logger: Optional[HummingbotLogger] = None

    async def _get_rest_assistant(self) -> RESTAssistant:
        if self._rest_assistant is None:
            self._rest_assistant = await self._api_factory.get_rest_assistant()
        return self._rest_assistant

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBook:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = KrakenOrderBook.snapshot_message_from_exchange(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair}
        )
        return snapshot_msg

    async def _request_order_book_snapshot(self, trading_pair: str, ) -> Dict[str, Any]:
        """
        Retrieves a copy of the full order book from the exchange, for a particular trading pair.

        :param trading_pair: the trading pair for which the order book will be retrieved

        :return: the response from the exchange (JSON dictionary)
        """
        params = {
            "pair": await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        }

        rest_assistant = await self._api_factory.get_rest_assistant()
        response_json = await rest_assistant.execute_request(
            url=web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL),
            params=params,
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
        )
        if len(response_json["error"]) > 0:
            raise IOError(f"Error fetching Kraken market snapshot for {trading_pair}. "
                          f"Error is {response_json['error']}.")
        data: Dict[str, Any] = next(iter(response_json["result"].values()))
        data = {"trading_pair": trading_pair, **data}
        data["latest_update"] = max([*map(lambda x: x[2], data["bids"] + data["asks"])], default=0.)
        return data

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.

        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            trading_pairs: List[str] = []
            for tp in self._trading_pairs:
                # trading_pairs.append(convert_to_exchange_trading_pair(tp, '/'))
                symbol = convert_to_exchange_trading_pair(tp, '/')
                trading_pairs.append(symbol)
            trades_payload = {
                "event": "subscribe",
                "pair": trading_pairs,
                "subscription": {"name": 'trade'},
            }
            subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trades_payload)

            order_book_payload = {
                "event": "subscribe",
                "pair": trading_pairs,
                "subscription": {"name": 'book', "depth": self.SUBSCRIPTION_DEPTH},
            }
            subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=order_book_payload)

            await ws.send(subscribe_trade_request)
            await ws.send(subscribe_orderbook_request)

            self.logger().info("Subscribed to public order book and trade channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error("Unexpected error occurred subscribing to order book data streams.")
            raise

    def _channel_originating_message(self, event_message) -> str:
        channel = ""
        if type(event_message) is list:
            channel = self._trade_messages_queue_key if event_message[-2] == CONSTANTS.TRADE_EVENT_TYPE \
                else self._diff_messages_queue_key
        else:
            if event_message.get("errorMessage") is not None:
                err_msg = event_message.get("errorMessage")
                raise IOError(f"Error event received from the server ({err_msg})")
        return channel

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=CONSTANTS.WS_URL,
                         ping_timeout=CONSTANTS.PING_TIMEOUT)
        return ws

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):

        trades = [
            {"pair": convert_from_exchange_trading_pair(raw_message[-1]), "trade": trade}
            for trade in raw_message[1]
        ]
        for trade in trades:
            trade_msg: OrderBookMessage = KrakenOrderBook.trade_message_from_exchange(trade)
            message_queue.put_nowait(trade_msg)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        trading_pair = convert_from_exchange_trading_pair(raw_message[-1])
        # KRK-1: Kraken WS v1 sends combined both-side updates as SEPARATE payload dicts, e.g.
        # [ch, {"a": [...]}, {"b": [...], "c": "..."}, "book-10", "PAIR"] (live-observed). Reading only
        # raw_message[1] silently dropped the second dict's side (~0.16% of diffs on busy pairs).
        # Merge every dict element between the channel id and the trailing [channel-name, pair] tail;
        # the checksum key "c" may ride in either dict.
        payload_dicts = [element for element in raw_message[1:-2] if isinstance(element, dict)]
        asks: List[Any] = []
        bids: List[Any] = []
        checksum: Optional[str] = None
        is_snapshot = False
        for payload in payload_dicts:
            asks.extend(payload.get("a", []) or payload.get("as", []) or [])
            bids.extend(payload.get("b", []) or payload.get("bs", []) or [])
            if "as" in payload or "bs" in payload:
                is_snapshot = True
            if "c" in payload:
                checksum = payload["c"]
        msg_dict = {"trading_pair": trading_pair, "asks": asks, "bids": bids}
        raw_update_id = max(
            [*map(lambda x: float(x[2]), msg_dict["bids"] + msg_dict["asks"])], default=0.
        )
        # Kraken derives update_id from per-level lastchange timestamps, which can momentarily move
        # backwards between messages; clamp to a monotonic non-decreasing value so the order-book tracker
        # does not discard a newer diff as stale.
        self._last_diff_update_id = max(raw_update_id, self._last_diff_update_id)
        msg_dict["update_id"] = self._last_diff_update_id
        if is_snapshot:
            self._reset_checksum_book(trading_pair, asks=asks, bids=bids)
            order_book_message: OrderBookMessage = (
                KrakenOrderBook.snapshot_ws_message_from_exchange(msg_dict, time.time())
            )
        else:
            self._apply_diff_to_checksum_book(trading_pair, payload_dicts)
            order_book_message: OrderBookMessage = KrakenOrderBook.diff_message_from_exchange(
                msg_dict, time.time())
        message_queue.put_nowait(order_book_message)
        if checksum is not None:
            await self._validate_book_checksum(trading_pair, checksum)

    def _reset_checksum_book(self, trading_pair: str, asks: List[Any], bids: List[Any]):
        book: Dict[str, Dict[Decimal, Tuple[str, str]]] = {"asks": {}, "bids": {}}
        for level in asks:
            book["asks"][Decimal(level[0])] = (level[0], level[1])
        for level in bids:
            book["bids"][Decimal(level[0])] = (level[0], level[1])
        self._checksum_books[trading_pair] = book
        self._checksum_mismatch_counts.pop(trading_pair, None)

    def _apply_diff_to_checksum_book(self, trading_pair: str, payload_dicts: List[Dict[str, Any]]):
        book = self._checksum_books.get(trading_pair)
        if book is None:
            # No WS snapshot seen yet for this pair (warm-up); nothing to validate against.
            return
        # Apply deltas in message order; volume 0 deletes the level. Level entries are
        # [price, volume, timestamp] or [price, volume, timestamp, "r"] (republished) — the trailing
        # fields are irrelevant here.
        for payload in payload_dicts:
            for key, side in (("a", "asks"), ("b", "bids")):
                for level in payload.get(key, []):
                    price_str, volume_str = level[0], level[1]
                    price = Decimal(price_str)
                    if Decimal(volume_str) == 0:
                        book[side].pop(price, None)
                    else:
                        book[side][price] = (price_str, volume_str)
        # Trim to the subscribed depth: the feed republishes levels that move into the window, so
        # levels pushed out of it must be dropped or the replica diverges from the real book.
        for price in sorted(book["asks"])[self.SUBSCRIPTION_DEPTH:]:
            del book["asks"][price]
        for price in sorted(book["bids"], reverse=True)[self.SUBSCRIPTION_DEPTH:]:
            del book["bids"][price]

    @staticmethod
    def _checksum_field(value: str) -> str:
        # Live-verified 2026-07-14: the checksum input is the feed's own string with the decimal
        # point removed and leading zeros stripped ("64487.90000" -> "6448790000",
        # "0.00420000" -> "420000").
        return value.replace(".", "").lstrip("0")

    def _compute_book_checksum(self, book: Dict[str, Dict[Decimal, Tuple[str, str]]]) -> int:
        parts: List[str] = []
        for price in sorted(book["asks"])[:self.CHECKSUM_LEVEL_DEPTH]:
            price_str, volume_str = book["asks"][price]
            parts.append(self._checksum_field(price_str))
            parts.append(self._checksum_field(volume_str))
        for price in sorted(book["bids"], reverse=True)[:self.CHECKSUM_LEVEL_DEPTH]:
            price_str, volume_str = book["bids"][price]
            parts.append(self._checksum_field(price_str))
            parts.append(self._checksum_field(volume_str))
        return zlib.crc32("".join(parts).encode("utf-8")) & 0xffffffff

    async def _validate_book_checksum(self, trading_pair: str, checksum: str):
        book = self._checksum_books.get(trading_pair)
        if (book is None
                or len(book["asks"]) < self.CHECKSUM_LEVEL_DEPTH
                or len(book["bids"]) < self.CHECKSUM_LEVEL_DEPTH):
            # Warm-up guard (mandatory): only validate once the book holds a full 10x10 — the live
            # capture's false mismatches all occurred on partially built books.
            return
        try:
            expected = int(checksum)
        except (TypeError, ValueError):
            return
        local = self._compute_book_checksum(book)
        if local == expected:
            self._checksum_mismatch_counts.pop(trading_pair, None)
            return
        self._checksum_mismatch_counts[trading_pair] += 1
        mismatches = self._checksum_mismatch_counts[trading_pair]
        if mismatches < self.CHECKSUM_MISMATCHES_BEFORE_RECONNECT:
            self.logger().debug(
                f"Order book checksum mismatch for {trading_pair} "
                f"({mismatches}/{self.CHECKSUM_MISMATCHES_BEFORE_RECONNECT}): "
                f"local={local} expected={expected}. Awaiting confirmation before reconnect.")
            return
        self.logger().warning(
            f"ORDERBOOK_CHECKSUM_EVENT: pair={trading_pair} consecutive_mismatches={mismatches} "
            f"local={local} expected={expected}. Local book has drifted from the exchange; "
            "disconnecting websocket for a clean resync.")
        self._checksum_books.pop(trading_pair, None)
        self._checksum_mismatch_counts.pop(trading_pair, None)
        ws = self._ws_assistant
        if ws is not None:
            try:
                await ws.disconnect()
            except Exception:
                self.logger().debug("Error disconnecting WS after checksum mismatch", exc_info=True)

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        await super()._on_order_stream_interruption(websocket_assistant)
        # Fresh snapshots arrive per pair after reconnect; stale replicas must not be diffed against.
        self._checksum_books.clear()
        self._checksum_mismatch_counts.clear()

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        """
        Subscribes to order book and trade channels for a single trading pair
        on the existing WebSocket connection.

        :param trading_pair: the trading pair to subscribe to
        :return: True if subscription was successful, False otherwise
        """
        if self._ws_assistant is None:
            self.logger().warning(
                f"Cannot subscribe to {trading_pair}: WebSocket not connected"
            )
            return False

        try:
            symbol = convert_to_exchange_trading_pair(trading_pair, '/')

            trades_payload = {
                "event": "subscribe",
                "pair": [symbol],
                "subscription": {"name": "trade"},
            }
            subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trades_payload)

            order_book_payload = {
                "event": "subscribe",
                "pair": [symbol],
                "subscription": {"name": "book", "depth": self.SUBSCRIPTION_DEPTH},
            }
            subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=order_book_payload)

            await self._ws_assistant.send(subscribe_trade_request)
            await self._ws_assistant.send(subscribe_orderbook_request)

            self.add_trading_pair(trading_pair)
            self.logger().info(f"Subscribed to {trading_pair} order book and trade channels")
            return True

        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Error subscribing to {trading_pair}")
            return False

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        """
        Unsubscribes from order book and trade channels for a single trading pair
        on the existing WebSocket connection.

        :param trading_pair: the trading pair to unsubscribe from
        :return: True if unsubscription was successful, False otherwise
        """
        if self._ws_assistant is None:
            self.logger().warning(
                f"Cannot unsubscribe from {trading_pair}: WebSocket not connected"
            )
            return False

        try:
            symbol = convert_to_exchange_trading_pair(trading_pair, '/')

            trades_payload = {
                "event": "unsubscribe",
                "pair": [symbol],
                "subscription": {"name": "trade"},
            }
            unsubscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trades_payload)

            order_book_payload = {
                "event": "unsubscribe",
                "pair": [symbol],
                "subscription": {"name": "book", "depth": self.SUBSCRIPTION_DEPTH},
            }
            unsubscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=order_book_payload)

            await self._ws_assistant.send(unsubscribe_trade_request)
            await self._ws_assistant.send(unsubscribe_orderbook_request)

            self.remove_trading_pair(trading_pair)
            self.logger().info(f"Unsubscribed from {trading_pair} order book and trade channels")
            return True

        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Error unsubscribing from {trading_pair}")
            return False
