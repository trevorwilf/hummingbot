import asyncio
import logging
import re
import time
from collections import OrderedDict
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from async_timeout import timeout
from bidict import ValueDuplicationError, bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.nonkyc import (
    nonkyc_constants as CONSTANTS,
    nonkyc_utils,
    nonkyc_web_utils as web_utils,
)
from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import NonkycAPIOrderBookDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import NonkycAPIUserStreamDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import TradeFillOrderDetails, combine_to_hb_trading_pair, split_hb_trading_pair
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class NonkycExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    # Cap of the persisted processed-trade-id dedupe set (FIFO eviction of the oldest ids).
    _PROCESSED_TRADE_IDS_MAX = 5000
    # Reserved key under which the dedupe set is persisted inside tracking_states.
    _PROCESSED_TRADE_IDS_STATE_KEY = "__nonkyc_processed_trade_ids__"
    ENABLE_BALANCE_WS = True  # Undocumented WS methods (subscribeBalances/currentBalances/balanceUpdate).
                               # Confirmed working 2026-03-29. Auto-disables if no response within 60s.
                               # unsubscribeBalances does NOT exist (404). Do not attempt to unsubscribe.
    # Sentinel stored as exchange_order_id when createorder returns an ambiguous 503 (the order may
    # exist server-side). It is never a real NonKYC id and must never be sent to the API as one.
    UNKNOWN_EXCHANGE_ORDER_ID = "UNKNOWN"
    # NKC-9: unknown status strings coerce to OPEN (fail-open on state) but must be loud in logs.
    _UNKNOWN_STATUS_WARN_INTERVAL_S = 30.0

    web_utils = web_utils

    def __init__(self,
                 nonkyc_api_key: str,
                 nonkyc_api_secret: str,
                 balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
                 rate_limits_share_pct: Decimal = Decimal("100"),
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 cancel_exchange_orphans: bool = False,
                 ):
        self.api_key = nonkyc_api_key
        self.secret_key = nonkyc_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._cancel_exchange_orphans = cancel_exchange_orphans
        self._last_trades_poll_nonkyc_timestamp = 1.0
        self._bulk_fills_fetched_this_cycle: bool = False
        self._balance_ws_confirmed: bool = False
        self._balance_ws_subscription_time: Optional[float] = None
        BALANCE_WS_HEALTH_TIMEOUT = 60.0
        self._trading_fees: Dict[str, Decimal] = {}
        self._trading_fees_last_computed: float = 0.0
        self._trading_fees_ttl: float = 3600.0  # 1 hour cache TTL
        # Bulk /tickers snapshot cache (2026-07-13): hummingbot-api's ticker pool asks for
        # the last price of EVERY listed pair every 30s; served per-pair that was ~400
        # concurrent GET /ticker/{symbol} calls and an instant, total 429 storm. One
        # short-TTL snapshot serves any number of pairs; the lock makes it single-flight.
        self._tickers_snapshot_cache: Optional[Dict[str, float]] = None
        self._tickers_snapshot_ts: float = 0.0
        self._tickers_snapshot_lock: asyncio.Lock = asyncio.Lock()
        self._pre_adjusted_assets: Dict[str, float] = {}  # asset -> timestamp of last pre-adjust
        self._ws_reconnect_count: int = 0
        self._ws_reconnect_count_since_log: int = 0
        self._last_balance_health_log: float = 0.0
        self._api_latency_samples: Dict[str, list] = {}  # endpoint -> list of recent latencies (ms)
        self._api_latency_max_samples: int = 100
        self._error_counters: Dict[str, int] = {
            "order_reject_insufficient": 0,
            "order_reject_other": 0,
            "rest_5xx": 0,
            "rest_timeout": 0,
            "ws_reconnect": 0,
            "balance_poll_failure": 0,
        }
        self._error_counters_last_reset: float = time.time()
        self._balance_settling: bool = False
        self._balance_settle_start: float = 0.0
        self._BALANCE_SETTLE_TIMEOUT: float = 15.0
        # NonKYC has returned successful but incomplete /balances snapshots in production
        # (for example, omitting a non-zero USDT row for one poll). Treating an omitted row as
        # zero can make shared-account controllers permanently write down their ledgers. Keep
        # an incomplete snapshot quarantined until REST explicitly reports every previously
        # non-zero omitted asset again. Unlike ordinary reconnect settling, this condition must
        # never fail open merely because the short reconnect timeout elapsed.
        self._balance_snapshot_incomplete: bool = False
        self._missing_nonzero_balance_assets: set = set()
        self._last_incomplete_balance_warn: float = 0.0
        self._orders_reconciled_after_reconnect: bool = True
        # Orphan exchange-order ids already warned about: a persistent orphan (e.g. an order
        # left by a previous session) otherwise re-warns at EVERY reconnect reconciliation.
        self._warned_orphan_ids: set = set()
        self._nonce_error_cooldown_until: float = 0.0
        self._last_server_disconnect_time: float = 0.0
        self._SERVER_DISCONNECT_BACKOFF: float = 10.0
        self._balance_recheck_in_progress: bool = False
        # Persisted, bounded FIFO dedupe of already-processed exchange trade ids. /account/trades
        # returns the GLOBAL account trade list (the symbol filter is ignored), so the reconciliation
        # path can otherwise see and re-process the same trade many times across pairs and poll cycles.
        # Persisted via tracking_states so it survives restarts.
        self._processed_trade_ids: "OrderedDict[str, None]" = OrderedDict()
        # NKC-9: rate-limit state for unknown-order-status warnings, and per-order consecutive
        # unknown REST status counts (repeated unknowns trigger a reconciliation log).
        self._unknown_status_last_warn: Dict[str, float] = {}
        self._unknown_status_counts: Dict[str, int] = {}
        # NKC-2: rate-limit state for the catch-all WS error-frame warning.
        self._ws_error_frame_last_warn: float = 0.0
        # Read-only accounting of balance held by untracked ("orphan" = the operator's manual)
        # active exchange orders, per trading pair: {pair: {"quote": Decimal, "base": Decimal}}.
        # Refreshed by the post-reconnect reconciliation snapshot and by the subscribeReports
        # ack snapshot. Feeds the ladder understatement check (LOG-2'); never used to adopt,
        # track or cancel those orders.
        self._external_order_holds: Dict[str, Dict[str, Decimal]] = {}
        super().__init__(balance_asset_limit, rate_limits_share_pct)
        self.logger().info(
            "NonKYC connector supports LIMIT and MARKET order types. "
            "Post-only/maker-only (LIMIT_MAKER) orders are not supported by this exchange."
        )

    @property
    def balance_data_source(self) -> str:
        """Returns the current balance data source for operational visibility."""
        if not self.ENABLE_BALANCE_WS:
            return "rest_only"
        if self._balance_ws_confirmed:
            return "websocket_confirmed"
        if self._balance_ws_subscription_time is not None:
            return "websocket_pending"
        return "rest_fallback"

    def _reset_balance_ws_state(self):
        """
        Reset balance WebSocket confirmation state for a new session.
        Called on WS reconnect to ensure the watchdog can re-evaluate.
        """
        was_confirmed = self._balance_ws_confirmed
        self._balance_ws_confirmed = False
        self._balance_ws_subscription_time = time.time()
        if was_confirmed:
            self.logger().info(
                "Balance WS state reset for new session — awaiting re-confirmation"
            )
        self._enter_balance_settling()

    def _enter_balance_settling(self):
        self._balance_settling = True
        self._balance_settle_start = time.time()
        self.logger().info("Balance settling: ACTIVE — order creation paused until REST balance sync")
        self._emit_structured_event("balance_settling_entered", {
            "reason": "ws_reconnect",
        })

    def _exit_balance_settling(self):
        if self._balance_settling:
            if getattr(self, "_balance_snapshot_incomplete", False):
                self.logger().debug(
                    "Balance settling: REST snapshot is still incomplete; keeping order creation paused"
                )
                return
            # Wait for both balances AND active orders reconciliation
            if not self._orders_reconciled_after_reconnect:
                self.logger().debug("Balance settling: balances refreshed but orders not yet reconciled")
                return
            elapsed = time.time() - self._balance_settle_start
            self._balance_settling = False
            self.logger().info(f"Balance settling: RESOLVED after {elapsed:.1f}s — order creation resumed")
            self._emit_structured_event("balance_settling_exited", {
                "duration_s": round(elapsed, 1),
            })

    @property
    def is_balance_settling(self) -> bool:
        """True while a post-reconnect / post-fill REST balance sync is still in flight. Controllers
        should DEFER ledger/wallet over-claim reconciliation while this is True, since the cached
        wallet balance can be transiently stale-low and produce false over-claim positives."""
        return self._balance_settling

    # --- Persisted, bounded dedupe of processed exchange trade ids -------------------------------

    def _is_trade_processed(self, trade_id: str) -> bool:
        return str(trade_id) in self._processed_trade_ids

    def _mark_trade_processed(self, trade_id: str) -> None:
        tid = str(trade_id)
        if tid in self._processed_trade_ids:
            return
        self._processed_trade_ids[tid] = None
        while len(self._processed_trade_ids) > self._PROCESSED_TRADE_IDS_MAX:
            self._processed_trade_ids.popitem(last=False)  # FIFO: evict oldest

    @property
    def tracking_states(self) -> Dict[str, Any]:
        # Persist the dedupe set alongside the in-flight order states so it survives restarts.
        states = super().tracking_states
        states[self._PROCESSED_TRADE_IDS_STATE_KEY] = list(self._processed_trade_ids.keys())
        return states

    def restore_tracking_states(self, saved_states: Dict[str, Any]):
        processed = None
        if isinstance(saved_states, dict) and self._PROCESSED_TRADE_IDS_STATE_KEY in saved_states:
            # Copy + strip the reserved key so the order tracker only ever sees order states.
            saved_states = dict(saved_states)
            processed = saved_states.pop(self._PROCESSED_TRADE_IDS_STATE_KEY, None)
        super().restore_tracking_states(saved_states)
        if processed:
            for tid in processed:
                self._mark_trade_processed(str(tid))

    async def _safe_resolve_trading_pair(self, trade: Dict[str, Any]) -> Optional[str]:
        """Resolve a trade record's actual hb trading pair from its ``market.symbol`` (never from the
        loop/poll pair). Returns None if the symbol is missing or unknown to the symbol map."""
        symbol = None
        market = trade.get("market")
        if isinstance(market, dict):
            symbol = market.get("symbol")
        symbol = symbol or trade.get("symbol")
        if not symbol:
            return None
        try:
            return await self.trading_pair_associated_to_exchange_symbol(symbol=str(symbol))
        except Exception:
            return None

    async def _reconcile_active_orders_after_reconnect(self):
        """Fetch active orders from exchange after reconnect and reconcile with tracked orders."""
        try:
            exchange_orders = await self._api_get(
                path_url=CONSTANTS.ACCOUNT_ORDERS_PATH_URL,
                params={"status": "active"},
                is_auth_required=True,
            )
            if not isinstance(exchange_orders, list):
                self.logger().warning(f"Unexpected active orders response type: {type(exchange_orders)}")
                return

            exchange_order_ids = set()
            for eo in exchange_orders:
                eo_id = str(eo.get("id", ""))
                if eo_id:
                    exchange_order_ids.add(eo_id)

            tracked = self._order_tracker.active_orders
            tracked_exchange_ids = set()
            for o in tracked.values():
                if o.exchange_order_id:
                    tracked_exchange_ids.add(o.exchange_order_id)

            orphans = exchange_order_ids - tracked_exchange_ids
            missing = tracked_exchange_ids - exchange_order_ids

            if orphans:
                new_orphans = orphans - self._warned_orphan_ids
                if new_orphans:
                    self.logger().warning(
                        f"Post-reconnect reconciliation: {len(orphans)} exchange orders "
                        f"not tracked locally (orphans): {orphans}. These hold balance on the "
                        "exchange; cancel manually if they are not intentional."
                    )
                else:
                    self.logger().debug(
                        f"Post-reconnect reconciliation: {len(orphans)} known orphan(s) still "
                        f"active on exchange: {orphans}"
                    )
            # Replace (not update) so an orphan that resolves and later reappears warns again.
            self._warned_orphan_ids = set(orphans)
            # Refresh the read-only external-holds accounting from this reconciliation snapshot.
            await self._update_external_holds_from_order_snapshot(exchange_orders)
            if missing:
                self.logger().warning(
                    f"Post-reconnect reconciliation: {len(missing)} tracked orders "
                    f"not found on exchange (may have filled/cancelled during disconnect): {missing}"
                )
                for client_id, order in tracked.items():
                    if order.exchange_order_id in missing:
                        try:
                            await self._request_order_status(order)
                        except Exception as e:
                            self.logger().debug(f"Status check for {client_id} failed: {repr(e)}")

            self._emit_structured_event("post_reconnect_order_reconciliation", {
                "exchange_active": len(exchange_order_ids),
                "tracked_active": len(tracked_exchange_ids),
                "orphans": len(orphans),
                "missing_from_exchange": len(missing),
            })
        except Exception as e:
            self.logger().warning(f"Post-reconnect order reconciliation failed: {repr(e)}")
        finally:
            self._orders_reconciled_after_reconnect = True
            # Now try to exit balance settling (balances may already be refreshed)
            self._exit_balance_settling()

    def external_order_holds(self, trading_pair: str) -> Dict[str, Decimal]:
        """Balance held by untracked (manual/external) active exchange orders for the pair.
        Returns {"quote": Decimal, "base": Decimal}; zeros when no snapshot has run or no
        external orders exist. Read-only accounting — feeds the ladder understatement check."""
        holds = self._external_order_holds.get(trading_pair)
        if holds is None:
            return {"quote": Decimal("0"), "base": Decimal("0")}
        return dict(holds)

    async def _update_external_holds_from_order_snapshot(self, orders: List[Dict[str, Any]]):
        """Recompute `_external_order_holds` from a full open-orders snapshot (the REST
        post-reconnect reconciliation response, or the subscribeReports ack `result`).

        For each ACTIVE order not tracked locally (matched by neither exchange id nor
        client/userProvidedId — the latter covers UNKNOWN-sentinel orders):
          BUY:  quote hold = price × (quantity − executedQuantity)
          SELL: base hold  = remaining quantity
        The dict is REPLACED wholesale so holds of orders that resolved since the last
        snapshot are cleared. NO cancellation, NO tracking-adoption."""
        if not isinstance(orders, list):
            return
        tracked = self._order_tracker.active_orders
        tracked_exchange_ids = {o.exchange_order_id for o in tracked.values() if o.exchange_order_id}
        tracked_client_ids = set(tracked.keys())
        holds: Dict[str, Dict[str, Decimal]] = {}
        for eo in orders:
            if not isinstance(eo, dict):
                continue
            try:
                if eo.get("isActive") is False:
                    continue
                eo_id = str(eo.get("id") or "")
                client_id = str(eo.get("userProvidedId") or "")
                if (eo_id and eo_id in tracked_exchange_ids) or (client_id and client_id in tracked_client_ids):
                    continue
                symbol = eo.get("symbol")
                if not symbol and isinstance(eo.get("market"), dict):
                    symbol = eo["market"].get("symbol")
                if not symbol:
                    continue
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=str(symbol))
                quantity = Decimal(str(eo.get("quantity", "0")))
                executed = Decimal(str(eo.get("executedQuantity") or "0"))
                remaining = quantity - executed
                if remaining <= 0:
                    continue
                pair_holds = holds.setdefault(
                    trading_pair, {"quote": Decimal("0"), "base": Decimal("0")})
                side = str(eo.get("side", "")).lower()
                if side == "buy":
                    price = Decimal(str(eo.get("price", "0")))
                    pair_holds["quote"] += price * remaining
                elif side == "sell":
                    pair_holds["base"] += remaining
            except (KeyError, InvalidOperation, TypeError, ValueError):
                self.logger().debug(
                    f"Skipping malformed order in external-holds snapshot: {eo}", exc_info=True)
                continue
        self._external_order_holds = holds

    def _on_nonce_error_detected(self):
        """Set a short cooldown on private REST requests after nonce error."""
        self._nonce_error_cooldown_until = time.time() + 2.0
        self.logger().warning("Nonce error detected — private REST cooldown for 2s")
        self._emit_structured_event("nonce_error_cooldown_started", {
            "cooldown_s": 2.0,
        })

    def _emit_structured_event(self, event_type: str, payload: dict):
        """Emit a structured JSON event to the forensic log and JSONL ledger."""
        try:
            from hummingbot.logger.structured_event_logger import get_structured_logger
            get_structured_logger().emit(event_type, connector="nonkyc", **payload)
        except Exception:
            pass
        try:
            import json
            import logging as _logging
            event = {
                "event_type": event_type,
                "connector": "nonkyc",
                "timestamp_ms": int(time.time() * 1e3),
                **payload
            }
            json_str = json.dumps(event)
            self.logger().info(f"[STRUCTURED_EVENT] {json_str}")
            _logging.getLogger("hummingbot.structured_events").info(json_str)
        except Exception:
            pass

    def _record_api_latency(self, endpoint: str, latency_ms: float):
        if endpoint not in self._api_latency_samples:
            self._api_latency_samples[endpoint] = []
        samples = self._api_latency_samples[endpoint]
        samples.append(latency_ms)
        if len(samples) > self._api_latency_max_samples:
            samples.pop(0)

    def _increment_error(self, category: str):
        if category in self._error_counters:
            self._error_counters[category] += 1

    def _get_and_reset_error_summary(self) -> str:
        elapsed = time.time() - self._error_counters_last_reset
        non_zero = {k: v for k, v in self._error_counters.items() if v > 0}
        summary = f"Errors ({elapsed:.0f}s window): "
        if non_zero:
            summary += ", ".join(f"{k}={v}" for k, v in non_zero.items())
        else:
            summary += "none"
        for k in self._error_counters:
            self._error_counters[k] = 0
        self._error_counters_last_reset = time.time()
        return summary

    def _log_order_lifecycle(self, order_id: str, stage: str, details: str = ""):
        """Structured order lifecycle log for easy tracing."""
        self.logger().info(f"[ORDER {order_id}] {stage}{' — ' + details if details else ''}")

    @staticmethod
    def nonkyc_order_type(order_type: OrderType) -> str:
        """
        Map Hummingbot OrderType to NonKYC API type string.

        NonKYC does not support a native post-only/maker-only order type.
        LIMIT_MAKER is rejected with ValueError since it would silently
        convert to a regular limit order that can cross the spread.
        """
        if order_type == OrderType.LIMIT_MAKER:
            raise ValueError(
                "NonKYC does not support LIMIT_MAKER (post-only) orders. "
                "Use OrderType.LIMIT instead. Note: limit orders on NonKYC may take liquidity."
            )
        return order_type.name.lower()

    @staticmethod
    def to_hb_order_type(nonkyc_type: str) -> OrderType:
        """Convert NonKYC order type to Hummingbot OrderType. Defaults to LIMIT for unknown types."""
        try:
            return OrderType[nonkyc_type.upper()]
        except KeyError:
            logging.getLogger(__name__).warning(
                f"Unknown NonKYC order type '{nonkyc_type}', defaulting to LIMIT"
            )
            return OrderType.LIMIT

    @property
    def authenticator(self):
        return NonkycAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer)

    @property
    def name(self) -> str:
        return "nonkyc"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return self._domain

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.MARKETS_INFO_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.MARKETS_INFO_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.PING_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def real_time_balance_update(self) -> bool:
        return True

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    async def get_all_pairs_prices(self) -> List[Dict[str, str]]:
        pairs_prices = await self._api_get(path_url=CONSTANTS.TICKER_BOOK_PATH_URL)
        return pairs_prices

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        error_description = str(request_exception).lower()
        # A request TIMEOUT is a latency problem, not a clock/nonce problem. The exclusion is
        # checked FIRST: "timeout"/"timed out" contain the substring "time" and previously
        # triggered the 2s private-REST nonce cooldown plus a time-sync retry on every REST
        # timeout -- compounding latency exactly during exchange slowdowns.
        if "timeout" in error_description or "timed out" in error_description:
            return False
        is_time_related = (
            "nonce" in error_description
            or "timestamp" in error_description
            or "clock" in error_description
            or re.search(r"\btime\b", error_description) is not None
        )
        if is_time_related:
            self._on_nonce_error_detected()
        return is_time_related

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return str(CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE) in str(
            status_update_exception
        ) and CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return str(CONSTANTS.UNKNOWN_ORDER_ERROR_CODE) in str(
            cancelation_exception
        ) and CONSTANTS.UNKNOWN_ORDER_MESSAGE in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return NonkycAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return NonkycAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or False
        fee_pct = None
        if self._trading_fees:
            fee_key = "maker_fee" if is_maker else "taker_fee"
            fee_pct = self._trading_fees.get(fee_key)
        if fee_pct is None:
            fee_pct = self.estimate_fee_pct(is_maker)
        # BUY fees are charged in quote (added to cost); SELL fees are deducted from returns
        if order_side == TradeType.BUY:
            return AddedToCostTradeFee(percent=fee_pct)
        return DeductedFromReturnsTradeFee(percent=fee_pct)

    @staticmethod
    def _extract_fee_token_and_amount(trade_data: Dict[str, Any], quote_asset: str) -> Tuple[str, Decimal]:
        """Extract fee token and amount, respecting alternateFeeAsset if present."""
        alt_asset = trade_data.get("alternateFeeAsset")
        if alt_asset:
            return alt_asset, Decimal(str(trade_data.get("alternateFee", "0")))
        # WS reports use 'tradeFee', REST uses 'fee'
        fee_amount = trade_data.get("fee") or trade_data.get("tradeFee", "0")
        return quote_asset, Decimal(str(fee_amount))

    # NOTE: The connector reports accurate available/held balances. However, position
    # executors that create exit orders (take-profit/stop-loss) must size them from
    # connector-reported *spendable free* balance, not the nominal filled quantity.
    # See RCA report 2026-03-26 Issue 5 for details.
    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:
        order_result = None
        amount_str = f"{amount:f}"
        type_str = NonkycExchange.nonkyc_order_type(order_type)
        side_str = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        api_params = {"symbol": symbol,
                      "side": side_str,
                      "quantity": amount_str,
                      "type": type_str,
                      "userProvidedId": order_id}
        if order_type is OrderType.LIMIT:
            price_str = f"{price:f}"
            api_params["price"] = price_str

        t_start = time.monotonic()
        try:
            order_result = await self._api_post(
                path_url=CONSTANTS.CREATE_ORDER_PATH_URL,
                data=api_params,
                is_auth_required=True)
            t_elapsed = (time.monotonic() - t_start) * 1000
            o_id = str(order_result["id"])
            transact_time = order_result["createdAt"] * 1e-3
            self._record_api_latency("createorder", t_elapsed)
            self.logger().debug(
                f"REST latency: createorder {trading_pair} {trade_type.name} -> "
                f"{t_elapsed:.0f}ms (id={o_id})"
            )
            # 2500ms: NonKYC createorder averages 771-1329ms with normal peaks to ~2549ms;
            # the previous 2000ms threshold flagged routine traffic.
            if t_elapsed > 2500:
                self.logger().warning(
                    f"REST SLOW: createorder {trading_pair} took {t_elapsed:.0f}ms "
                    f"(threshold: 2500ms)"
                )
            self._log_order_lifecycle(order_id, "PLACED",
                f"{trade_type.name} {amount} {trading_pair} @ {price} -> exch_id={o_id}")
            try:
                from hummingbot.logger.structured_event_logger import get_structured_logger
                get_structured_logger().emit("order_submit_requested",
                    connector="nonkyc",
                    client_order_id=order_id,
                    trading_pair=trading_pair,
                    trade_type=trade_type.name,
                    order_type=order_type.name,
                    price=str(price),
                    amount=str(amount),
                    exchange_order_id=o_id,
                )
            except Exception:
                pass
        except IOError as e:
            t_elapsed = (time.monotonic() - t_start) * 1000
            self._record_api_latency("createorder", t_elapsed)
            error_description = str(e)
            is_server_overloaded = ("503" in error_description
                                    and "Unknown error, please check your request or try again later." in error_description)
            if is_server_overloaded:
                o_id = self.UNKNOWN_EXCHANGE_ORDER_ID
                transact_time = self._time_synchronizer.time()
            else:
                raise
        return o_id, transact_time

    async def _place_order_and_process_update(self, order: InFlightOrder, **kwargs) -> str:
        """
        Override to locally pre-adjust available balance immediately after order
        placement succeeds, closing the ~1s race window before the WS balanceUpdate
        arrives. All controllers reading available_balances will see the adjusted
        value immediately. The WS balanceUpdate overwrites with the real exchange
        value when it arrives.
        """
        # Nonce cooldown gate
        if time.time() < self._nonce_error_cooldown_until:
            remaining = self._nonce_error_cooldown_until - time.time()
            self.logger().debug(f"Nonce cooldown: waiting {remaining:.1f}s before order placement")
            await asyncio.sleep(remaining)
        # Server disconnect backoff
        server_disconnect_age = time.time() - self._last_server_disconnect_time
        if server_disconnect_age < self._SERVER_DISCONNECT_BACKOFF:
            remaining = self._SERVER_DISCONNECT_BACKOFF - server_disconnect_age
            self.logger().info(
                f"Order creation deferred: server disconnect {server_disconnect_age:.1f}s ago, "
                f"backing off for {remaining:.1f}s more"
            )
            raise Exception(
                f"Order creation backed off: ServerDisconnectedError {server_disconnect_age:.1f}s ago"
            )
        if self._balance_settling:
            elapsed = time.time() - self._balance_settle_start
            if getattr(self, "_balance_snapshot_incomplete", False):
                missing_assets = sorted(getattr(self, "_missing_nonzero_balance_assets", set()))
                raise Exception(
                    f"Order creation blocked: incomplete REST balance snapshot omitted previously "
                    f"non-zero asset(s) {missing_assets}. Waiting for a complete REST balance sync."
                )
            elif elapsed > self._BALANCE_SETTLE_TIMEOUT:
                self.logger().warning(
                    f"Balance settling: TIMEOUT after {elapsed:.1f}s — allowing order creation")
                self._balance_settling = False
                self._emit_structured_event("balance_settling_timeout", {
                    "elapsed_s": round(elapsed, 1),
                })
            else:
                raise Exception(
                    f"Order creation blocked: balance settling in progress "
                    f"(elapsed={elapsed:.1f}s). Waiting for REST balance sync after reconnect.")
        exchange_order_id = await super()._place_order_and_process_update(order, **kwargs)

        # If we reach here, the exchange accepted the order and is holding collateral.
        # Locally mirror that hold so other strategies/controllers see it immediately.
        try:
            base_asset, quote_asset = order.trading_pair.split("-")

            if order.trade_type == TradeType.SELL:
                # Sell order: exchange holds base asset
                current = self._account_available_balances.get(base_asset, Decimal("0"))
                adjusted = max(Decimal("0"), current - order.amount)
                if adjusted != current:
                    self.logger().debug(
                        f"Local balance pre-adjust: SELL {order.amount} {base_asset}, "
                        f"available {current} -> {adjusted} (pending WS confirmation)"
                    )
                    self._account_available_balances[base_asset] = adjusted
                    self._pre_adjusted_assets[base_asset] = time.time()
                    self._emit_structured_event("local_balance_pre_adjust_applied", {
                        "side": "SELL",
                        "asset": base_asset,
                        "previous": str(current),
                        "adjusted": str(adjusted),
                        "order_id": order.client_order_id,
                    })
            elif order.price is None or order.price.is_nan():
                # NKC-6: market BUY orders carry no price (None/NaN) — `amount * price` raised into
                # the non-fatal except on EVERY market buy, so no local hold was ever recorded.
                # Without a price there is nothing sound to hold locally; skip quietly and let the
                # WS balanceUpdate / REST poll report the real hold.
                # NOTE: NonKYC market-buy quantity semantics (base vs quote denomination) are
                # UNVERIFIED against the live API — confirm before any strategy uses MARKET buys.
                self.logger().debug(
                    f"Skipping local balance pre-adjust for {order.client_order_id}: "
                    f"no valid order price (market order)."
                )
            else:
                # Buy order: exchange holds quote asset (amount * price + fee)
                current = self._account_available_balances.get(quote_asset, Decimal("0"))
                notional = order.amount * order.price
                # Include estimated quote-side fee in hold amount.
                # Use taker fee (conservative — covers both maker and taker fills).
                fee_pct = Decimal("0")
                if self._trading_fees:
                    fee_pct = self._trading_fees.get("taker_fee", Decimal("0"))
                if fee_pct == Decimal("0"):
                    # Fallback to estimate
                    fee_pct = Decimal(str(self.estimate_fee_pct(is_maker=False)))
                fee_amount = notional * fee_pct
                hold_amount = notional + fee_amount
                adjusted = max(Decimal("0"), current - hold_amount)
                if adjusted != current:
                    self.logger().debug(
                        f"Local balance pre-adjust: BUY {quote_asset} "
                        f"notional={notional} fee={fee_amount} total_hold={hold_amount}, "
                        f"available {current} -> {adjusted} (pending WS confirmation)"
                    )
                    self._account_available_balances[quote_asset] = adjusted
                    self._pre_adjusted_assets[quote_asset] = time.time()
                    self._emit_structured_event("local_balance_pre_adjust_applied", {
                        "side": "BUY",
                        "asset": quote_asset,
                        "previous": str(current),
                        "adjusted": str(adjusted),
                        "order_id": order.client_order_id,
                    })
        except Exception as e:
            # Never let balance bookkeeping break order flow
            self.logger().warning(f"Local balance pre-adjust failed (non-fatal): {repr(e)}")

        return exchange_order_id

    def _on_order_failure(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Optional[Decimal],
        exception: Exception,
        **kwargs,
    ):
        # Classify and count for error summary
        error_str = str(exception).lower()
        if "serverdisconnectederror" in error_str or "server disconnected" in error_str:
            self._last_server_disconnect_time = time.time()
            self._increment_error("rest_5xx")
            classification = "Server disconnected"
        elif "insufficient" in error_str or "20001" in str(exception):
            self._increment_error("order_reject_insufficient")
            classification = "Insufficient funds"
        elif any(t in error_str for t in ("500", "502", "503", "504", "server error")):
            self._increment_error("rest_5xx")
            classification = "Exchange server error"
        elif any(t in error_str for t in ("timeout", "timed out")):
            self._increment_error("rest_timeout")
            classification = "Timeout"
        else:
            self._increment_error("order_reject_other")
            classification = "Other"

        self._log_order_lifecycle(order_id, "REJECTED", f"{classification}: {str(exception)[:150]}")
        try:
            from hummingbot.logger.structured_event_logger import get_structured_logger
            get_structured_logger().emit("order_submit_rejected",
                connector="nonkyc",
                client_order_id=order_id,
                trading_pair=trading_pair,
                trade_type=trade_type.name,
                order_type=order_type.name,
                price=str(price) if price else None,
                amount=str(amount),
                error_classification=classification,
                error_message=str(exception)[:200],
            )
        except Exception:
            pass

        super()._on_order_failure(
            order_id=order_id, trading_pair=trading_pair, amount=amount,
            trade_type=trade_type, order_type=order_type, price=price,
            exception=exception, **kwargs,
        )
        # Emit detailed balance context for insufficient-funds failures
        if "insufficient" in error_str or "20001" in str(exception):
            try:
                base_asset, quote_asset = trading_pair.split("-")
                avail_base = self._account_available_balances.get(base_asset, Decimal("0"))
                total_base = self._account_balances.get(base_asset, Decimal("0"))
                held_base = total_base - avail_base
                avail_quote = self._account_available_balances.get(quote_asset, Decimal("0"))
                total_quote = self._account_balances.get(quote_asset, Decimal("0"))

                active_sell_count = 0
                active_buy_count = 0
                total_sell_held = Decimal("0")
                total_buy_held = Decimal("0")
                for oid, tracked in self._order_tracker.active_orders.items():
                    if tracked.trading_pair == trading_pair:
                        if tracked.trade_type == TradeType.SELL:
                            active_sell_count += 1
                            total_sell_held += tracked.amount - tracked.executed_amount_base
                        elif tracked.trade_type == TradeType.BUY:
                            active_buy_count += 1
                            total_buy_held += (tracked.amount - tracked.executed_amount_base) * tracked.price

                self.logger().warning(
                    f"INSUFFICIENT FUNDS CONTEXT: {trading_pair} {trade_type.name} "
                    f"attempted={amount} price={price} | "
                    f"{base_asset}: avail={avail_base:.8f} held={held_base:.8f} total={total_base:.8f} | "
                    f"{quote_asset}: avail={avail_quote:.8f} total={total_quote:.8f} | "
                    f"Active orders on pair: {active_sell_count} sells (holding ~{total_sell_held:.8f} {base_asset}), "
                    f"{active_buy_count} buys (holding ~{total_buy_held:.8f} {quote_asset})"
                )
            except Exception as ctx_err:
                self.logger().debug(f"Failed to log insufficient funds context: {repr(ctx_err)}")

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        cancel_id = tracked_order.exchange_order_id
        if not cancel_id or cancel_id == self.UNKNOWN_EXCHANGE_ORDER_ID:
            # Fallback: NonKYC API accepts cancel by userProvidedId
            cancel_id = tracked_order.client_order_id
            self.logger().info(
                f"cancel: exchange_order_id unavailable for {order_id}, "
                f"falling back to client_order_id: {cancel_id}"
            )
        api_params = {
            "id": cancel_id,
        }
        self._log_order_lifecycle(order_id, "CANCEL_SENT", f"exch_id={cancel_id}")
        try:
            from hummingbot.logger.structured_event_logger import get_structured_logger
            get_structured_logger().emit("order_cancel_requested",
                connector="nonkyc",
                client_order_id=order_id,
                trading_pair=tracked_order.trading_pair,
                exchange_order_id=str(cancel_id),
            )
        except Exception:
            pass
        CANCEL_REQUEST_TIMEOUT = 15.0
        t_start = time.monotonic()
        try:
            # async_timeout (not asyncio.timeout): asyncio.timeout needs Python >= 3.11 while
            # the env pin allows resolving lower; async_timeout raises asyncio.TimeoutError so
            # the except clause below is unchanged. Keeps parity with cancel_all.
            async with timeout(CANCEL_REQUEST_TIMEOUT):
                cancel_result = await self._api_post(
                    path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
                    data=api_params,
                    is_auth_required=True)
        except asyncio.TimeoutError:
            t_elapsed = (time.monotonic() - t_start) * 1000
            self._record_api_latency("cancelorder", t_elapsed)
            self.logger().warning(
                f"Cancel request timed out after {CANCEL_REQUEST_TIMEOUT}s for {order_id}. "
                f"Will retry on next cycle."
            )
            self._emit_structured_event("cancel_request_timeout", {
                "order_id": order_id,
                "timeout_s": CANCEL_REQUEST_TIMEOUT,
            })
            return False
        t_elapsed = (time.monotonic() - t_start) * 1000
        self._record_api_latency("cancelorder", t_elapsed)
        self.logger().debug(
            f"REST latency: cancelorder {tracked_order.trading_pair} -> {t_elapsed:.0f}ms"
        )
        if t_elapsed > 2000:
            self.logger().warning(
                f"REST SLOW: cancelorder {tracked_order.trading_pair} took {t_elapsed:.0f}ms"
            )
        if cancel_result.get("id") is not None:
            return True
        return False

    async def cancel_all(self, timeout_seconds: float) -> List[CancellationResult]:
        """
        Cancel open orders, with two modes controlled by ``_cancel_exchange_orphans``.

        **Default mode** (``_cancel_exchange_orphans=False``):
            Only cancels orders tracked by this bot instance, individually via
            ``_cancel_all_fallback()``.  Does NOT query ``/account/orders`` or call
            ``/cancelallorders``.  Safe for shared API keys where multiple bots or
            manual trades coexist on the same account.

        **Orphan-recovery mode** (``_cancel_exchange_orphans=True``):
            Queries the exchange for ALL active orders, detects orphans (orders
            present on the exchange but not tracked locally), and batch-cancels per
            symbol via ``/cancelallorders``.  Only enable this with a dedicated API
            key that is not shared with other sessions.

        :param timeout_seconds: maximum time to wait for cancel operations
        :return: list of CancellationResult for each tracked order
        """
        # Collect locally tracked incomplete orders for result reporting
        tracked_orders = {o.client_order_id: o for o in self.in_flight_orders.values() if not o.is_done}

        # Default safe mode: only cancel orders this bot instance is tracking
        if not self._cancel_exchange_orphans:
            return await self._cancel_all_fallback(timeout_seconds, tracked_orders)

        tracked_exchange_ids = {o.exchange_order_id for o in tracked_orders.values() if o.exchange_order_id}

        results = []
        try:
            async with timeout(timeout_seconds):
                # Step 1: Query ALL active orders from the exchange
                try:
                    exchange_orders = await self._api_get(
                        path_url=CONSTANTS.ACCOUNT_ORDERS_PATH_URL,
                        params={"status": "active"},
                        is_auth_required=True,
                        limit_id=CONSTANTS.ACCOUNT_ORDERS_PATH_URL,
                    )
                except Exception as e:
                    self.logger().warning(
                        f"Failed to query active orders from exchange: {e}. "
                        f"Falling back to individual cancel."
                    )
                    # Fall back to base class behavior (cancel tracked orders individually)
                    return await self._cancel_all_fallback(timeout_seconds, tracked_orders)

                if not isinstance(exchange_orders, list):
                    self.logger().warning(
                        f"Unexpected /account/orders response type: {type(exchange_orders)}. "
                        f"Falling back to individual cancel."
                    )
                    return await self._cancel_all_fallback(timeout_seconds, tracked_orders)

                # Step 2: Detect orphans — orders on exchange but not in local tracker
                for ex_order in exchange_orders:
                    ex_id = str(ex_order.get("id", ""))
                    if ex_id and ex_id not in tracked_exchange_ids:
                        # Defensive symbol extraction (top-level or nested under market)
                        symbol = ex_order.get("symbol") or ex_order.get("market", {}).get("symbol", "unknown")
                        self.logger().warning(
                            f"Orphaned order detected on exchange: id={ex_id}, "
                            f"symbol={symbol}, side={ex_order.get('side', '?')}, "
                            f"price={ex_order.get('price', '?')}, "
                            f"qty={ex_order.get('quantity', '?')}. Will be cancelled."
                        )

                # Step 3: Extract unique symbols that have active orders
                symbols_with_orders = set()
                for ex_order in exchange_orders:
                    symbol = ex_order.get("symbol") or ex_order.get("market", {}).get("symbol")
                    if symbol:
                        symbols_with_orders.add(symbol)

                if not symbols_with_orders and not tracked_orders:
                    self.logger().info("cancel_all: No active orders on exchange and no tracked orders.")
                    return []

                # Also include symbols from tracked orders that might not be on exchange yet
                for order in tracked_orders.values():
                    try:
                        exchange_symbol = await self.exchange_symbol_associated_to_pair(
                            trading_pair=order.trading_pair
                        )
                        symbols_with_orders.add(exchange_symbol)
                    except Exception:
                        pass  # Symbol mapping not available, skip

                # Convert to stable list for deterministic zip alignment
                symbols_list = sorted(symbols_with_orders)

                # Step 4: Batch cancel per symbol
                cancel_tasks = []
                for symbol in symbols_list:
                    cancel_tasks.append(self._cancel_all_for_symbol(symbol))

                cancel_results = await safe_gather(*cancel_tasks, return_exceptions=True)

                # Log results per symbol
                for symbol, cr in zip(symbols_list, cancel_results):
                    if isinstance(cr, Exception):
                        self.logger().warning(f"Failed to cancel orders for {symbol}: {cr}")
                    else:
                        self.logger().info(f"cancel_all for {symbol}: {cr}")

                # Step 5: Build CancellationResult list from tracked orders
                # After batch cancel, mark all tracked orders as successfully cancelled
                # (if the batch call succeeded for their symbol)
                cancelled_symbols = set()
                for symbol, cr in zip(symbols_list, cancel_results):
                    if not isinstance(cr, Exception):
                        cancelled_symbols.add(symbol)

                for client_oid, order in tracked_orders.items():
                    try:
                        exchange_symbol = await self.exchange_symbol_associated_to_pair(
                            trading_pair=order.trading_pair
                        )
                        success = exchange_symbol in cancelled_symbols
                    except Exception:
                        success = False
                    results.append(CancellationResult(client_oid, success))

        except asyncio.TimeoutError:
            self.logger().warning(f"cancel_all timed out after {timeout_seconds}s")
            # Mark any un-reported tracked orders as failed
            reported_ids = {r.order_id for r in results}
            for client_oid in tracked_orders:
                if client_oid not in reported_ids:
                    results.append(CancellationResult(client_oid, False))
        except Exception:
            self.logger().network(
                "Unexpected error cancelling orders.",
                exc_info=True,
                app_warning_msg="Failed to cancel orders. Check API key and network connection."
            )
            for client_oid in tracked_orders:
                if not any(r.order_id == client_oid for r in results):
                    results.append(CancellationResult(client_oid, False))

        return results

    async def _cancel_all_for_symbol(self, symbol: str) -> dict:
        """
        Call POST /cancelallorders for a single symbol.

        :param symbol: Exchange symbol in slash format (e.g., "BTC/USDT")
        :return: API response dict
        :raises: Exception on API error
        """
        response = await self._api_post(
            path_url=CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL,
            data={"symbol": symbol},
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL,
        )
        # The endpoint returns HTTP 200 even for errors — check response body
        if isinstance(response, dict) and "error" in response:
            error_msg = response["error"].get("description", response["error"].get("message", "Unknown"))
            raise IOError(f"cancelallorders failed for {symbol}: {error_msg}")
        return response

    async def _cancel_all_fallback(
        self,
        timeout_seconds: float,
        tracked_orders: dict,
    ) -> List[CancellationResult]:
        """
        Fallback: cancel tracked orders individually (base class behavior).
        Used when /account/orders query fails.
        """
        tasks = [self._execute_cancel(o.trading_pair, o.client_order_id) for o in tracked_orders.values()]
        order_id_set = set(tracked_orders.keys())
        successful = []
        try:
            async with timeout(timeout_seconds):
                cancellation_results = await safe_gather(*tasks, return_exceptions=True)
                for cr in cancellation_results:
                    if isinstance(cr, Exception):
                        continue
                    if cr is not None:
                        order_id_set.discard(cr)
                        successful.append(CancellationResult(cr, True))
        except Exception:
            self.logger().network("Unexpected error in cancel fallback.", exc_info=True)
        failed = [CancellationResult(oid, False) for oid in order_id_set]
        return successful + failed

    async def cancel_all_orders_on_exchange(self, trading_pair: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Cancels all open orders on the exchange, optionally filtered by trading pair.
        NonKYC's /cancelallorders endpoint requires a symbol parameter, so when
        trading_pair is None, we fan out across all pairs with tracked orders.

        :param trading_pair: if provided (Hummingbot format, e.g. 'BTC-USDT'),
                             only cancel orders for this pair. If None, cancel all active pairs.
        :return: list of cancelled order data dicts from the exchange
        """
        results = []

        if trading_pair is not None:
            result = await self._cancel_all_for_pair(trading_pair)
            results.extend(result)
        else:
            # Fan out across all pairs with active orders
            active_pairs = set()
            for order in self._order_tracker.active_orders.values():
                active_pairs.add(order.trading_pair)

            for pair in active_pairs:
                try:
                    result = await self._cancel_all_for_pair(pair)
                    results.extend(result)
                except Exception as e:
                    self.logger().warning(f"Failed to cancel all orders for {pair}: {e}")

        return results

    async def _cancel_all_for_pair(self, trading_pair: str) -> List[Dict[str, Any]]:
        """Cancel all orders for a specific trading pair."""
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        api_params = {"symbol": symbol}

        result = await self._api_post(
            path_url=CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)

        # Check for error responses (NonKYC can return errors inside HTTP 200)
        if isinstance(result, dict) and "error" in result:
            raise IOError(f"Cancel all orders failed for {trading_pair}: {result['error']}")

        return result if isinstance(result, list) else [result] if isinstance(result, dict) else []

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        trading_pair_rules = exchange_info_dict
        retval = []
        for rule in filter(nonkyc_utils.is_market_active, trading_pair_rules):
            try:
                try:
                    trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=rule.get("symbol"))
                except KeyError:
                    # Not in the symbol map (e.g. filtered out or a race with a fresh listing) —
                    # nothing to parse; skip quietly instead of dumping the full rule at ERROR.
                    self.logger().debug(
                        f"Skipping trading rule for unmapped symbol {rule.get('symbol')}.")
                    continue
                price_decimals = Decimal(rule.get("priceDecimals"))
                quantity_decimals = Decimal(rule.get("quantityDecimals"))

                min_price_increment = Decimal(10) ** (-int(price_decimals))
                min_base_amount_increment = Decimal(10) ** (-int(quantity_decimals))

                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=Decimal(str(rule.get("minimumQuantity", min_base_amount_increment))),
                        max_order_size=Decimal(str(rule.get("maximumQuantity"))) if rule.get("maximumQuantity") else Decimal("Inf"),
                        min_price_increment=min_price_increment,
                        min_base_amount_increment=min_base_amount_increment,
                        min_notional_size=Decimal(str(rule.get("minQuote", 0))) if rule.get("isMinQuoteActive") else Decimal("0"),
                        supports_market_orders=bool(rule.get("allowMarketOrders", True)),
                    ))

            except Exception as e:
                self.logger().exception(f"Error parsing the trading pair rule {rule}. Skipping.")
        return retval

    async def _update_trading_rules(self):
        exchange_info = await self._make_trading_rules_request()
        # Refresh the symbol map BEFORE formatting rules: a pair listed since the last poll is
        # absent from the old map, and formatting against the old map raised a noisy KeyError
        # (e.g. a new listing appearing mid-session) while delaying its rules by one full cycle.
        self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
        trading_rules_list = await self._format_trading_rules(exchange_info)
        # Detect trading rule changes before overwriting (LOG 14)
        for new_rule in trading_rules_list:
            pair = new_rule.trading_pair
            old_rule = self._trading_rules.get(pair)
            if old_rule is not None:
                changes = []
                if old_rule.min_order_size != new_rule.min_order_size:
                    changes.append(f"min_order_size: {old_rule.min_order_size} -> {new_rule.min_order_size}")
                if old_rule.min_price_increment != new_rule.min_price_increment:
                    changes.append(f"tick_size: {old_rule.min_price_increment} -> {new_rule.min_price_increment}")
                if old_rule.min_base_amount_increment != new_rule.min_base_amount_increment:
                    changes.append(f"qty_step: {old_rule.min_base_amount_increment} -> {new_rule.min_base_amount_increment}")
                if old_rule.min_notional_size != new_rule.min_notional_size:
                    changes.append(f"min_notional: {old_rule.min_notional_size} -> {new_rule.min_notional_size}")
                if changes:
                    self.logger().warning(
                        f"TRADING RULES CHANGED for {pair}: {', '.join(changes)}"
                    )
        self._trading_rules.clear()
        for trading_rule in trading_rules_list:
            self._trading_rules[trading_rule.trading_pair] = trading_rule

    async def _status_polling_loop_fetch_updates(self):
        # NKC-7: try/finally — an exception mid-cycle previously left the flag stuck True,
        # permanently disabling per-order fill recovery in _all_trade_updates_for_order.
        self._bulk_fills_fetched_this_cycle = True
        try:
            await self._update_order_fills_from_trades()
            await super()._status_polling_loop_fetch_updates()
        finally:
            self._bulk_fills_fetched_this_cycle = False

    async def _update_trading_fees(self):
        """
        Computes actual maker/taker fee rates from the user's recent trade history.

        NonKYC does not have a dedicated fee-tier API endpoint. Instead, we calculate
        fee percentages from actual trades using:
            fee_rate = fee / (quantity * price)

        Maker vs taker is determined by comparing the 'side' and 'triggeredBy' fields:
            - side != triggeredBy -> maker (your resting order was matched)
            - side == triggeredBy -> taker (you matched a resting order)

        Results are cached for 1 hour (_trading_fees_ttl) since NonKYC fee tiers
        rarely change. This prevents fetching the entire trade history every poll cycle.
        """
        # Check cache TTL — skip if fees were computed recently
        now = self._time_synchronizer.time() if hasattr(self, '_time_synchronizer') else time.time()
        if (self._trading_fees
                and (now - self._trading_fees_last_computed) < self._trading_fees_ttl):
            return

        try:
            # Only fetch trades from the last 24 hours for fee calculation
            since_ts = str(int((now - 86400) * 1e3))
            all_trades = await self._api_get(
                path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL,
                params={"since": since_ts},
                is_auth_required=True)

            if not all_trades:
                self.logger().debug("No trade history found for dynamic fee calculation. Using defaults.")
                return

            maker_rates = []
            taker_rates = []
            alt_fee_skipped = 0
            zero_fee_skipped = 0

            for trade in all_trades:
                try:
                    fee = Decimal(str(trade.get("fee", "0")))

                    # Skip trades with alternate fee assets — fee is denominated
                    # in a different asset, making percentage calculation invalid
                    alt_fee_asset = trade.get("alternateFeeAsset")
                    if alt_fee_asset:
                        alt_fee_skipped += 1
                        continue

                    quantity = Decimal(str(trade.get("quantity", "0")))
                    price = Decimal(str(trade.get("price", "0")))
                    notional = quantity * price

                    if notional <= 0 or fee <= 0:
                        zero_fee_skipped += 1
                        continue

                    fee_rate = fee / notional
                    side = str(trade.get("side", "")).lower()
                    triggered_by = str(trade.get("triggeredBy", "")).lower()

                    if not side or not triggered_by:
                        continue

                    if side != triggered_by:
                        maker_rates.append(fee_rate)
                    else:
                        taker_rates.append(fee_rate)

                except (InvalidOperation, DivisionByZero, TypeError, KeyError):
                    continue

            if maker_rates:
                avg_maker = sum(maker_rates) / len(maker_rates)
                self._trading_fees["maker_fee"] = avg_maker
            if taker_rates:
                avg_taker = sum(taker_rates) / len(taker_rates)
                self._trading_fees["taker_fee"] = avg_taker

            self._trading_fees_last_computed = now

            if self._trading_fees:
                default_schema = self.trade_fee_schema()
                computed_maker = self._trading_fees.get("maker_fee")
                computed_taker = self._trading_fees.get("taker_fee")
                default_maker = default_schema.maker_percent_fee_decimal
                default_taker = default_schema.taker_percent_fee_decimal

                parts = []
                if computed_maker is not None:
                    parts.append(f"maker={computed_maker:.6f} (default={default_maker:.6f}, {len(maker_rates)} trades)")
                if computed_taker is not None:
                    parts.append(f"taker={computed_taker:.6f} (default={default_taker:.6f}, {len(taker_rates)} trades)")
                self.logger().info(
                    f"Dynamic fee rates computed from trade history: {', '.join(parts)} "
                    f"(skipped: {alt_fee_skipped} alt-fee, {zero_fee_skipped} zero-fee)"
                )

        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().network(
                "Error computing dynamic fee rates from trade history.",
                exc_info=True,
                app_warning_msg=f"Could not compute trading fees for {self.name}. Using defaults.")

    async def _user_stream_event_listener(self):
        """
        This functions runs in background continuously processing the events received from the exchange by the user
        stream data source. It keeps reading events from the queue until the task is interrupted.
        The events received are balance updates, order updates and trade events.
        """
        # Start balance WS health timer
        if self._balance_ws_subscription_time is None:
            self._balance_ws_subscription_time = time.time()
        async for event_message in self._iter_user_event_queue():
            event_type = None
            try:
                # Balance WS health check — warn if subscription sent but no events received
                if (self._balance_ws_subscription_time is not None
                        and not self._balance_ws_confirmed
                        and time.time() - self._balance_ws_subscription_time > 60.0):
                    self.logger().warning(
                        "NonKYC private balance WebSocket: NO CONFIRMATION received within 60s. "
                        "Auto-disabling undocumented balance WS for this session. "
                        "REST polling will handle balance updates."
                    )
                    self.ENABLE_BALANCE_WS = False  # Prevent re-subscription on WS reconnect
                    self._balance_ws_subscription_time = None  # Don't warn again

                event_type = event_message.get("method")

                # Handle subscription responses (have "id" but no "method")
                # subscribeBalances returns balance data in "result" field directly
                if event_type is None and "result" in event_message:
                    result = event_message["result"]
                    # Check if this looks like a balance array
                    if (isinstance(result, list) and result
                            and isinstance(result[0], dict)
                            and "ticker" in result[0]
                            and "available" in result[0]):
                        if not self._balance_ws_confirmed:
                            self.logger().info(
                                "NonKYC private balance WebSocket: CONFIRMED "
                                "(received balance data in subscribeBalances response)"
                            )
                            self._balance_ws_confirmed = True
                        for balance_entry in result:
                            asset_name = balance_entry["ticker"]
                            free_balance = Decimal(balance_entry["available"])
                            total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
                            self._account_available_balances[asset_name] = free_balance
                            self._account_balances[asset_name] = total_balance
                        self.logger().debug(
                            f"Processed {len(result)} assets from subscribeBalances response"
                        )
                    continue  # Don't fall through to method-based handlers

                if event_type == "report":
                    message_params = event_message.get('params', {})
                    reportType = message_params.get('reportType')
                    client_order_id = message_params.get("userProvidedId")

                    if reportType == "trade":
                        tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)
                        # Prefer deriving quote from tracked order's trading pair (reliable)
                        # Fall back to parsing the exchange symbol
                        if tracked_order is not None:
                            quote_asset = tracked_order.trading_pair.split("-")[1]
                        else:
                            symbol = message_params.get('symbol', '')
                            parts = symbol.split('/') if symbol else []
                            if len(parts) >= 2:
                                quote_asset = parts[1]
                            else:
                                self.logger().warning(
                                    f"Could not parse quote asset from symbol '{symbol}' in trade report. Skipping.")
                                continue
                        if tracked_order is not None:
                            # NKC-1(b): a WS report carries the real exchange id — repair the
                            # "UNKNOWN" placement sentinel so REST polling/cancel can use it.
                            self._repair_unknown_exchange_order_id(tracked_order, message_params.get("id"))
                            fee_token, fee_amount = self._extract_fee_token_and_amount(message_params, quote_asset)
                            fee = TradeFeeBase.new_spot_fee(
                                fee_schema=self.trade_fee_schema(),
                                trade_type=tracked_order.trade_type,
                                percent_token=fee_token,
                                flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)]
                            )
                            # Derive maker/taker the same way as the REST poll path
                            # (side vs triggeredBy; default taker when either is missing)
                            _side = str(message_params.get("side", "")).lower()
                            _triggered_by = str(message_params.get("triggeredBy", "")).lower()
                            _is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
                            trade_update = TradeUpdate(
                                trade_id=str(message_params["tradeId"]),
                                client_order_id=client_order_id,
                                exchange_order_id=str(message_params["id"]),
                                trading_pair=tracked_order.trading_pair,
                                fee=fee,
                                fill_base_amount=Decimal(message_params["tradeQuantity"]),
                                fill_quote_amount=Decimal(message_params["tradeQuantity"]) * Decimal(message_params["tradePrice"]),
                                fill_price=Decimal(message_params["tradePrice"]),
                                fill_timestamp=message_params["updatedAt"] * 1e-3,
                                is_taker=_is_taker,
                                received_timestamp_ms=int(time.time() * 1e3),
                                source_channel="ws",
                            )
                            self._order_tracker.process_trade_update(trade_update)
                            # Tag fill source for provenance tracking
                            tracked_order.fill_sources[str(message_params["tradeId"])] = "ws"
                            try:
                                from hummingbot.logger.structured_event_logger import get_structured_logger
                                _sel = get_structured_logger()
                                _best_bid = _best_ask = None
                                _ob = self.get_order_book(tracked_order.trading_pair)
                                if _ob:
                                    _best_bid = float(_ob.get_price(False)) if _ob.get_price(False) else None
                                    _best_ask = float(_ob.get_price(True)) if _ob.get_price(True) else None
                                _sel.emit("connector_fill_received",
                                    connector="nonkyc", source="ws",
                                    order_id=client_order_id,
                                    trade_id=str(message_params["tradeId"]),
                                    trading_pair=tracked_order.trading_pair,
                                    side=tracked_order.trade_type.name,
                                    fill_price=str(message_params["tradePrice"]),
                                    fill_amount=str(message_params["tradeQuantity"]),
                                    exchange_timestamp_ms=message_params["updatedAt"],
                                    is_taker=_is_taker,
                                    best_bid=_best_bid, best_ask=_best_ask,
                                    balance_settling=self._balance_settling)
                            except Exception:
                                pass
                            self._log_order_lifecycle(client_order_id, "FILL",
                                f"qty={message_params['tradeQuantity']} price={message_params['tradePrice']} "
                                f"exch_id={message_params['id']}")

                    tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                    if tracked_order is not None:
                        # NKC-1(b): repair the "UNKNOWN" placement sentinel from the WS report.
                        self._repair_unknown_exchange_order_id(tracked_order, message_params.get("id"))
                        # NKC-9: unknown statuses coerce to OPEN with a rate-limited WARNING.
                        new_state = self._order_state_for_status(
                            message_params["status"], client_order_id, "ws")
                        order_update = OrderUpdate(
                            trading_pair=tracked_order.trading_pair,
                            update_timestamp=message_params["updatedAt"] * 1e-3,
                            new_state=new_state,
                            client_order_id=client_order_id,
                            exchange_order_id=str(message_params["id"]),
                        )
                        self._order_tracker.process_order_update(order_update=order_update)
                        # Order lifecycle logging for cancel/fill completion
                        if reportType == "cancelled":
                            self._log_order_lifecycle(client_order_id, "CANCEL_CONFIRMED",
                                f"exch_id={message_params['id']}")
                        elif new_state == OrderState.FILLED:
                            self._log_order_lifecycle(client_order_id, "COMPLETED",
                                f"fully filled exch_id={message_params['id']}")

                # NOTE: subscribeBalances / currentBalances / balanceUpdate are undocumented
                # NonKYC WS methods. They work as of 2026-03, but are not in the official
                # WS API docs. If they stop working, the connector falls back to REST
                # balance polling via _update_balances() which runs on the standard
                # polling loop. See also: _subscribe_channels() in user stream data source.
                elif event_type == "currentBalances":
                    if not self._balance_ws_confirmed:
                        self.logger().info(
                            "NonKYC private balance WebSocket: CONFIRMED (received currentBalances snapshot)"
                        )
                        self._balance_ws_confirmed = True
                    balance_entries = event_message.get("result", [])
                    for balance_entry in balance_entries:
                        asset_name = balance_entry["ticker"]
                        free_balance = Decimal(balance_entry["available"])
                        total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
                        old_free = self._account_available_balances.get(asset_name, Decimal("0"))
                        old_total = self._account_balances.get(asset_name, Decimal("0"))
                        if free_balance != old_free or total_balance != old_total:
                            self.logger().debug(
                                f"NonKYC balance delta: source=currentBalances asset={asset_name} "
                                f"free {old_free}->{free_balance} total {old_total}->{total_balance}"
                            )
                        # Convergence check: was this asset recently pre-adjusted?
                        if asset_name in self._pre_adjusted_assets:
                            adjust_age = time.time() - self._pre_adjusted_assets[asset_name]
                            if adjust_age < 10.0:
                                delta = free_balance - old_free
                                if abs(delta) > Decimal("0.00000001"):
                                    self.logger().info(
                                        f"Balance pre-adjust convergence: {asset_name} "
                                        f"pre-adjusted={old_free:.8f} exchange={free_balance:.8f} "
                                        f"delta={delta:+.8f} age={adjust_age:.1f}s"
                                    )
                            del self._pre_adjusted_assets[asset_name]
                        self._account_available_balances[asset_name] = free_balance
                        self._account_balances[asset_name] = total_balance

                elif event_type == "balanceUpdate":
                    balance_entry = event_message.get("params")
                    if not balance_entry:
                        self.logger().warning("Received balanceUpdate with empty params, skipping.")
                        continue
                    asset_name = balance_entry["ticker"]
                    free_balance = Decimal(balance_entry["available"])
                    total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
                    old_free = self._account_available_balances.get(asset_name, Decimal("0"))
                    old_total = self._account_balances.get(asset_name, Decimal("0"))
                    if free_balance != old_free or total_balance != old_total:
                        self.logger().debug(
                            f"NonKYC balance delta: source=balanceUpdate asset={asset_name} "
                            f"free {old_free}->{free_balance} total {old_total}->{total_balance}"
                        )
                    # Convergence check: was this asset recently pre-adjusted?
                    if asset_name in self._pre_adjusted_assets:
                        adjust_age = time.time() - self._pre_adjusted_assets[asset_name]
                        if adjust_age < 10.0:
                            delta = free_balance - old_free
                            if abs(delta) > Decimal("0.00000001"):
                                self.logger().info(
                                    f"Balance pre-adjust convergence: {asset_name} "
                                    f"pre-adjusted={old_free:.8f} exchange={free_balance:.8f} "
                                    f"delta={delta:+.8f} age={adjust_age:.1f}s"
                                )
                        del self._pre_adjusted_assets[asset_name]
                    self._account_available_balances[asset_name] = free_balance
                    self._account_balances[asset_name] = total_balance

                elif event_type == "activeOrders":
                    active_orders = event_message.get("result") or event_message.get("params") or []
                    if not isinstance(active_orders, list):
                        active_orders = []
                    for order_data in active_orders:
                        client_order_id = str(order_data.get("userProvidedId", ""))
                        tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                        if tracked_order is not None:
                            # NKC-1(b) + NKC-9 (see the report branch above).
                            self._repair_unknown_exchange_order_id(tracked_order, order_data.get("id"))
                            new_state = self._order_state_for_status(
                                order_data.get("status", ""), client_order_id, "ws")
                            order_update = OrderUpdate(
                                trading_pair=tracked_order.trading_pair,
                                update_timestamp=order_data.get("updatedAt", 0) * 1e-3,
                                new_state=new_state,
                                client_order_id=client_order_id,
                                exchange_order_id=str(order_data.get("id", "")),
                            )
                            self._order_tracker.process_order_update(order_update)

                # NKC-2: server error frames ({"id": N, "error": {...}}) used to fall through
                # every branch unlogged — a rejected subscription or failed request was
                # invisible. Catch-all WARNING, rate-limited to one per 30s window.
                elif "error" in event_message:
                    now = time.time()
                    if now - self._ws_error_frame_last_warn >= self._UNKNOWN_STATUS_WARN_INTERVAL_S:
                        self._ws_error_frame_last_warn = now
                        error = event_message.get("error") or {}
                        self.logger().warning(
                            f"NonKYC WS error frame (unhandled): id={event_message.get('id')} "
                            f"code={error.get('code') if isinstance(error, dict) else None} "
                            f"message={error.get('message') if isinstance(error, dict) else error}"
                        )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(
                    "Unexpected error in user stream listener loop.",
                    exc_info=True
                )
                await self._sleep(5.0)

    async def _update_order_fills_from_trades(self):
        """
        This is intended to be a backup measure to get filled events with trade ID for orders,
        in case Nonkyc's user stream events are not working.
        NOTE: It is not required to copy this functionality in other connectors.
        This is separated from _update_order_status which only updates the order status without producing filled
        events, since Nonkyc's get order endpoint does not return trade IDs.
        The minimum poll interval for order status is 10 seconds.
        """
        small_interval_last_tick = self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        small_interval_current_tick = self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        long_interval_last_tick = self._last_poll_timestamp / self.LONG_POLL_INTERVAL
        long_interval_current_tick = self.current_timestamp / self.LONG_POLL_INTERVAL

        if (long_interval_current_tick > long_interval_last_tick
                or (self.in_flight_orders and small_interval_current_tick > small_interval_last_tick)):
            query_time = int(self._last_trades_poll_nonkyc_timestamp * 1e3)
            self._last_trades_poll_nonkyc_timestamp = self._time_synchronizer.time()
            order_by_exchange_id_map = {}
            sentinel_orders = []
            for order in self._order_tracker.all_fillable_orders.values():
                if order.exchange_order_id is None:
                    continue
                if str(order.exchange_order_id) == self.UNKNOWN_EXCHANGE_ORDER_ID:
                    sentinel_orders.append(order)
                    continue
                order_by_exchange_id_map[str(order.exchange_order_id)] = order
            # NKC-1(c): never key the fill map under the "UNKNOWN" placement sentinel — a real
            # trade's orderid can never match it, so its fills would fall into the untracked branch
            # and be dropped. Resolve the real id by client id first (repairs the tracked order);
            # orders that cannot be resolved yet stay out of the map for this cycle.
            for order in sentinel_orders:
                repaired_id = await self._resolve_unknown_exchange_order_id(order)
                if repaired_id is not None:
                    order_by_exchange_id_map[repaired_id] = order

            # NKC-8: /account/trades IGNORES the symbol param and returns the GLOBAL account trade
            # list, so the previous per-pair fan-out fetched N identical copies of the same list
            # every cycle (20 weight each). Fetch ONCE per cycle; attribution below is by orderid /
            # market.symbol — never a poll pair — so the dedupe makes this behavior-neutral.
            params = {}
            if self._last_poll_timestamp > 0:
                params["since"] = query_time
            else:
                # First poll: /account/trades is GLOBAL, so omitting `since` pulls the
                # account's entire trade history on startup. Floor to the last 3 days.
                params["since"] = int((self._time_synchronizer.time() - 3 * 24 * 3600) * 1e3)

            self.logger().debug(
                f"Polling for order fills (single global trades fetch covering "
                f"{len(self.trading_pairs or [])} trading pairs).")
            try:
                trades = await self._api_get(
                    path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL,
                    params=params,
                    is_auth_required=True)
            except asyncio.CancelledError:
                raise
            except Exception as request_error:
                self.logger().network(
                    f"Error fetching account trades update: {request_error}.",
                    app_warning_msg="Failed to fetch trade updates for nonkyc."
                )
                return
            if not isinstance(trades, list):
                return

            # Attribute each trade to its OWN order (by orderid) / its OWN market (resolved from
            # market.symbol) -- NEVER a poll pair -- and dedupe by trade id so each trade is
            # processed exactly once. This eliminates both the cross-pair fill leak and the
            # duplicate-fill re-insert.
            for trade in trades:
                trade_id = str(trade["id"])
                if self._is_trade_processed(trade_id):
                    continue
                exchange_order_id = str(trade["orderid"])
                if exchange_order_id in order_by_exchange_id_map:
                    # Fill for a currently-tracked order. The ORDER (not the poll pair) is
                    # authoritative for the trading pair.
                    tracked_order = order_by_exchange_id_map[exchange_order_id]
                    order_pair = tracked_order.trading_pair
                    # Secondary guard: if the trade's market resolves and disagrees with the order's
                    # pair, skip it -- never attribute a trade to the wrong market.
                    resolved_pair = await self._safe_resolve_trading_pair(trade)
                    if resolved_pair is not None and resolved_pair != order_pair:
                        self.logger().debug(
                            f"Skipping trade {trade_id}: market {resolved_pair} != order pair {order_pair}.")
                        continue
                    _, quote_asset = split_hb_trading_pair(trading_pair=order_pair)
                    fee_token, fee_amount = self._extract_fee_token_and_amount(trade, quote_asset)
                    fee = TradeFeeBase.new_spot_fee(
                        fee_schema=self.trade_fee_schema(),
                        trade_type=tracked_order.trade_type,
                        percent_token=fee_token,
                        flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)]
                    )
                    # Derive maker/taker from side vs triggeredBy
                    _side = str(trade.get("side", "")).lower()
                    _triggered_by = str(trade.get("triggeredBy", "")).lower()
                    _is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
                    trade_update = TradeUpdate(
                        trade_id=trade_id,
                        client_order_id=tracked_order.client_order_id,
                        exchange_order_id=exchange_order_id,
                        trading_pair=order_pair,
                        fee=fee,
                        fill_base_amount=Decimal(trade["quantity"]),
                        fill_quote_amount=Decimal(trade["quantity"]) * Decimal(trade["price"]),
                        fill_price=Decimal(trade["price"]),
                        fill_timestamp=trade["timestamp"] * 1e-3,
                        is_taker=_is_taker,
                        received_timestamp_ms=int(time.time() * 1e3),
                        source_channel="rest_poll",
                    )
                    self._order_tracker.process_trade_update(trade_update)
                    # Tag fill source for provenance tracking
                    tracked_order.fill_sources[trade_id] = "rest_poll"
                    self._mark_trade_processed(trade_id)
                else:
                    # Fill for an order registered in the DB but no longer tracked. Recover it ONLY
                    # for the trade's ACTUAL market (resolved from market.symbol) -- never fan it out
                    # across every connector market, and never use the poll pair.
                    resolved_pair = await self._safe_resolve_trading_pair(trade)
                    if resolved_pair is None:
                        self.logger().debug(
                            f"Skipping untracked trade {trade_id}: cannot resolve its market.")
                        continue
                    if self.is_confirmed_new_order_filled_event(trade_id, exchange_order_id, resolved_pair):
                        self._current_trade_fills.add(TradeFillOrderDetails(
                            market=self.display_name,
                            exchange_trade_id=trade_id,
                            symbol=resolved_pair))
                        _, quote_asset = split_hb_trading_pair(trading_pair=resolved_pair)
                        _fee_token, _fee_amount = self._extract_fee_token_and_amount(trade, quote_asset)
                        _trade_type = TradeType.BUY if str(trade["side"]).lower() == "buy" else TradeType.SELL
                        self.trigger_event(
                            MarketEvent.OrderFilled,
                            OrderFilledEvent(
                                timestamp=float(trade["timestamp"]) * 1e-3,
                                order_id=self._exchange_order_ids.get(exchange_order_id, None),
                                trading_pair=resolved_pair,
                                trade_type=_trade_type,
                                order_type=OrderType.LIMIT,
                                price=Decimal(trade["price"]),
                                amount=Decimal(trade["quantity"]),
                                trade_fee=TradeFeeBase.new_spot_fee(
                                    fee_schema=self.trade_fee_schema(),
                                    trade_type=_trade_type,
                                    flat_fees=[TokenAmount(_fee_token, _fee_amount)]
                                ),
                                exchange_trade_id=trade_id
                            ))
                        self._mark_trade_processed(trade_id)
                        self.logger().info(
                            f"Recreating missing trade in TradeFill (pair={resolved_pair}): {trade}")

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        # Skip per-order fill fetching if bulk _update_order_fills_from_trades()
        # already ran this cycle (prevents duplicate /account/trades API calls).
        # The flag is cleared after the polling cycle, so lost order updates
        # (which run separately) still get their fills fetched correctly.
        if self._bulk_fills_fetched_this_cycle:
            return []

        trade_updates = []

        order_exchange_id = order.exchange_order_id
        if order_exchange_id is not None and str(order_exchange_id) == self.UNKNOWN_EXCHANGE_ORDER_ID:
            # NKC-1(c): the "UNKNOWN" placement sentinel can never match a trade's orderid —
            # resolve the real id by client id first (repairs the tracked order on success).
            order_exchange_id = await self._resolve_unknown_exchange_order_id(order)

        if order_exchange_id is not None:
            exchange_order_id = str(order_exchange_id)
            symbol = await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair)
            base_asset, quote_asset = split_hb_trading_pair(trading_pair=order.trading_pair)

            # Use the order's creation timestamp to limit the trade window.
            # This prevents fetching the entire trade history for accounts with
            # thousands of trades. Subtract 60 seconds as safety margin.
            params = {"symbol": symbol}
            if order.creation_timestamp and order.creation_timestamp > 0:
                since_ms = int((order.creation_timestamp - 60) * 1e3)
                params["since"] = str(since_ms)

            all_fills_response = await self._api_get(
                path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL,
                params=params,
                is_auth_required=True,)

            # Filter the GLOBAL trade list to THIS order (orderid match), and skip any trade already
            # processed by the bulk reconciliation path (dedupe by exchange trade id).
            filtered_trades = [
                trade for trade in (all_fills_response if isinstance(all_fills_response, list) else [])
                if str(trade.get("orderid")) == exchange_order_id
                and not self._is_trade_processed(str(trade["id"]))
            ]

            for trade in filtered_trades:
                fee_token, fee_amount = self._extract_fee_token_and_amount(trade, quote_asset)
                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=order.trade_type,
                    percent_token=fee_token,
                    flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)]
                )
                # Derive maker/taker from side vs triggeredBy
                _side = str(trade.get("side", "")).lower()
                _triggered_by = str(trade.get("triggeredBy", "")).lower()
                _is_taker = (_side == _triggered_by) if (_side and _triggered_by) else True
                trade_update = TradeUpdate(
                    trade_id=str(trade["id"]),
                    client_order_id=order.client_order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=Decimal(trade["quantity"]),
                    fill_quote_amount=Decimal(trade["quantity"]) * Decimal(trade["price"]),
                    fill_price=Decimal(trade["price"]),
                    fill_timestamp=trade["timestamp"] * 1e-3,
                    is_taker=_is_taker,
                    received_timestamp_ms=int(time.time() * 1e3),
                    source_channel="rest_poll",
                )
                trade_updates.append(trade_update)

        return trade_updates

    def _repair_unknown_exchange_order_id(self, tracked_order: InFlightOrder, exchange_order_id: Any) -> None:
        """NKC-1(b): overwrite the "UNKNOWN" placement sentinel with the real exchange id carried by
        a later WS/REST update. InFlightOrder.update_with_order_update only repairs a None id, so
        the sentinel would otherwise stick forever and keep poisoning the REST status poll."""
        if exchange_order_id is None:
            return
        exchange_order_id = str(exchange_order_id)
        if not exchange_order_id or exchange_order_id == self.UNKNOWN_EXCHANGE_ORDER_ID:
            return
        if tracked_order.exchange_order_id == self.UNKNOWN_EXCHANGE_ORDER_ID:
            tracked_order.update_exchange_order_id(exchange_order_id)
            self.logger().info(
                f"Repaired exchange order id for {tracked_order.client_order_id}: "
                f"{self.UNKNOWN_EXCHANGE_ORDER_ID} -> {exchange_order_id}")

    async def _resolve_unknown_exchange_order_id(self, order: InFlightOrder) -> Optional[str]:
        """NKC-1(c): resolve an order stuck on the "UNKNOWN" placement sentinel to its real exchange
        id via GET /getorder/{client_order_id} (the endpoint accepts the userProvidedId as a path
        segment — live-verified 2026-07-14; the ?userProvidedId= query form 404s). Returns the real
        id (repairing the tracked order) or None if the order cannot be resolved yet."""
        try:
            order_data = await self._api_get(
                path_url=f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}",
                is_auth_required=True,
                limit_id=CONSTANTS.ORDER_INFO_PATH_URL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().debug(
                f"Could not resolve exchange order id for {order.client_order_id} "
                f"(placement outcome still unknown): {repr(e)}")
            return None
        raw_id = order_data.get("id") if isinstance(order_data, dict) else None
        if raw_id is None or str(raw_id) in ("", self.UNKNOWN_EXCHANGE_ORDER_ID):
            return None
        self._repair_unknown_exchange_order_id(order, raw_id)
        return str(raw_id)

    def _order_state_for_status(self, raw_status: Any, client_order_id: Optional[str], source: str) -> OrderState:
        """NKC-9: map a NonKYC status string to an OrderState. Unknown statuses coerce to OPEN
        (fail-open on state: never silently terminalize an order on an unrecognized spelling) with
        a rate-limited WARNING; repeated unknown REST statuses for the same order additionally
        trigger a reconciliation log."""
        new_state = CONSTANTS.ORDER_STATE.get(raw_status)
        if new_state is not None:
            if client_order_id is not None:
                self._unknown_status_counts.pop(client_order_id, None)
            return new_state
        now = time.time()
        warn_key = f"{source}:{raw_status}"
        if now - self._unknown_status_last_warn.get(warn_key, 0.0) >= self._UNKNOWN_STATUS_WARN_INTERVAL_S:
            self._unknown_status_last_warn[warn_key] = now
            self.logger().warning(
                f"Unknown order status '{raw_status}' from NonKYC {source} update"
                f"{f' for order {client_order_id}' if client_order_id else ''} — treating as OPEN.")
        if source == "rest" and client_order_id is not None:
            count = self._unknown_status_counts.get(client_order_id, 0) + 1
            self._unknown_status_counts[client_order_id] = count
            if count >= 2:
                recon_key = f"reconcile:{client_order_id}"
                if now - self._unknown_status_last_warn.get(recon_key, 0.0) >= self._UNKNOWN_STATUS_WARN_INTERVAL_S:
                    self._unknown_status_last_warn[recon_key] = now
                    self.logger().warning(
                        f"Order {client_order_id} has returned unknown status '{raw_status}' {count} "
                        f"consecutive times via REST — its local state may be stale; reconcile it "
                        f"manually against the exchange.")
        return OrderState.OPEN

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        # Prefer exchange_order_id (NonKYC internal id) when available; fall back to
        # client_order_id (userProvidedId) for orders not yet confirmed. The "UNKNOWN"
        # placement sentinel (ambiguous 503 on createorder) is NOT a queryable id — polling
        # /getorder/UNKNOWN returns 400/20002 "Order not found", which falsely feeds the
        # lost-order counter and gets a LIVE order marked LOST/FAILED (NKC-1a). /getorder
        # accepts the userProvidedId as a path segment (live-verified 2026-07-14).
        exchange_order_id = tracked_order.exchange_order_id
        if not exchange_order_id or exchange_order_id == self.UNKNOWN_EXCHANGE_ORDER_ID:
            order_id_for_query = tracked_order.client_order_id
        else:
            order_id_for_query = exchange_order_id
        updated_order_data = await self._api_get(
            path_url=f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order_id_for_query}",
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_INFO_PATH_URL)

        # NKC-1(b): the response carries the real exchange id — repair the sentinel so later
        # polls/cancels use it.
        self._repair_unknown_exchange_order_id(tracked_order, updated_order_data.get("id"))

        raw_status = updated_order_data["status"]
        new_state = self._order_state_for_status(raw_status, tracked_order.client_order_id, "rest")

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(updated_order_data["id"]),
            trading_pair=tracked_order.trading_pair,
            update_timestamp=updated_order_data["updatedAt"] * 1e-3,
            new_state=new_state,
        )

        return order_update

    async def _update_balances(self):

        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        # NKC-4: request-start reference for the pre-adjust freshness check below. Uses time.time()
        # (not monotonic) because _pre_adjusted_assets timestamps come from time.time().
        poll_start_time = time.time()
        t_start = time.monotonic()
        balances = await self._api_get(
            path_url=CONSTANTS.USER_BALANCES_PATH_URL,
            is_auth_required=True)
        t_elapsed = (time.monotonic() - t_start) * 1000
        self._record_api_latency("balances", t_elapsed)
        self.logger().debug(f"REST latency: balances -> {t_elapsed:.0f}ms ({len(balances)} assets)")
        if t_elapsed > 3000:
            self.logger().warning(f"REST SLOW: balance poll took {t_elapsed:.0f}ms")

        reconciliation_diffs = []
        for balance_entry in balances:
            asset_name = balance_entry["asset"]
            # NonKYC balance fields:
            #   'available' = funds free for new orders
            #   'held'      = funds locked in open orders
            #   'pending'   = unconfirmed deposits/withdrawals (NOT usable for trading)
            # Total trading balance = available + held (pending excluded intentionally)
            available_balance = Decimal(balance_entry["available"])
            total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
            remote_asset_names.add(asset_name)

            # NKC-4 (LOG-3): a REST snapshot requested BEFORE a local pre-adjust hold was applied
            # must not clobber that fresher hold — the stale snapshot otherwise lands ~1s after
            # order placement and briefly re-inflates available (co-deployed controllers over-place
            # in that window). Keep the local available; total is unaffected by order holds
            # (available+held is conserved by placement), so the REST total still applies.
            pre_adjust_ts = self._pre_adjusted_assets.get(asset_name)
            if pre_adjust_ts is not None and pre_adjust_ts >= poll_start_time:
                self.logger().debug(
                    f"Skipping REST available-balance overwrite for {asset_name}: local pre-adjust "
                    f"is newer than the REST snapshot request start "
                    f"({pre_adjust_ts - poll_start_time:+.3f}s).")
                self._account_balances[asset_name] = total_balance
                continue

            # REST vs WS reconciliation check (LOG 10)
            ws_available = self._account_available_balances.get(asset_name)
            if ws_available is not None and ws_available != available_balance:
                diff = available_balance - ws_available
                if abs(diff) > Decimal("0.00001") and total_balance > Decimal("0"):
                    reconciliation_diffs.append(
                        f"{asset_name}: WS={ws_available:.8f} REST={available_balance:.8f} "
                        f"delta={diff:+.8f}"
                    )

            self._account_available_balances[asset_name] = available_balance
            self._account_balances[asset_name] = total_balance

        # A successful HTTP response is not necessarily a complete account snapshot. NonKYC has
        # been observed returning hundreds of assets while omitting a non-zero USDT row for one
        # poll. Preserve omitted non-zero balances and fail closed until a later REST response
        # explicitly includes them. Zero-valued omitted rows may still be pruned normally.
        missing_asset_names = local_asset_names.difference(remote_asset_names)
        missing_nonzero_assets = {
            asset_name
            for asset_name in missing_asset_names
            if (
                self._account_balances.get(asset_name, Decimal("0")) != Decimal("0")
                or self._account_available_balances.get(asset_name, Decimal("0")) != Decimal("0")
            )
        }
        if missing_nonzero_assets:
            previous_missing = set(getattr(self, "_missing_nonzero_balance_assets", set()))
            self._balance_snapshot_incomplete = True
            self._missing_nonzero_balance_assets = set(missing_nonzero_assets)
            if not self._balance_settling:
                self._balance_settling = True
                self._balance_settle_start = time.time()

            now = time.time()
            missing_changed = previous_missing != missing_nonzero_assets
            if missing_changed or now - self._last_incomplete_balance_warn >= 60.0:
                self._last_incomplete_balance_warn = now
                preserved = {
                    asset_name: {
                        "available": str(self._account_available_balances.get(asset_name, Decimal("0"))),
                        "total": str(self._account_balances.get(asset_name, Decimal("0"))),
                    }
                    for asset_name in sorted(missing_nonzero_assets)
                }
                self.logger().warning(
                    "Incomplete REST balance snapshot omitted previously non-zero asset(s) "
                    f"{sorted(missing_nonzero_assets)}; preserving cached values and pausing order creation "
                    "until REST confirms them."
                )
                self._emit_structured_event("balance_snapshot_incomplete", {
                    "missing_assets": sorted(missing_nonzero_assets),
                    "preserved_balances": preserved,
                })
        else:
            if getattr(self, "_balance_snapshot_incomplete", False):
                recovered_assets = sorted(getattr(self, "_missing_nonzero_balance_assets", set()))
                self.logger().info(
                    f"REST balance snapshot complete again; recovered omitted asset(s) {recovered_assets}."
                )
                self._emit_structured_event("balance_snapshot_complete", {
                    "recovered_assets": recovered_assets,
                })
            self._balance_snapshot_incomplete = False
            self._missing_nonzero_balance_assets.clear()

        if reconciliation_diffs:
            self.logger().info(
                f"Balance reconciliation (REST overwrote WS): "
                + " | ".join(reconciliation_diffs)
            )
            self._emit_structured_event("balance_reconciled_ws_vs_rest", {
                "diffs": reconciliation_diffs,
                "count": len(reconciliation_diffs),
            })
            # Check for large balance mismatches — trigger second reconciliation pass
            BALANCE_RECONCILIATION_THRESHOLD = Decimal("1.0")
            large_diffs = []
            for d in reconciliation_diffs:
                try:
                    delta_str = d.split("delta=")[1].split(")")[0] if "delta=" in d else "0"
                    if abs(Decimal(delta_str)) > BALANCE_RECONCILIATION_THRESHOLD:
                        large_diffs.append(d)
                except Exception:
                    pass
            if large_diffs and not self._balance_recheck_in_progress:
                self.logger().warning(
                    f"Large balance mismatch detected ({len(large_diffs)} assets). "
                    f"Scheduling second reconciliation pass."
                )
                self._emit_structured_event("balance_large_mismatch_detected", {
                    "diffs": large_diffs,
                })
                self._balance_recheck_in_progress = True
                try:
                    await asyncio.sleep(2.0)
                    await self._update_balances()
                finally:
                    self._balance_recheck_in_progress = False
                return

        # An omitted non-zero asset is an incomplete-snapshot signal, not a zero balance. Trigger
        # one prompt confirmation pass while retaining the quarantine if the omission persists.
        if missing_nonzero_assets and not self._balance_recheck_in_progress:
            self.logger().warning(
                "Scheduling confirmation balance poll for incomplete REST snapshot."
            )
            self._balance_recheck_in_progress = True
            try:
                await asyncio.sleep(2.0)
                await self._update_balances()
            finally:
                self._balance_recheck_in_progress = False
            return

        if not missing_nonzero_assets:
            self._exit_balance_settling()

        asset_names_to_remove = missing_asset_names.difference(missing_nonzero_assets)
        for asset_name in asset_names_to_remove:
            self._account_available_balances.pop(asset_name, None)
            self._account_balances.pop(asset_name, None)

        # Periodic balance health snapshot (throttled to once per 60s)
        now = time.time()
        if now - self._last_balance_health_log > 60.0:
            self._last_balance_health_log = now
            non_zero = {
                asset: {
                    "avail": f"{self._account_available_balances.get(asset, Decimal('0')):.8f}",
                    "held": f"{(self._account_balances.get(asset, Decimal('0')) - self._account_available_balances.get(asset, Decimal('0'))):.8f}",
                    "total": f"{self._account_balances.get(asset, Decimal('0')):.8f}",
                }
                for asset in self._account_balances
                if self._account_balances[asset] > Decimal("0")
            }
            if non_zero:
                summary_parts = [f"{asset}: avail={v['avail']} held={v['held']} total={v['total']}"
                                 for asset, v in sorted(non_zero.items())]
                self.logger().info(
                    f"Balance health: {self.name} | balance_source={self.balance_data_source} | "
                    + " | ".join(summary_parts)
                )
            # Include WS reconnect stats (LOG 7)
            if self._ws_reconnect_count_since_log > 0:
                self.logger().info(
                    f"WS health: {self._ws_reconnect_count_since_log} reconnect(s) since last report, "
                    f"{self._ws_reconnect_count} total this session"
                )
                self._ws_reconnect_count_since_log = 0
            # API latency summary (LOG 9)
            if self._api_latency_samples:
                latency_parts = []
                for endpoint, samples in self._api_latency_samples.items():
                    if samples:
                        avg = sum(samples) / len(samples)
                        peak = max(samples)
                        latency_parts.append(f"{endpoint}: avg={avg:.0f}ms peak={peak:.0f}ms n={len(samples)}")
                if latency_parts:
                    self.logger().info(f"API latency: {' | '.join(latency_parts)}")
            # Error rate summary (LOG 13)
            self.logger().info(f"Error summary: {self._get_and_reset_error_summary()}")

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        self.logger().debug(f"Initializing {len(exchange_info)} NonKYC trading pairs")

        for symbol_data in filter(nonkyc_utils.is_market_active, exchange_info):
            symbol = symbol_data["symbol"]
            base = symbol_data.get("primaryTicker")
            quote = symbol_data.get("secondaryTicker")

            if not base or not quote:
                # Fallback to splitting symbol
                parts = symbol.split('/')
                if len(parts) != 2:
                    self.logger().warning(f"Skipping market with unexpected symbol format: {symbol}")
                    continue
                base = base or parts[0]
                quote = quote or parts[1]

            # NKC-5: a base/quote containing a HB or exchange separator would corrupt the
            # derived trading pair (and any later split of it). 0 occurrences in the 347
            # live markets — cheap insurance against a future listing.
            if any(sep in str(base) or sep in str(quote) for sep in ("-", "_", "/")):
                self.logger().warning(
                    f"Skipping market {symbol}: base '{base}' or quote '{quote}' "
                    f"contains a separator character"
                )
                continue

            try:
                mapping[symbol] = combine_to_hb_trading_pair(base=base, quote=quote)
            except ValueDuplicationError:
                # NKC-5: a second market resolving to the same HB pair used to crash the
                # whole symbol-map build (connector dead). Keep the first mapping.
                self.logger().warning(
                    f"Duplicate trading pair for market {symbol} "
                    f"({combine_to_hb_trading_pair(base=base, quote=quote)}): "
                    f"keeping the first mapping"
                )
        self._set_trading_pair_symbol_map(mapping)

    # How long one bulk /tickers snapshot serves price lookups. Half the hummingbot-api
    # ticker-pool interval (30s): every pool cycle gets fresh data, and everything else
    # that asks in between shares the same snapshot instead of hitting the exchange.
    _TICKERS_SNAPSHOT_TTL_S = 15.0

    async def get_last_traded_prices(self, trading_pairs: List[str] = None) -> Dict[str, float]:
        """Bulk last-traded prices from ONE /tickers snapshot.

        The base implementation fans out one GET /ticker/{symbol} per pair. The
        hummingbot-api ticker pool requests EVERY listed pair (~400) every 30s, which
        turned that into a full 429 storm on 2026-07-13. Any multi-pair request is now
        served from a single cached /tickers call; a single-pair request keeps the
        fresher per-symbol endpoint (with the shared snapshot as its fallback)."""
        trading_pairs = trading_pairs or []
        if len(trading_pairs) <= 1:
            return await super().get_last_traded_prices(trading_pairs=trading_pairs)
        snapshot = await self._tickers_snapshot()
        result: Dict[str, float] = {}
        for trading_pair in trading_pairs:
            try:
                symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            except Exception:
                continue  # unknown/delisted pair: skip rather than fail the whole batch
            price = snapshot.get(symbol.replace("/", "_"))
            if price is not None:
                result[trading_pair] = price
        return result

    async def _tickers_snapshot(self) -> Dict[str, float]:
        """{ticker_id ("BASE_QUOTE"): last_price} from GET /tickers, cached for
        _TICKERS_SNAPSHOT_TTL_S. Single-flight: concurrent callers (ticker-pool warmup +
        collection loop + per-pair fallbacks) share one request instead of stampeding."""
        async with self._tickers_snapshot_lock:
            now = self._time()
            if (self._tickers_snapshot_cache is not None
                    and (now - self._tickers_snapshot_ts) < self._TICKERS_SNAPSHOT_TTL_S):
                return self._tickers_snapshot_cache
            all_tickers = await self._api_request(
                method=RESTMethod.GET,
                path_url=CONSTANTS.TICKER_BOOK_PATH_URL,
                limit_id=CONSTANTS.TICKER_BOOK_PATH_URL,
            )
            snapshot: Dict[str, float] = {}
            for ticker in all_tickers:
                ticker_id = ticker.get("ticker_id")
                last_price = ticker.get("last_price")
                if ticker_id is None or last_price is None:
                    continue
                try:
                    snapshot[ticker_id] = float(last_price)
                except (TypeError, ValueError):
                    continue
            self._tickers_snapshot_cache = snapshot
            self._tickers_snapshot_ts = now
            return snapshot

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        try:
            resp_json = await self._api_request(
                method=RESTMethod.GET,
                path_url=f"{CONSTANTS.TICKER_INFO_PATH_URL}/{symbol}",
                limit_id=CONSTANTS.TICKER_INFO_PATH_URL
            )
            return float(resp_json["last_price"])
        except Exception as primary_err:
            self.logger().debug(
                f"Primary ticker endpoint failed for {trading_pair} "
                f"(/{CONSTANTS.TICKER_INFO_PATH_URL}/{symbol}): {repr(primary_err)}. "
                f"Falling back to full tickers list."
            )
            try:
                # Shared short-TTL snapshot (single-flight): when MANY pairs fail over at
                # once (exchange blip), they share one /tickers call instead of N.
                snapshot = await self._tickers_snapshot()
                price = snapshot.get(symbol.replace("/", "_"))
                if price is None:
                    raise ValueError(f"Ticker not found for {trading_pair} in tickers list")
                return price
            except Exception as fallback_err:
                self.logger().warning(
                    f"Both ticker endpoints failed for {trading_pair}: "
                    f"primary={repr(primary_err)}, fallback={repr(fallback_err)}"
                )
                raise
