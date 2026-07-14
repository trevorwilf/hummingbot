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
    RESYNC_MAX_FAILURES = 3
    RESYNC_TIMEOUT = 30.0
    REORDER_BUFFER_TIMEOUT = 2.0  # seconds to wait for out-of-order messages
    REORDER_BUFFER_MAX_SIZE = 50  # max buffered messages per pair before forced resync

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
        self._ws_request_id: int = 0  # JSON-RPC 2.0 request id counter
        self._stream_generation: int = 0
        self._resync_pending: Dict[str, bool] = {}
        self._resync_pending_since: Dict[str, float] = {}
        self._resync_failure_count: Dict[str, int] = {}
        self._last_diff_timestamp: Dict[str, float] = {}
        # Reorder buffer: handles out-of-order WS message delivery
        self._reorder_buffer: Dict[str, Dict[int, tuple]] = {}  # pair -> {sequence: (raw_message, timestamp)}
        self._reorder_timer_start: Dict[str, float] = {}  # pair -> when buffer started filling

    def _next_ws_id(self) -> int:
        """Returns the next JSON-RPC 2.0 request id."""
        self._ws_request_id += 1
        return self._ws_request_id

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
            "limit": str(CONSTANTS.ORDERBOOK_DEPTH)
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
                    "params": {"symbol": symbol},
                    "id": self._next_ws_id()
                }
                subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trade_payload)
                await ws.send(subscribe_trade_request)

                ob_payload = {
                    "method": CONSTANTS.WS_METHOD_SUBSCRIBE_ORDERBOOK,
                    "params": {"symbol": symbol, "limit": CONSTANTS.ORDERBOOK_DEPTH},
                    "id": self._next_ws_id()
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

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Suppresses the base class's timeout-driven REST snapshot injection.

        The base class fetches a REST snapshot after FULL_ORDER_BOOK_RESET_DELTA_SECONDS
        with no snapshot event. For NonKYC, REST and WS order book sequences are in different
        number spaces (~71x ratio), so injecting a REST snapshot into the tracker corrupts
        the local order book via restore_from_snapshot_and_diffs bisect_right mismatch.

        NonKYC handles all snapshot recovery via WS snapshotOrderbook messages.
        """
        message_queue = self._message_queue[self._snapshot_messages_queue_key]
        while True:
            try:
                snapshot_event = await message_queue.get()
                await self._parse_order_book_snapshot_message(raw_message=snapshot_event, message_queue=output)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error when processing public order book snapshots from exchange")

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "result" not in raw_message:
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
                symbol=raw_message.get("params", {}).get("symbol"))
            trade_messages = NonkycOrderBook.trade_messages_from_exchange(
                raw_message, {"trading_pair": trading_pair})
            for trade_message in trade_messages:
                message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        gen_before = self._stream_generation
        if "result" not in raw_message:
            params = raw_message.get("params", {})
            symbol = params.get("symbol")
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)

            if self._resync_pending.get(trading_pair, False):
                pending_since = self._resync_pending_since.get(trading_pair, 0)
                if pending_since > 0 and (time.time() - pending_since) > self.RESYNC_TIMEOUT:
                    self.logger().warning(
                        f"Orderbook resync for {trading_pair} timed out after "
                        f"{self.RESYNC_TIMEOUT}s. Forcing full WS reconnect."
                    )
                    self._resync_pending[trading_pair] = False
                    self._resync_pending_since.pop(trading_pair, None)
                    self._resync_failure_count[trading_pair] = 0
                    self._last_sequence.pop(trading_pair, None)
                    self._reorder_buffer.pop(trading_pair, None)
                    self._reorder_timer_start.pop(trading_pair, None)
                    self._stream_generation += 1
                    if self._ws_assistant is not None:
                        try:
                            await self._ws_assistant.disconnect()
                        except Exception:
                            self.logger().debug("Error disconnecting WS during resync timeout", exc_info=True)
                return

            sequence = int(params.get("sequence", 0))
            last_seq = self._last_sequence.get(trading_pair, 0)

            # Late arrival or duplicate — drop unconditionally (NKC-3). A message with
            # sequence <= last_seq can never fill a forward gap (gaps start at last_seq + 1);
            # inserting it into an active reorder buffer jams the buffer non-empty forever
            # and keeps the timeout timer armed, turning the next out-of-order message into
            # an instant spurious resync.
            if sequence <= last_seq:
                return

            # Contiguous — apply immediately
            if sequence == last_seq + 1 or last_seq == 0:
                if self._stream_generation != gen_before:
                    return
                self._last_sequence[trading_pair] = sequence
                self._last_diff_timestamp[trading_pair] = time.time()
                order_book_message: OrderBookMessage = NonkycOrderBook.diff_message_from_exchange(
                    raw_message, time.time(), {"trading_pair": trading_pair})
                message_queue.put_nowait(order_book_message)
                # Flush any buffered messages that are now contiguous
                await self._flush_reorder_buffer(trading_pair, message_queue)
                return

            # Forward gap — buffer and wait for out-of-order messages
            if trading_pair not in self._reorder_buffer:
                self._reorder_buffer[trading_pair] = {}
                self._reorder_timer_start[trading_pair] = time.time()
            self._reorder_buffer[trading_pair][sequence] = (raw_message, time.time())

            # Check buffer size cap
            if len(self._reorder_buffer[trading_pair]) > self.REORDER_BUFFER_MAX_SIZE:
                self.logger().warning(
                    f"ORDERBOOK_REORDER_BUFFER: pair={trading_pair} buffer size "
                    f"{len(self._reorder_buffer[trading_pair])} exceeds max "
                    f"{self.REORDER_BUFFER_MAX_SIZE}. Triggering resync."
                )
                self._reorder_buffer.pop(trading_pair, None)
                self._reorder_timer_start.pop(trading_pair, None)
                await self._handle_sequence_gap(trading_pair, last_seq, sequence)
                return

            # Check if buffer has timed out
            elapsed = time.time() - self._reorder_timer_start.get(trading_pair, time.time())
            if elapsed > self.REORDER_BUFFER_TIMEOUT:
                self.logger().warning(
                    f"ORDERBOOK_REORDER_TIMEOUT: pair={trading_pair} "
                    f"prev_seq={last_seq} buffered_seqs={sorted(self._reorder_buffer[trading_pair].keys())} "
                    f"elapsed={elapsed:.1f}s. Missing messages truly lost — triggering resync."
                )
                self._reorder_buffer.pop(trading_pair, None)
                self._reorder_timer_start.pop(trading_pair, None)
                await self._handle_sequence_gap(trading_pair, last_seq, sequence)
                return

    async def _flush_reorder_buffer(self, trading_pair: str, message_queue: asyncio.Queue):
        """Apply all contiguous messages from the reorder buffer."""
        buffer = self._reorder_buffer.get(trading_pair, {})
        if not buffer:
            return

        last_seq = self._last_sequence.get(trading_pair, 0)
        # NKC-3: purge stale entries (<= last_seq, e.g. left behind by a snapshot that advanced
        # the sequence) BEFORE the emptiness check, so a buffer holding only stale entries is
        # cleared and its timeout timer disarmed instead of jamming forever.
        stale_seqs = [seq for seq in buffer if seq <= last_seq]
        for seq in stale_seqs:
            buffer.pop(seq, None)
        applied = 0
        while last_seq + 1 in buffer:
            next_seq = last_seq + 1
            raw_msg, _ = buffer.pop(next_seq)
            order_book_message = NonkycOrderBook.diff_message_from_exchange(
                raw_msg, time.time(), {"trading_pair": trading_pair})
            message_queue.put_nowait(order_book_message)
            self._last_sequence[trading_pair] = next_seq
            self._last_diff_timestamp[trading_pair] = time.time()
            last_seq = next_seq
            applied += 1

        if applied > 0:
            self.logger().debug(
                f"NonKYC reorder buffer: flushed {applied} messages for {trading_pair}, "
                f"sequence now at {last_seq}"
            )

        # If buffer is now empty, clear timer
        if not buffer:
            self._reorder_buffer.pop(trading_pair, None)
            self._reorder_timer_start.pop(trading_pair, None)

    async def _handle_sequence_gap(self, trading_pair: str, expected_seq: int, received_seq: int):
        """
        Handle a detected sequence gap by resubscribing on the WebSocket.

        CRITICAL FIX: NonKYC's REST API /market/orderbook returns sequence numbers
        in a completely different number space than the WebSocket API. REST sequences
        are ~60-70x smaller. The old approach of fetching a REST snapshot caused an
        infinite resync loop. The fix: unsubscribe + resubscribe on the WS.
        """
        gap_size = received_seq - expected_seq - 1
        failure_count = self._resync_failure_count.get(trading_pair, 0)
        last_diff_ts = self._last_diff_timestamp.get(trading_pair, 0)
        time_since = time.time() - last_diff_ts if last_diff_ts > 0 else -1
        self.logger().warning(
            f"ORDERBOOK_GAP_EVENT: pair={trading_pair} "
            f"prev_seq={expected_seq} recv_seq={received_seq} gap={gap_size} "
            f"time_since_last={time_since:.1f}s gen={self._stream_generation} "
            f"resync_attempt={failure_count + 1}/{self.RESYNC_MAX_FAILURES} "
            f"action=ws_resubscribe"
        )

        self._resync_pending[trading_pair] = True
        self._resync_pending_since[trading_pair] = time.time()
        self._last_sequence.pop(trading_pair, None)

        try:
            if self._ws_assistant is None:
                raise RuntimeError("WebSocket not connected")

            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

            # Step 1: Unsubscribe from orderbook
            unsub_payload = {
                "method": CONSTANTS.WS_METHOD_UNSUBSCRIBE_ORDERBOOK,
                "params": {"symbol": symbol},
                "id": self._next_ws_id()
            }
            await self._ws_assistant.send(WSJSONRequest(payload=unsub_payload))

            # Brief pause to let the server process the unsubscribe
            await asyncio.sleep(0.2)

            # Step 2: Resubscribe — server will send a fresh snapshotOrderbook
            resub_payload = {
                "method": CONSTANTS.WS_METHOD_SUBSCRIBE_ORDERBOOK,
                "params": {"symbol": symbol, "limit": CONSTANTS.ORDERBOOK_DEPTH},
                "id": self._next_ws_id()
            }
            await self._ws_assistant.send(WSJSONRequest(payload=resub_payload))

            self.logger().info(
                f"Orderbook resync for {trading_pair}: WS resubscribe sent. "
                f"Awaiting fresh snapshot."
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._resync_failure_count[trading_pair] = failure_count + 1
            self._resync_pending[trading_pair] = False
            self._resync_pending_since.pop(trading_pair, None)

            if failure_count + 1 >= self.RESYNC_MAX_FAILURES:
                self.logger().warning(
                    f"Orderbook WS resync for {trading_pair} failed "
                    f"{failure_count + 1} times: {e}. Forcing full WS reconnect."
                )
                self._resync_failure_count[trading_pair] = 0
                self._last_sequence.pop(trading_pair, None)
                self._stream_generation += 1
                if self._ws_assistant is not None:
                    try:
                        await self._ws_assistant.disconnect()
                    except Exception:
                        self.logger().debug("Error disconnecting WS during max resync failure", exc_info=True)
            else:
                self.logger().warning(
                    f"Orderbook WS resync for {trading_pair} failed: {e}. "
                    f"Will retry on next gap."
                )
                self._last_sequence.pop(trading_pair, None)

    async def _parse_order_book_snapshot_message(self, raw_message, message_queue: asyncio.Queue):
        gen_before = self._stream_generation
        # Handle pre-parsed OrderBookMessage (from base class initial REST snapshot path)
        if isinstance(raw_message, OrderBookMessage):
            if self._stream_generation != gen_before:
                return
            # NOTE: The REST snapshot sequence is in a different number space than WS.
            # We intentionally do NOT set _last_sequence here for the REST path.
            # The first WS snapshotOrderbook will set _last_sequence correctly.
            message_queue.put_nowait(raw_message)
            return

        # WebSocket snapshot (snapshotOrderbook) — authoritative sequence source
        if "result" not in raw_message:
            params = raw_message.get("params", {})
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
                symbol=params.get("symbol"))
            sequence = int(params.get("sequence", 0))

            # Set the sequence from the WS snapshot — this is the correct number space
            self._last_sequence[trading_pair] = sequence

            snapshot_msg: OrderBookMessage = NonkycOrderBook.snapshot_message_from_exchange(
                params, time.time(), metadata={"trading_pair": trading_pair})

            if self._stream_generation != gen_before:
                return

            message_queue.put_nowait(snapshot_msg)

            # If this snapshot arrived as part of a resync, clear the pending flag
            if self._resync_pending.get(trading_pair, False):
                self._resync_pending[trading_pair] = False
                self._resync_pending_since.pop(trading_pair, None)
                self._resync_failure_count[trading_pair] = 0
                self.logger().info(
                    f"Orderbook resync for {trading_pair} completed via WS snapshot. "
                    f"New sequence base: {sequence}"
                )

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        self._stream_generation += 1
        self._last_sequence.clear()
        self._last_diff_timestamp.clear()
        self._resync_pending.clear()
        self._resync_pending_since.clear()
        self._resync_failure_count.clear()
        self._reorder_buffer.clear()
        self._reorder_timer_start.clear()
        self._drain_public_message_queues()
        await super()._on_order_stream_interruption(websocket_assistant=websocket_assistant)

    def _drain_public_message_queues(self):
        for key in [self._diff_messages_queue_key, self._snapshot_messages_queue_key]:
            q = self._message_queue.get(key)
            if q is not None:
                drained = 0
                while not q.empty():
                    try:
                        q.get_nowait()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break
                if drained > 0:
                    self.logger().info(f"Drained {drained} stale messages from {key} queue")

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
                "params": {"symbol": symbol},
                "id": self._next_ws_id()
            }
            await self._ws_assistant.send(WSJSONRequest(payload=trade_payload))

            ob_payload = {
                "method": CONSTANTS.WS_METHOD_SUBSCRIBE_ORDERBOOK,
                "params": {"symbol": symbol, "limit": CONSTANTS.ORDERBOOK_DEPTH},
                "id": self._next_ws_id()
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
                "params": {"symbol": symbol},
                "id": self._next_ws_id()
            }
            await self._ws_assistant.send(WSJSONRequest(payload=unsub_trades_payload))

            # Send unsubscribe for orderbook
            unsub_ob_payload = {
                "method": CONSTANTS.WS_METHOD_UNSUBSCRIBE_ORDERBOOK,
                "params": {"symbol": symbol},
                "id": self._next_ws_id()
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
