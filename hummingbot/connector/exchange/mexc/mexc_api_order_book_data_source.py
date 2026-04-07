import asyncio
import enum
import random
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.mexc import mexc_constants as CONSTANTS, mexc_web_utils as web_utils
from hummingbot.connector.exchange.mexc.mexc_order_book import MexcOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange


class _OBState(enum.Enum):
    """Order book sync state per trading pair."""
    BUFFERING = "BUFFERING"    # WS connected, caching diffs, waiting for snapshot
    BRIDGING = "BRIDGING"      # snapshot received, trying to bridge from cached diffs
    LIVE = "LIVE"              # bridge established, applying diffs with strict continuity
    RESYNCING = "RESYNCING"    # continuity lost, re-fetching snapshot (diffs still cached)


class MexcAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = 30.0
    TRADE_STREAM_ID = 1
    DIFF_STREAM_ID = 2
    ONE_HOUR = 60 * 60
    _DYNAMIC_SUBSCRIBE_ID_START = 100
    _next_subscribe_id: int = _DYNAMIC_SUBSCRIBE_ID_START

    # Order book continuity tracking constants
    SNAPSHOT_RESYNC_MAX_FAILURES = 5
    SNAPSHOT_RESYNC_INITIAL_DELAY = 2.0
    SNAPSHOT_RESYNC_BACKOFF_FACTOR = 2.0
    SNAPSHOT_RESYNC_MAX_DELAY = 60.0
    CACHED_DIFF_BUFFER_MAX_SIZE = 1000
    BRIDGING_TIMEOUT = 5.0  # seconds

    # Snapshot depth — MEXC documents 5000 for full local-book maintenance.
    SNAPSHOT_DEPTH = 5000

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'MexcExchange',
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.TRADE_EVENT_TYPE
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._domain = domain
        self._api_factory = api_factory

        # Version continuity tracking (per trading pair)
        self._last_to_version: Dict[str, int] = {}
        self._resync_pending: Dict[str, bool] = {}
        self._resync_failure_count: Dict[str, int] = {}
        self._resync_next_allowed_time: Dict[str, float] = {}
        # Snapshot bridge validation
        self._snapshot_version: Dict[str, int] = {}
        self._bridge_established: Dict[str, bool] = {}
        self._stream_generation: int = 0
        # Diff buffering state machine
        self._ob_state: Dict[str, _OBState] = {}
        self._cached_diffs: Dict[str, deque] = {}  # pair -> deque of (from_ver, to_ver, OrderBookMessage)
        self._bridging_start_time: Dict[str, float] = {}

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
        params = {
            "symbol": await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair),
            "limit": str(self.SNAPSHOT_DEPTH)
        }

        rest_assistant = await self._api_factory.get_rest_assistant()
        data = await rest_assistant.execute_request(
            url=web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self._domain),
            params=params,
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
            headers={"Content-Type": "application/json"}
        )

        return data

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            trade_params = []
            depth_params = []
            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                trade_params.append(f"{CONSTANTS.PUBLIC_TRADES_ENDPOINT_NAME}@100ms@{symbol}")
                depth_params.append(f"{CONSTANTS.PUBLIC_DIFF_ENDPOINT_NAME}@100ms@{symbol}")
            payload = {
                "method": "SUBSCRIPTION",
                "params": trade_params,
                "id": 1
            }
            subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=payload)

            payload = {
                "method": "SUBSCRIPTION",
                "params": depth_params,
                "id": 2
            }
            subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=payload)

            await ws.send(subscribe_trade_request)
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
        await ws.connect(ws_url=CONSTANTS.WSS_URL.format(self._domain),
                         ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL)
        return ws

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = MexcOrderBook.snapshot_message_from_exchange(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair}
        )
        # Store snapshot version for bridge validation
        self._snapshot_version[trading_pair] = snapshot_msg.update_id
        # Transition to BRIDGING — try to bridge from cached diffs
        self._ob_state[trading_pair] = _OBState.BRIDGING
        self._bridge_established[trading_pair] = False
        self._bridging_start_time[trading_pair] = time.time()
        return snapshot_msg

    def _get_ob_state(self, trading_pair: str) -> _OBState:
        return self._ob_state.get(trading_pair, _OBState.BUFFERING)

    def _cache_diff(self, trading_pair: str, from_version, to_version, order_book_message: OrderBookMessage):
        """Cache a diff message for later bridging. Enforces max buffer size."""
        if trading_pair not in self._cached_diffs:
            self._cached_diffs[trading_pair] = deque()
        buf = self._cached_diffs[trading_pair]
        buf.append((from_version, to_version, order_book_message))
        # Enforce max size — discard oldest if exceeded
        while len(buf) > self.CACHED_DIFF_BUFFER_MAX_SIZE:
            buf.popleft()

    def _try_bridge_from_cache(self, trading_pair: str, message_queue: asyncio.Queue) -> tuple:
        """
        Try to bridge from cached diffs after snapshot.
        Returns (bridged: bool, gap_detected: bool).
        """
        snapshot_ver = self._snapshot_version.get(trading_pair)
        if snapshot_ver is None:
            return (False, False)

        buf = self._cached_diffs.get(trading_pair, deque())

        # Discard diffs where toVersion <= lastUpdateId (MEXC docs step 5)
        while buf and buf[0][1] <= snapshot_ver:
            buf.popleft()

        if not buf:
            return (False, False)  # No diffs to bridge with yet

        first_from, first_to, first_msg = buf[0]
        # Check bridge condition: fromVersion <= lastUpdateId + 1
        if first_from is not None and first_from > snapshot_ver + 1:
            # Gap between snapshot and first cached diff — need newer snapshot
            self.logger().warning(
                f"MEXC bridge gap for {trading_pair}: "
                f"snapshot_ver={snapshot_ver}, first_from={first_from}, "
                f"first_to={first_to}, cached_diffs={len(buf)}"
            )
            return (False, True)

        # Bridge established! Apply all cached diffs sequentially
        self._bridge_established[trading_pair] = True
        self._ob_state[trading_pair] = _OBState.LIVE
        self._bridging_start_time.pop(trading_pair, None)
        applied = 0
        while buf:
            fv, tv, msg = buf.popleft()
            if tv <= snapshot_ver:
                continue  # skip stale
            self._last_to_version[trading_pair] = tv
            message_queue.put_nowait(msg)
            applied += 1

        self.logger().info(
            f"MEXC order book bridge established for {trading_pair}: "
            f"snapshot_ver={snapshot_ver}, applied {applied} cached diffs, "
            f"last_to_version={self._last_to_version.get(trading_pair)}"
        )
        # Clear the cache
        self._cached_diffs.pop(trading_pair, None)
        return (True, False)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "code" not in raw_message:
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=raw_message["symbol"])
            for single_msg in raw_message['publicAggreDeals']['deals']:
                trade_message = MexcOrderBook.trade_message_from_exchange(
                    single_msg, timestamp=float(single_msg['time']), metadata={"trading_pair": trading_pair})
                message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "code" in raw_message:
            return

        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
            symbol=raw_message["symbol"])

        order_book_message: OrderBookMessage = MexcOrderBook.diff_message_from_exchange(
            raw_message, timestamp=float(raw_message['sendTime']),
            metadata={"trading_pair": trading_pair})

        from_version = order_book_message.content.get("first_update_id")
        to_version = order_book_message.update_id

        state = self._get_ob_state(trading_pair)

        # BUFFERING or RESYNCING: cache the diff instead of dropping it
        if state in (_OBState.BUFFERING, _OBState.RESYNCING):
            self._cache_diff(trading_pair, from_version, to_version, order_book_message)

            # If RESYNCING, check if retry is due
            if state == _OBState.RESYNCING:
                now = time.time()
                next_allowed = self._resync_next_allowed_time.get(trading_pair, 0)
                if now >= next_allowed:
                    await self._attempt_resync(trading_pair, message_queue)
            return

        # BRIDGING: cache the diff and try to bridge
        if state == _OBState.BRIDGING:
            self._cache_diff(trading_pair, from_version, to_version, order_book_message)
            bridged, gap_detected = self._try_bridge_from_cache(trading_pair, message_queue)
            if gap_detected:
                # Gap detected — escalate to RESYNCING immediately
                await self._initiate_resync(trading_pair, from_version, to_version, order_book_message, message_queue)
            elif not bridged:
                # Check BRIDGING timeout
                bridging_start = self._bridging_start_time.get(trading_pair, 0)
                if bridging_start > 0 and (time.time() - bridging_start) > self.BRIDGING_TIMEOUT:
                    self.logger().warning(
                        f"MEXC BRIDGING timeout for {trading_pair} "
                        f"({time.time() - bridging_start:.1f}s). Forcing resync."
                    )
                    await self._initiate_resync(trading_pair, message_queue=message_queue)
            return

        # LIVE: enforce strict continuity
        last_to = self._last_to_version.get(trading_pair)

        # If no continuity baseline yet, accept
        if last_to is None:
            self._last_to_version[trading_pair] = to_version
            message_queue.put_nowait(order_book_message)
            return

        # Check continuity: fromVersion must equal last_toVersion + 1
        if from_version is not None and from_version != last_to + 1:
            if to_version <= last_to:
                # Stale/duplicate diff — just drop it
                return

            self.logger().warning(
                f"MEXC order book version gap for {trading_pair}: "
                f"expected fromVersion={last_to + 1}, got {from_version}. "
                f"Triggering REST snapshot resync."
            )
            await self._initiate_resync(trading_pair, from_version, to_version, order_book_message, message_queue)
            return

        # Continuity is valid — accept the diff and update tracking
        self._last_to_version[trading_pair] = to_version
        message_queue.put_nowait(order_book_message)

    async def _initiate_resync(self, trading_pair: str,
                               from_version=None, to_version=None,
                               order_book_message: Optional[OrderBookMessage] = None,
                               message_queue: Optional[asyncio.Queue] = None):
        """Mark a pair as needing resync and attempt the first snapshot fetch."""
        self._ob_state[trading_pair] = _OBState.RESYNCING
        self._resync_pending[trading_pair] = True
        self._bridge_established[trading_pair] = False
        # Start caching diffs from the one that triggered resync
        if order_book_message is not None:
            self._cache_diff(trading_pair, from_version, to_version, order_book_message)
        await self._attempt_resync(trading_pair, message_queue)

    async def _attempt_resync(self, trading_pair: str, message_queue: Optional[asyncio.Queue] = None):
        """
        Attempt to fetch a fresh REST snapshot and apply it.
        On failure, apply exponential backoff and keep resync_pending=True
        so diffs continue to be cached.
        On max failures, trigger a full websocket reconnect.
        """
        failure_count = self._resync_failure_count.get(trading_pair, 0)

        # Check if max failures exceeded — trigger reconnect via explicit disconnect
        if failure_count >= self.SNAPSHOT_RESYNC_MAX_FAILURES:
            self.logger().warning(
                f"MEXC order book resync for {trading_pair} failed "
                f"{failure_count} consecutive times. Forcing WebSocket disconnect."
            )
            self._resync_failure_count[trading_pair] = 0
            self._resync_pending[trading_pair] = False
            self._cached_diffs.pop(trading_pair, None)
            if self._ws_assistant is not None:
                try:
                    await self._ws_assistant.disconnect()
                except Exception:
                    self.logger().debug("Error disconnecting WS during max resync", exc_info=True)
            return  # listen_for_subscriptions will handle reconnect

        self.logger().info(
            f"MEXC order book resync for {trading_pair} "
            f"(attempt {failure_count + 1}/{self.SNAPSHOT_RESYNC_MAX_FAILURES})"
        )

        try:
            snapshot_msg = await self._order_book_snapshot(trading_pair)
            snapshot_queue = self._message_queue[self._snapshot_messages_queue_key]
            snapshot_queue.put_nowait(snapshot_msg)

            # Try to bridge from cached diffs
            if message_queue is not None:
                bridged, gap_detected = self._try_bridge_from_cache(trading_pair, message_queue)
            else:
                bridged, gap_detected = False, False

            if bridged:
                # True success — bridge established, now LIVE
                self._resync_failure_count[trading_pair] = 0
                self._resync_next_allowed_time[trading_pair] = 0
                self._resync_pending[trading_pair] = False
                self._bridging_start_time.pop(trading_pair, None)
                self.logger().info(
                    f"MEXC order book resync for {trading_pair} succeeded. "
                    f"Snapshot version: {snapshot_msg.update_id}, state=LIVE"
                )
            else:
                # Snapshot fetched but no cached diffs available to bridge yet.
                # This is NOT a failure — transition to BRIDGING and wait for
                # new diffs to arrive that are contiguous with this snapshot.
                # The BRIDGING handler in _parse_order_book_diff_message will
                # attempt bridging as each new diff arrives.
                self._ob_state[trading_pair] = _OBState.BRIDGING
                self._bridging_start_time[trading_pair] = time.time()
                # Do NOT increment failure count — this is expected behavior.
                # Reset failure count since we got a valid snapshot.
                self._resync_failure_count[trading_pair] = 0
                self._resync_pending[trading_pair] = False
                self.logger().info(
                    f"MEXC order book resync for {trading_pair}: snapshot fetched "
                    f"(ver={snapshot_msg.update_id}). Transitioning to BRIDGING, "
                    f"waiting for contiguous diffs."
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Snapshot failed — apply exponential backoff
            # Keep resync_pending=True so diffs are cached
            self._resync_failure_count[trading_pair] = failure_count + 1
            delay = min(
                self.SNAPSHOT_RESYNC_INITIAL_DELAY * (self.SNAPSHOT_RESYNC_BACKOFF_FACTOR ** failure_count),
                self.SNAPSHOT_RESYNC_MAX_DELAY
            )
            jitter = delay * random.uniform(0, 0.25)
            self._resync_next_allowed_time[trading_pair] = time.time() + delay + jitter

            self.logger().warning(
                f"MEXC order book resync for {trading_pair} failed "
                f"(attempt {failure_count + 1}): {e}. "
                f"Next retry in {delay + jitter:.1f}s."
            )

    async def _parse_order_book_snapshot_message(self, raw_message, message_queue: asyncio.Queue):
        """
        Handle snapshot messages. Supports both:
        - Pre-parsed OrderBookMessage objects from the resync path
        - Raw dict messages from the normal REST snapshot pipeline
        """
        if isinstance(raw_message, OrderBookMessage):
            message_queue.put_nowait(raw_message)
        else:
            snapshot_msg = MexcOrderBook.snapshot_message_from_exchange(
                raw_message, time.time(),
                metadata={"trading_pair": raw_message.get("trading_pair", "")})
            message_queue.put_nowait(snapshot_msg)

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        """Clear all continuity tracking state on websocket interruption."""
        self._stream_generation += 1
        self._last_to_version.clear()
        self._resync_pending.clear()
        self._resync_failure_count.clear()
        self._resync_next_allowed_time.clear()
        self._snapshot_version.clear()
        self._bridge_established.clear()
        self._ob_state.clear()
        self._cached_diffs.clear()
        self._bridging_start_time.clear()
        await super()._on_order_stream_interruption(websocket_assistant=websocket_assistant)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = ""
        if "code" not in event_message:
            event_type = event_message.get("channel", "")
            channel = (self._diff_messages_queue_key if CONSTANTS.DIFF_EVENT_TYPE in event_type
                       else self._trade_messages_queue_key)
        return channel

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
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

            trade_payload = {
                "method": "SUBSCRIPTION",
                "params": [f"{CONSTANTS.PUBLIC_TRADES_ENDPOINT_NAME}@100ms@{symbol}"],
                "id": self._get_next_subscribe_id()
            }
            subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trade_payload)

            depth_payload = {
                "method": "SUBSCRIPTION",
                "params": [f"{CONSTANTS.PUBLIC_DIFF_ENDPOINT_NAME}@100ms@{symbol}"],
                "id": self._get_next_subscribe_id()
            }
            subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=depth_payload)

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
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

            trade_payload = {
                "method": "UNSUBSCRIPTION",
                "params": [f"{CONSTANTS.PUBLIC_TRADES_ENDPOINT_NAME}@100ms@{symbol}"],
                "id": self._get_next_subscribe_id()
            }
            unsubscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trade_payload)

            depth_payload = {
                "method": "UNSUBSCRIPTION",
                "params": [f"{CONSTANTS.PUBLIC_DIFF_ENDPOINT_NAME}@100ms@{symbol}"],
                "id": self._get_next_subscribe_id()
            }
            unsubscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=depth_payload)

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

    @classmethod
    def _get_next_subscribe_id(cls) -> int:
        """Returns the next subscription ID and increments the counter."""
        current_id = cls._next_subscribe_id
        cls._next_subscribe_id += 1
        return current_id
