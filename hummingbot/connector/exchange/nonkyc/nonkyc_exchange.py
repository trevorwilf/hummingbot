import asyncio
import logging
import time
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from async_timeout import timeout
from bidict import bidict

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
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class NonkycExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(self,
                 nonkyc_api_key: str,
                 nonkyc_api_secret: str,
                 balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
                 rate_limits_share_pct: Decimal = Decimal("100"),
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 ):
        self.api_key = nonkyc_api_key
        self.secret_key = nonkyc_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._last_trades_poll_nonkyc_timestamp = 1.0
        self._trading_fees: Dict[str, Decimal] = {}
        self._trading_fees_last_computed: float = 0.0
        self._trading_fees_ttl: float = 3600.0  # 1 hour cache TTL
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @staticmethod
    def nonkyc_order_type(order_type: OrderType) -> str:
        """
        Map Hummingbot OrderType to NonKYC API type string.

        NOTE: LIMIT_MAKER is mapped to 'limit' because NonKYC does not support
        a native post-only/maker-only order type. Unlike Binance's LIMIT_MAKER
        (which rejects if it would take), this limit order CAN cross the spread.
        The dynamic fee system (Phase 5C) correctly classifies maker/taker fills
        using the 'triggeredBy' field from trade history.
        """
        if order_type == OrderType.LIMIT_MAKER:
            return "limit"
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
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    async def get_all_pairs_prices(self) -> List[Dict[str, str]]:
        pairs_prices = await self._api_get(path_url=CONSTANTS.TICKER_BOOK_PATH_URL)
        return pairs_prices

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        error_description = str(request_exception).lower()
        is_time_related = any(phrase in error_description for phrase in [
            "nonce",
            "timestamp",
            "signature",
            "unauthorized",
        ])
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
        is_maker = is_maker or (order_type is OrderType.LIMIT_MAKER)
        if self._trading_fees:
            fee_key = "maker_fee" if is_maker else "taker_fee"
            fee_pct = self._trading_fees.get(fee_key)
            if fee_pct is not None:
                return DeductedFromReturnsTradeFee(percent=fee_pct)
        # Fall back to static defaults from DEFAULT_FEES / fee overrides
        return DeductedFromReturnsTradeFee(percent=self.estimate_fee_pct(is_maker))

    @staticmethod
    def _extract_fee_token_and_amount(trade_data: Dict[str, Any], quote_asset: str) -> Tuple[str, Decimal]:
        """Extract fee token and amount, respecting alternateFeeAsset if present."""
        alt_asset = trade_data.get("alternateFeeAsset")
        if alt_asset:
            return alt_asset, Decimal(str(trade_data.get("alternateFee", "0")))
        # WS reports use 'tradeFee', REST uses 'fee'
        fee_amount = trade_data.get("fee") or trade_data.get("tradeFee", "0")
        return quote_asset, Decimal(str(fee_amount))

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
        if order_type is OrderType.LIMIT_MAKER:
            self.logger().debug(
                f"LIMIT_MAKER mapped to 'limit' for NonKYC (no native post-only). "
                f"Order may take liquidity if price crosses spread."
            )
        side_str = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        api_params = {"symbol": symbol,
                      "side": side_str,
                      "quantity": amount_str,
                      "type": type_str,
                      "userProvidedId": order_id}
        if order_type is OrderType.LIMIT or order_type is OrderType.LIMIT_MAKER:
            price_str = f"{price:f}"
            api_params["price"] = price_str

        try:
            order_result = await self._api_post(
                path_url=CONSTANTS.CREATE_ORDER_PATH_URL,
                data=api_params,
                is_auth_required=True)
            o_id = str(order_result["id"])
            transact_time = order_result["createdAt"] * 1e-3
        except IOError as e:
            error_description = str(e)
            is_server_overloaded = ("503" in error_description
                                    and "Unknown error, please check your request or try again later." in error_description)
            if is_server_overloaded:
                o_id = "UNKNOWN"
                transact_time = self._time_synchronizer.time()
            else:
                raise
        return o_id, transact_time

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        cancel_id = tracked_order.exchange_order_id
        if not cancel_id or cancel_id == "UNKNOWN":
            # Fallback: NonKYC API accepts cancel by userProvidedId
            cancel_id = tracked_order.client_order_id
            self.logger().info(
                f"cancel: exchange_order_id unavailable for {order_id}, "
                f"falling back to client_order_id: {cancel_id}"
            )
        api_params = {
            "id": cancel_id,
        }
        cancel_result = await self._api_post(
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True)
        if cancel_result.get("id") is not None:
            return True
        return False

    async def cancel_all(self, timeout_seconds: float) -> List[CancellationResult]:
        """
        Cancel all open orders on the exchange using batch /cancelallorders per symbol.

        This overrides the base class implementation to:
        1. Query the exchange for ALL active orders (catches orphans after crash)
        2. Use batch /cancelallorders per symbol (efficient: 1 call per pair, not 1 per order)
        3. Log orphan detection (orders on exchange but not in local tracker)

        :param timeout_seconds: maximum time to wait for cancel operations
        :return: list of CancellationResult for each tracked order
        """
        # Collect locally tracked incomplete orders for result reporting
        tracked_orders = {o.client_order_id: o for o in self.in_flight_orders.values() if not o.is_done}
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
        Uses the NonKYC /cancelallorders REST endpoint for atomic batch cancellation.

        :param trading_pair: if provided (Hummingbot format, e.g. 'BTC-USDT'),
                             only cancel orders for this pair. If None, cancel ALL.
        :return: list of cancelled order data dicts from the exchange
        """
        api_params = {}
        if trading_pair is not None:
            symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            api_params["symbol"] = symbol

        result = await self._api_post(
            path_url=CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL)

        return result if isinstance(result, list) else [result] if isinstance(result, dict) else []

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        trading_pair_rules = exchange_info_dict
        retval = []
        for rule in filter(nonkyc_utils.is_market_active, trading_pair_rules):
            try:
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=rule.get("symbol"))
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

    async def _status_polling_loop_fetch_updates(self):
        await self._update_order_fills_from_trades()
        await super()._status_polling_loop_fetch_updates()

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

            for trade in all_trades:
                try:
                    fee = Decimal(str(trade.get("fee", "0")))
                    quantity = Decimal(str(trade.get("quantity", "0")))
                    price = Decimal(str(trade.get("price", "0")))
                    notional = quantity * price

                    if notional <= 0 or fee <= 0:
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
                self.logger().info(f"Dynamic fee rates computed from trade history: {', '.join(parts)}")

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
        async for event_message in self._iter_user_event_queue():
            event_type = None
            try:
                event_type = event_message.get("method")
                if event_type == "report":
                    message_params = event_message.get('params', {})
                    reportType = message_params.get('reportType')
                    client_order_id = message_params.get("userProvidedId")

                    if reportType == "trade":
                        tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)
                        quote_asset = (message_params.get('symbol').split('/'))[1]
                        if tracked_order is not None:
                            fee_token, fee_amount = self._extract_fee_token_and_amount(message_params, quote_asset)
                            fee = TradeFeeBase.new_spot_fee(
                                fee_schema=self.trade_fee_schema(),
                                trade_type=tracked_order.trade_type,
                                percent_token=fee_token,
                                flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)]
                            )
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
                            )
                            self._order_tracker.process_trade_update(trade_update)

                    tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                    if tracked_order is not None:
                        order_update = OrderUpdate(
                            trading_pair=tracked_order.trading_pair,
                            update_timestamp=message_params["updatedAt"] * 1e-3,
                            new_state=CONSTANTS.ORDER_STATE.get(message_params["status"], OrderState.OPEN),
                            client_order_id=client_order_id,
                            exchange_order_id=str(message_params["id"]),
                        )
                        self._order_tracker.process_order_update(order_update=order_update)

                elif event_type == "currentBalances":
                    balance_entries = event_message.get("result", [])
                    for balance_entry in balance_entries:
                        asset_name = balance_entry["ticker"]
                        # WS uses 'ticker' not 'asset'; same fields: available + held (pending excluded)
                        free_balance = Decimal(balance_entry["available"])
                        total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
                        self._account_available_balances[asset_name] = free_balance
                        self._account_balances[asset_name] = total_balance

                elif event_type == "balanceUpdate":
                    balance_entry = event_message.get("params")
                    if not balance_entry:
                        self.logger().warning("Received balanceUpdate with empty params, skipping.")
                        continue
                    asset_name = balance_entry["ticker"]
                    # Incremental balance update -- same formula: available + held (pending excluded)
                    free_balance = Decimal(balance_entry["available"])
                    total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
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
                            new_state = CONSTANTS.ORDER_STATE.get(order_data.get("status", ""), OrderState.OPEN)
                            order_update = OrderUpdate(
                                trading_pair=tracked_order.trading_pair,
                                update_timestamp=order_data.get("updatedAt", 0) * 1e-3,
                                new_state=new_state,
                                client_order_id=client_order_id,
                                exchange_order_id=str(order_data.get("id", "")),
                            )
                            self._order_tracker.process_order_update(order_update)

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
            for order in self._order_tracker.all_fillable_orders.values():
                order_by_exchange_id_map[order.exchange_order_id] = order
            tasks = []
            trading_pairs = self.trading_pairs
            for trading_pair in trading_pairs:
                symbol = str(await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair))
                params = {
                    "symbol": symbol
                }

                if self._last_poll_timestamp > 0:
                    params["since"] = query_time
                tasks.append(self._api_get(
                    path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL,
                    params=params,
                    is_auth_required=True))

            self.logger().debug(f"Polling for order fills of {len(tasks)} trading pairs.")
            results = await safe_gather(*tasks, return_exceptions=True)

            for trades, trading_pair in zip(results, trading_pairs):
                base_asset, quote_asset = split_hb_trading_pair(trading_pair=trading_pair)

                if isinstance(trades, Exception):
                    self.logger().network(
                        f"Error fetching trades update for {trading_pair}: {trades}.",
                        app_warning_msg=f"Failed to fetch trade update for {trading_pair}."
                    )
                    continue
                for trade in trades:
                    exchange_order_id = str(trade["orderid"])
                    if exchange_order_id in order_by_exchange_id_map:
                        # This is a fill for a tracked order
                        tracked_order = order_by_exchange_id_map[exchange_order_id]
                        fee_token, fee_amount = self._extract_fee_token_and_amount(trade, quote_asset)
                        fee = TradeFeeBase.new_spot_fee(
                            fee_schema=self.trade_fee_schema(),
                            trade_type=tracked_order.trade_type,
                            percent_token=fee_token,
                            flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)]
                        )
                        trade_update = TradeUpdate(
                            trade_id=str(trade["id"]),
                            client_order_id=tracked_order.client_order_id,
                            exchange_order_id=exchange_order_id,
                            trading_pair=trading_pair,
                            fee=fee,
                            fill_base_amount=Decimal(trade["quantity"]),
                            fill_quote_amount=Decimal(trade["quantity"]) * Decimal(trade["price"]),
                            fill_price=Decimal(trade["price"]),
                            fill_timestamp=trade["timestamp"] * 1e-3,
                        )
                        self._order_tracker.process_trade_update(trade_update)
                    elif self.is_confirmed_new_order_filled_event(str(trade["id"]), exchange_order_id, trading_pair):
                        # This is a fill of an order registered in the DB but not tracked any more
                        self._current_trade_fills.add(TradeFillOrderDetails(
                            market=self.display_name,
                            exchange_trade_id=str(trade["id"]),
                            symbol=trading_pair))
                        _fee_token, _fee_amount = self._extract_fee_token_and_amount(trade, quote_asset)
                        self.trigger_event(
                            MarketEvent.OrderFilled,
                            OrderFilledEvent(
                                timestamp=float(trade["timestamp"]) * 1e-3,
                                order_id=self._exchange_order_ids.get(str(trade["orderid"]), None),
                                trading_pair=trading_pair,
                                trade_type=TradeType.BUY if trade["side"].lower() == "buy" else TradeType.SELL,
                                order_type=OrderType.LIMIT_MAKER if trade["side"].lower() != trade['triggeredBy'].lower() else OrderType.LIMIT,
                                price=Decimal(trade["price"]),
                                amount=Decimal(trade["quantity"]),
                                trade_fee=DeductedFromReturnsTradeFee(
                                    flat_fees=[TokenAmount(_fee_token, _fee_amount)]
                                ),
                                exchange_trade_id=str(trade["id"])
                            ))
                        self.logger().info(f"Recreating missing trade in TradeFill: {trade}")

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if order.exchange_order_id is not None:
            exchange_order_id = str(order.exchange_order_id)
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

            filtered_trades = [trade for trade in all_fills_response if trade["orderid"] == exchange_order_id]

            for trade in filtered_trades:
                fee_token, fee_amount = self._extract_fee_token_and_amount(trade, quote_asset)
                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=order.trade_type,
                    percent_token=fee_token,
                    flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)]
                )
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
                )
                trade_updates.append(trade_update)

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        # Prefer exchange_order_id (NonKYC internal id) when available;
        # fall back to client_order_id (userProvidedId) for orders not yet confirmed
        order_id_for_query = tracked_order.exchange_order_id or tracked_order.client_order_id
        updated_order_data = await self._api_get(
            path_url=f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order_id_for_query}",
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_INFO_PATH_URL)

        raw_status = updated_order_data["status"]
        new_state = CONSTANTS.ORDER_STATE.get(raw_status)
        if new_state is None:
            self.logger().warning(f"Unknown order state from NonKYC: {raw_status}")
            new_state = OrderState.OPEN

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

        balances = await self._api_get(
            path_url=CONSTANTS.USER_BALANCES_PATH_URL,
            is_auth_required=True)

        for balance_entry in balances:
            asset_name = balance_entry["asset"]
            # NonKYC balance fields:
            #   'available' = funds free for new orders
            #   'held'      = funds locked in open orders
            #   'pending'   = unconfirmed deposits/withdrawals (NOT usable for trading)
            # Total trading balance = available + held (pending excluded intentionally)
            available_balance = Decimal(balance_entry["available"])
            total_balance = Decimal(balance_entry["available"]) + Decimal(balance_entry["held"])
            self._account_available_balances[asset_name] = available_balance
            self._account_balances[asset_name] = total_balance
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

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

            mapping[symbol] = combine_to_hb_trading_pair(base=base, quote=quote)
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        try:
            resp_json = await self._api_request(
                method=RESTMethod.GET,
                path_url=f"{CONSTANTS.TICKER_INFO_PATH_URL}/{symbol}",
                limit_id=CONSTANTS.TICKER_INFO_PATH_URL
            )
            return float(resp_json["last_price"])
        except Exception:
            # Fallback: filter from tickers list
            all_tickers = await self._api_request(
                method=RESTMethod.GET,
                path_url=CONSTANTS.TICKER_BOOK_PATH_URL,
                limit_id=CONSTANTS.TICKER_BOOK_PATH_URL
            )
            ticker_id = symbol.replace("/", "_")
            for ticker in all_tickers:
                if ticker.get("ticker_id") == ticker_id:
                    return float(ticker["last_price"])
            raise ValueError(f"Ticker not found for {trading_pair}")
