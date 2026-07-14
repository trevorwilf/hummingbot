import asyncio
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS, kraken_web_utils as web_utils
from hummingbot.connector.exchange.kraken.kraken_api_order_book_data_source import KrakenAPIOrderBookDataSource
from hummingbot.connector.exchange.kraken.kraken_api_user_stream_data_source import KrakenAPIUserStreamDataSource
from hummingbot.connector.exchange.kraken.kraken_auth import KrakenAuth
from hummingbot.connector.exchange.kraken.kraken_constants import KrakenAPITier
from hummingbot.connector.exchange.kraken.kraken_utils import (
    build_rate_limits_by_tier,
    convert_from_exchange_symbol,
    convert_from_exchange_trading_pair,
)
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_numeric_client_order_id
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.utils.tracking_nonce import NonceCreator
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class KrakenExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    SHORT_POLL_INTERVAL = 30.0

    web_utils = web_utils
    REQUEST_ATTEMPTS = 5

    def __init__(self,
                 kraken_api_key: str,
                 kraken_secret_key: str,
                 balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
                 rate_limits_share_pct: Decimal = Decimal("100"),
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 kraken_api_tier: str = "starter"
                 ):
        self.api_key = kraken_api_key
        self.secret_key = kraken_secret_key
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._kraken_api_tier = KrakenAPITier(kraken_api_tier.upper() if kraken_api_tier else "STARTER")
        self._asset_pairs = {}
        # KRK-7: Ticker responses key by canonical pair name (e.g. XXBTZUSD) even when queried by
        # altname (XBTUSD); this reverse map (canonical -> altname) is built alongside _asset_pairs.
        self._canonical_to_altname: Dict[str, str] = {}
        self._client_order_id_nonce_provider = NonceCreator.for_microseconds()
        self._rate_limits_share_pct = rate_limits_share_pct
        self._throttler = self._build_async_throttler(api_tier=self._kraken_api_tier)
        # Kraken has no WS balance push and, with a healthy user stream, the REST balance poll
        # runs only every LONG_POLL_INTERVAL (120s). A fill therefore leaves cached balances
        # stale for up to 2 minutes. A WS fill triggers a debounced REST refresh instead.
        self._fill_balance_refresh_task: Optional[asyncio.Task] = None

        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @staticmethod
    def kraken_order_type(order_type: OrderType) -> str:
        return order_type.name.lower()

    @staticmethod
    def to_hb_order_type(kraken_type: str) -> OrderType:
        return OrderType[kraken_type]

    @property
    def authenticator(self):
        return KrakenAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer)

    @property
    def name(self) -> str:
        return "kraken"

    # not used
    @property
    def rate_limits_rules(self):
        return build_rate_limits_by_tier(self._kraken_api_tier)

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
        return CONSTANTS.ASSET_PAIRS_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.ASSET_PAIRS_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.TICKER_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    def _build_async_throttler(self, api_tier: KrakenAPITier) -> AsyncThrottler:
        limits_pct = self._rate_limits_share_pct
        if limits_pct < Decimal("100"):
            self.logger().warning(
                f"The Kraken API does not allow enough bandwidth for a reduced rate-limit share percentage."
                f" Current percentage: {limits_pct}."
            )
        throttler = AsyncThrottler(build_rate_limits_by_tier(api_tier))
        return throttler

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        # KRK-6: QueryOrders with an unknown txid returns `error: [], result: {}` (live-verified
        # 2026-07-14), which _request_order_status surfaces as IOError(ORDER_NOT_EXIST_ERROR_CODE ...).
        # The EOrder strings cover the explicit error-shaped variants. Without this classification,
        # process_order_not_found is never invoked from the status poll and unknown orders stay in
        # in_flight_orders forever, occupying strategy budget/level slots.
        error_text = str(status_update_exception)
        return (CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE in error_text
                or "EOrder:Invalid order" in error_text
                or "EOrder:Unknown order" in error_text)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return CONSTANTS.UNKNOWN_ORDER_MESSAGE in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return KrakenAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return KrakenAPIUserStreamDataSource(
            connector=self,
            api_factory=self._web_assistants_factory,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = order_type is OrderType.LIMIT_MAKER
        trade_base_fee = build_trade_fee(
            exchange=self.name,
            is_maker=is_maker,
            order_side=order_side,
            order_type=order_type,
            amount=amount,
            price=price,
            base_currency=base_currency,
            quote_currency=quote_currency
        )
        return trade_base_fee

    async def _api_get(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.GET
        return await self._api_request_with_retry(*args, **kwargs)

    async def _api_post(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.POST
        return await self._api_request_with_retry(*args, **kwargs)

    async def _api_put(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.PUT
        return await self._api_request_with_retry(*args, **kwargs)

    async def _api_delete(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.DELETE
        return await self._api_request_with_retry(*args, **kwargs)

    @staticmethod
    def _is_retryable_kraken_error(error_text: str) -> bool:
        # KRK-8: rate-limit and busy/unavailable-service errors are transient; failing an order (or a
        # poll) hard on the first hit turns a throttling blip into a strategy-visible failure.
        return any(message in error_text for message in CONSTANTS.RETRYABLE_ERROR_MESSAGES)

    @staticmethod
    def is_cloudflare_exception(exception: Exception):
        """
        Error status 5xx or 10xx are related to Cloudflare.
        https://support.kraken.com/hc/en-us/articles/360001491786-API-error-messages#6
        """
        return bool(re.search(r"HTTP status is (5|10)\d\d\.", str(exception)))

    async def get_open_orders_with_userref(self, userref: int):
        data = {'userref': userref}
        return await self._api_request_with_retry(RESTMethod.POST,
                                                  CONSTANTS.OPEN_ORDERS_PATH_URL,
                                                  is_auth_required=True,
                                                  data=data)

    async def get_closed_orders_with_userref(self, userref: int):
        data = {'userref': userref}
        return await self._api_request_with_retry(RESTMethod.POST,
                                                  CONSTANTS.CLOSED_ORDERS_PATH_URL,
                                                  is_auth_required=True,
                                                  data=data)

    @staticmethod
    def _orders_matching_userref(orders: Dict[str, Any], userref) -> Dict[str, Any]:
        # Kraken returns userref as an int; only adopt orders that actually carry our userref.
        return {txid: order for txid, order in orders.items()
                if str(order.get("userref", "")) == str(userref)}

    async def _reconcile_ambiguous_add_order(self, data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        KRK-4/KRK-9: after an ambiguous AddOrder outcome (Cloudflare 5xx/10xx, EService:*, request
        timeout) the order may be live — or already executed — on the exchange. Reconcile by userref
        against OpenOrders AND ClosedOrders (userref is live-verified present and reliable in both).
        Returns an AddOrder-shaped result ({"descr": ..., "txid": [...]}) when the order reached the
        exchange, or None when reconciliation positively found nothing. Errors raised by the
        reconciliation queries propagate (fail closed): the caller must not resubmit blindly when
        the order's existence could not be determined.
        """
        userref = (data or {}).get("userref")
        if userref is None:
            return None
        response = await self.get_open_orders_with_userref(userref)
        matched = self._orders_matching_userref(response.get("open", {}) or {}, userref)
        if not matched:
            response = await self.get_closed_orders_with_userref(userref)
            matched = self._orders_matching_userref(response.get("closed", {}) or {}, userref)
        if matched:
            matched_txids = list(matched.keys())
            self.logger().info(
                f"Reconciled ambiguous AddOrder via userref {userref}: found {matched_txids}.")
            # Return an AddOrder-shaped result so _place_order can read result["txid"][0],
            # marking the order OPEN rather than crashing with KeyError('txid') and FAILED.
            return {
                "descr": matched[matched_txids[0]].get("descr", {}),
                "txid": matched_txids,
            }
        return None

    # === Orders placing ===

    def _next_client_order_id(self) -> str:
        """
        Generates a new numeric client order id (Kraken userref). The 31-bit userref space allows
        birthday collisions with orders still in flight; a collision would silently overwrite the
        tracked order (plain dict assignment) and orphan the live one, so regenerate while the id
        is already tracked (bounded, in case the nonce source is degenerate).
        """
        order_id = str(get_new_numeric_client_order_id(
            nonce_creator=self._client_order_id_nonce_provider,
            max_id_bit_count=CONSTANTS.MAX_ID_BIT_COUNT,
        ))
        for _ in range(10):
            if order_id not in self._order_tracker.all_fillable_orders:
                break
            order_id = str(get_new_numeric_client_order_id(
                nonce_creator=self._client_order_id_nonce_provider,
                max_id_bit_count=CONSTANTS.MAX_ID_BIT_COUNT,
            ))
        else:
            self.logger().warning(
                f"Could not generate a collision-free client order id after 10 attempts; using {order_id}.")
        return order_id

    def buy(self,
            trading_pair: str,
            amount: Decimal,
            order_type=OrderType.LIMIT,
            price: Decimal = s_decimal_NaN,
            **kwargs) -> str:
        """
        Creates a promise to create a buy order using the parameters

        :param trading_pair: the token pair to operate with
        :param amount: the order amount
        :param order_type: the type of order to create (MARKET, LIMIT, LIMIT_MAKER)
        :param price: the order price

        :return: the id assigned by the connector to the order (the client id)
        """
        order_id = self._next_client_order_id()
        safe_ensure_future(self._create_order(
            trade_type=TradeType.BUY,
            order_id=order_id,
            trading_pair=trading_pair,
            amount=amount,
            order_type=order_type,
            price=price))
        return order_id

    def sell(self,
             trading_pair: str,
             amount: Decimal,
             order_type: OrderType = OrderType.LIMIT,
             price: Decimal = s_decimal_NaN,
             **kwargs) -> str:
        """
        Creates a promise to create a sell order using the parameters.
        :param trading_pair: the token pair to operate with
        :param amount: the order amount
        :param order_type: the type of order to create (MARKET, LIMIT, LIMIT_MAKER)
        :param price: the order price
        :return: the id assigned by the connector to the order (the client id)
        """
        order_id = self._next_client_order_id()
        safe_ensure_future(self._create_order(
            trade_type=TradeType.SELL,
            order_id=order_id,
            trading_pair=trading_pair,
            amount=amount,
            order_type=order_type,
            price=price))
        return order_id

    async def get_asset_pairs(self) -> Dict[str, Any]:
        if not self._asset_pairs:
            asset_pairs = await self._api_request_with_retry(method=RESTMethod.GET,
                                                             path_url=CONSTANTS.ASSET_PAIRS_PATH_URL)
            self._asset_pairs = {f"{details['base']}-{details['quote']}": details
                                 for _, details in asset_pairs.items()
                                 if web_utils.is_exchange_information_valid(details)
                                 and details.get('base') and details.get('quote')}
            self._canonical_to_altname = {canonical: details["altname"]
                                          for canonical, details in asset_pairs.items()
                                          if web_utils.is_exchange_information_valid(details)
                                          and details.get("altname")}
        return self._asset_pairs

    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:
        trading_pair = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        # KRK-13: str(Decimal) emits scientific notation for sub-1e-6 values (e.g. "1.2E-7");
        # serialize fixed-point, which Kraken unconditionally accepts.
        data = {
            "pair": trading_pair,
            "type": "buy" if trade_type is TradeType.BUY else "sell",
            "ordertype": "market" if order_type is OrderType.MARKET else "limit",
            "volume": f"{amount:f}",
            "userref": order_id,
            "price": f"{price:f}"
        }

        if order_type is OrderType.MARKET:
            del data["price"]
        if order_type is OrderType.LIMIT_MAKER:
            data["oflags"] = "post"
        order_result = await self._api_request_with_retry(RESTMethod.POST,
                                                          CONSTANTS.ADD_ORDER_PATH_URL,
                                                          data=data,
                                                          is_auth_required=True)

        o_id = order_result["txid"][0]
        return (o_id, self.current_timestamp)

    async def _api_request_with_retry(self,
                                      method: RESTMethod,
                                      path_url: str,
                                      params: Optional[Dict[str, Any]] = None,
                                      data: Optional[Dict[str, Any]] = None,
                                      is_auth_required: bool = False,
                                      retry_interval=2.0) -> Dict[str, Any]:
        response_json = None
        result = None
        for retry_attempt in range(self.REQUEST_ATTEMPTS):
            try:
                response_json = await self._api_request(path_url=path_url, method=method, params=params, data=data,
                                                        is_auth_required=is_auth_required)

                # Kraken returns errors as a LIST, so serialize before substring-matching (a plain `in`
                # check would only match an exact list element and miss decorated variants).
                error = response_json.get("error") or []
                error_str = " ".join(error) if isinstance(error, list) else str(error)
                if "EAPI:Invalid nonce" in error_str:
                    # Self-healing: the next attempt generates a fresh, larger nonce. WARNING (not
                    # ERROR) because a one-off out-of-order arrival between concurrent private
                    # requests is expected under load and the retry below recovers it.
                    self.logger().warning(
                        f"Invalid nonce error from {path_url}. "
                        "Please ensure your Kraken API key nonce window is at least 10, "
                        "and if needed reset your API key.")
                    # The next attempt generates a fresh, larger nonce, so retry instead of failing hard.
                    await asyncio.sleep(retry_interval ** retry_attempt)
                    continue
                result = response_json.get("result")
                # Treat an empty-but-present result (e.g. Balance {} for a zero-balance account) as success;
                # a genuine Kraken error omits the result key entirely and is caught by the error check.
                if result is None or error:
                    raise IOError({"error": response_json})
                break
            except asyncio.TimeoutError:
                if path_url == CONSTANTS.ADD_ORDER_PATH_URL:
                    # KRK-9: a timed-out AddOrder may still have been accepted. Reconcile before
                    # declaring the order failed; never blind-resubmit on an ambiguous outcome.
                    recovered = await self._reconcile_ambiguous_add_order(data=data)
                    if recovered is not None:
                        return recovered
                    raise IOError(
                        f"AddOrder request for userref {(data or {}).get('userref')} timed out and "
                        "reconciliation found no matching order; failing without retry.")
                raise
            except IOError as e:
                error_text = str(e)
                # KRK-8: AddOrder failures that are transient (EService:*, rate limits) or Cloudflare
                # errors are treated as AMBIGUOUS — the order may have reached the engine — and are
                # reconciled by userref, never blind-retried.
                is_ambiguous_add_order = (
                    path_url == CONSTANTS.ADD_ORDER_PATH_URL
                    and (self.is_cloudflare_exception(e)
                         or "EService:" in error_text
                         or self._is_retryable_kraken_error(error_text))
                )
                if is_ambiguous_add_order:
                    # KRK-4/KRK-9: order placement could have been successful despite the error.
                    # Reconcile by userref (OpenOrders + ClosedOrders) before any resubmission.
                    self.logger().info(f"Ambiguous AddOrder outcome ({error_text}); reconciling by userref.")
                    recovered = await self._reconcile_ambiguous_add_order(data=data)
                    if recovered is not None:
                        return recovered
                    if data.get("ordertype") == "market":
                        # A MARKET order never rests; if the first request was accepted it executed
                        # immediately. Resubmitting could double-execute, so a market AddOrder with
                        # no reconciled match fails WITHOUT retry.
                        raise IOError(
                            f"Ambiguous market AddOrder outcome for userref {data.get('userref')} "
                            f"({error_text}); reconciliation found no matching order. "
                            "Failing without retry.")
                    if not self.is_cloudflare_exception(e):
                        # EService:* / rate-limit on a limit AddOrder with no reconciled match: fail
                        # closed rather than blind-retrying against a busy matching engine.
                        raise e
                    # Cloudflare error on a limit AddOrder: reconciliation positively found no order,
                    # so a resubmission cannot duplicate — retry.
                    self.logger().warning(
                        f"Cloudflare error on AddOrder; no order with userref {data.get('userref')} found."
                        f" Resubmitting. Attempt {retry_attempt + 1}/{self.REQUEST_ATTEMPTS}"
                    )
                    await asyncio.sleep(retry_interval ** retry_attempt)
                    continue
                if self._is_retryable_kraken_error(error_text):
                    # KRK-8: non-AddOrder rate-limit / EService errors back off and retry instead of
                    # failing hard on the first hit (Kraken's counters decay within seconds).
                    self.logger().warning(
                        f"Retryable Kraken error ({error_text})."
                        f" Attempt {retry_attempt + 1}/{self.REQUEST_ATTEMPTS}"
                        f" API command {method}: {path_url}"
                    )
                    await asyncio.sleep(retry_interval ** retry_attempt)
                    continue
                if self.is_cloudflare_exception(e):
                    self.logger().warning(
                        f"Cloudflare error. Attempt {retry_attempt + 1}/{self.REQUEST_ATTEMPTS}"
                        f" API command {method}: {path_url}"
                    )
                    await asyncio.sleep(retry_interval ** retry_attempt)
                    continue
                else:
                    raise e
        if result is None:
            raise IOError(f"Error fetching data from {path_url}, msg is {response_json}.")
        return result

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        exchange_order_id = await tracked_order.get_exchange_order_id()
        api_params = {
            "txid": exchange_order_id,
        }
        cancel_result = await self._api_request_with_retry(
            method=RESTMethod.POST,
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True)
        # _api_request_with_retry raises on any Kraken error payload, so a returned dict is always a
        # success result. count >= 1 means at least one order was cancelled. An already-gone order surfaces
        # as a raised "Unknown order" error and is classified by _is_order_not_found_during_cancelation_error.
        return isinstance(cancel_result, dict) and cancel_result.get("count", 0) >= 1

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Example:
        {
            "XBTUSDT": {
              "altname": "XBTUSDT",
              "wsname": "XBT/USDT",
              "aclass_base": "currency",
              "base": "XXBT",
              "aclass_quote": "currency",
              "quote": "USDT",
              "lot": "unit",
              "pair_decimals": 1,
              "lot_decimals": 8,
              "lot_multiplier": 1,
              "leverage_buy": [2, 3],
              "leverage_sell": [2, 3],
              "fees": [
                [0, 0.26],
                [50000, 0.24],
                [100000, 0.22],
                [250000, 0.2],
                [500000, 0.18],
                [1000000, 0.16],
                [2500000, 0.14],
                [5000000, 0.12],
                [10000000, 0.1]
              ],
              "fees_maker": [
                [0, 0.16],
                [50000, 0.14],
                [100000, 0.12],
                [250000, 0.1],
                [500000, 0.08],
                [1000000, 0.06],
                [2500000, 0.04],
                [5000000, 0.02],
                [10000000, 0]
              ],
              "fee_volume_currency": "ZUSD",
              "margin_call": 80,
              "margin_stop": 40,
              "ordermin": "0.0002"
            }
        }
        """
        retval: list = []
        trading_pair_rules = exchange_info_dict.values()
        for rule in filter(web_utils.is_exchange_information_valid, trading_pair_rules):
            try:
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=rule.get("altname"))
                min_order_size = Decimal(rule.get('ordermin', 0))
                # Prefer Kraken's explicit tick_size (the true minimum price increment) and fall back to
                # 10^-pair_decimals (display precision) when it is absent.
                tick_size = rule.get('tick_size')
                if tick_size is not None:
                    min_price_increment = Decimal(str(tick_size))
                else:
                    min_price_increment = Decimal(f"1e-{rule.get('pair_decimals')}")
                min_base_amount_increment = Decimal(f"1e-{rule.get('lot_decimals')}")
                # costmin is Kraken's minimum order cost (notional); orders below it are rejected.
                costmin = rule.get('costmin')
                min_notional_size = Decimal(str(costmin)) if costmin is not None else Decimal("0")
                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=min_order_size,
                        min_price_increment=min_price_increment,
                        min_base_amount_increment=min_base_amount_increment,
                        min_notional_size=min_notional_size,
                    )
                )
            except Exception:
                self.logger().error(f"Error parsing the trading pair rule {rule}. Skipping.", exc_info=True)
        return retval

    async def _update_trading_rules(self):
        # Refresh the symbol map BEFORE formatting trading rules so that a new
        # listing (e.g. SN75USD class) is resolvable on the same refresh cycle.
        exchange_info = await self._make_trading_rules_request()
        self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
        trading_rules_list = await self._format_trading_rules(exchange_info)
        self._trading_rules.clear()
        for trading_rule in trading_rules_list:
            self._trading_rules[trading_rule.trading_pair] = trading_rule

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    async def _user_stream_event_listener(self):
        """
        Listens to messages from _user_stream_tracker.user_stream queue.
        Traders, Orders, and Balance updates from the WS.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                if isinstance(event_message, list):
                    channel: str = event_message[-2]
                    results: List[Any] = event_message[0]
                    if channel == CONSTANTS.USER_TRADES_ENDPOINT_NAME:
                        self._process_trade_message(results)
                    elif channel == CONSTANTS.USER_ORDERS_ENDPOINT_NAME:
                        self._process_order_message(event_message)
                elif event_message is asyncio.CancelledError:
                    raise asyncio.CancelledError
                else:
                    raise Exception(event_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    def _create_trade_update_with_order_fill_data(
            self,
            order_fill: Dict[str, Any],
            order: InFlightOrder):
        fee_asset = order.quote_asset

        fee = TradeFeeBase.new_spot_fee(
            fee_schema=self.trade_fee_schema(),
            trade_type=order.trade_type,
            percent_token=fee_asset,
            flat_fees=[TokenAmount(
                amount=Decimal(order_fill["fee"]),
                token=fee_asset
            )]
        )
        trade_update = TradeUpdate(
            trade_id=str(order_fill["trade_id"]),
            client_order_id=order.client_order_id,
            exchange_order_id=order_fill.get("ordertxid"),
            trading_pair=order.trading_pair,
            fee=fee,
            fill_base_amount=Decimal(order_fill["vol"]),
            fill_quote_amount=Decimal(order_fill["vol"]) * Decimal(order_fill["price"]),
            fill_price=Decimal(order_fill["price"]),
            # Kraken sends 'time' as a numeric string over WS ownTrades (e.g. "1560516023.070651") and as a
            # number over REST QueryTrades. Cast to float so TradeUpdate.fill_timestamp stays numeric
            # (otherwise downstream `fill_timestamp * 1e3` telemetry raises and order timestamps corrupt).
            fill_timestamp=float(order_fill["time"]),
        )
        return trade_update

    def _process_trade_message(self, trades: List):
        any_tracked_fill = False
        for update in trades:
            trade_id: str = next(iter(update))
            trade: Dict[str, str] = update[trade_id]
            trade["trade_id"] = trade_id
            exchange_order_id = trade.get("ordertxid")
            client_order_id = str(trade.get("userref", ""))
            tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)

            if not tracked_order:
                self.logger().debug(f"Ignoring trade message with id {exchange_order_id}: not in in_flight_orders.")
            else:
                trade_update = self._create_trade_update_with_order_fill_data(
                    order_fill=trade,
                    order=tracked_order)
                self._order_tracker.process_trade_update(trade_update)
                any_tracked_fill = True
        if any_tracked_fill:
            self._schedule_fill_balance_refresh()

    def _schedule_fill_balance_refresh(self):
        """Debounced REST balance refresh after a WS fill. Kraken pushes no balance updates and
        the status poll runs only every LONG_POLL_INTERVAL (120s) while the user stream is
        healthy, so without this a fill leaves cached balances stale for up to 2 minutes.
        A short sleep coalesces bursts of fills into a single Balance request."""
        if self._fill_balance_refresh_task is not None and not self._fill_balance_refresh_task.done():
            return  # a refresh is already pending; it will pick up this fill too

        async def _refresh_after_debounce():
            await self._sleep(1.0)
            try:
                await self._update_balances()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Best-effort: the periodic status poll remains the fallback sync path.
                self.logger().warning("Post-fill balance refresh failed; will retry on next poll.")

        self._fill_balance_refresh_task = safe_ensure_future(_refresh_after_debounce())

    async def stop_network(self):
        if self._fill_balance_refresh_task is not None:
            self._fill_balance_refresh_task.cancel()
            self._fill_balance_refresh_task = None
        await super().stop_network()

    def _create_order_update_with_order_status_data(self, order_status: Dict[str, Any], order: InFlightOrder):
        order_update = OrderUpdate(
            trading_pair=order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=CONSTANTS.ORDER_STATE[order_status["status"]],
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
        )
        return order_update

    def _process_order_message(self, orders: List):
        update = orders[0]
        for message in update:
            for exchange_order_id, order_msg in message.items():
                client_order_id = str(order_msg.get("userref", ""))
                tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                if not tracked_order:
                    self.logger().debug(
                        f"Ignoring order message with id {order_msg}: not in in_flight_orders.")
                    # The first openOrders message is a snapshot of EVERY open order on the account
                    # (including manual / other-bot orders sharing the API key). Skip the untracked
                    # entry and keep processing the rest of the batch instead of returning early.
                    continue
                if "status" in order_msg:
                    order_update = self._create_order_update_with_order_status_data(order_status=order_msg,
                                                                                    order=tracked_order)
                    self._order_tracker.process_order_update(order_update=order_update)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        # KRK-3: QueryTrades only accepts T-prefixed TRADE txids; querying it with the ORDER txid
        # returns EOrder:Invalid order (live-verified 2026-07-14), which the whitelist below used to
        # swallow as "no fills" — REST fill recovery was structurally dead. Fetch the order's
        # trade-id list via QueryOrders (trades=true) first, then fetch the fills themselves via
        # QueryTrades in batches of at most 20 ids.
        trade_updates = []

        try:
            exchange_order_id = await order.get_exchange_order_id()
        except asyncio.TimeoutError:
            raise IOError(f"Skipped order update with order fills for {order.client_order_id} "
                          "- waiting for exchange order id.")

        try:
            orders_response = await self._api_request_with_retry(
                method=RESTMethod.POST,
                path_url=CONSTANTS.QUERY_ORDERS_PATH_URL,
                data={"txid": exchange_order_id, "trades": "true"},
                is_auth_required=True)
            order_data = orders_response.get(exchange_order_id) or {}
            trade_ids: List[str] = list(order_data.get("trades") or [])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # An unknown/invalid order id genuinely has no fills. This whitelist applies ONLY to the
            # QueryOrders step: any error from the QueryTrades fetch below must propagate so the base
            # class logs it and retries next cycle — swallowing it would understate executed amounts.
            if "EOrder:Unknown order" in str(e) or "EOrder:Invalid order" in str(e):
                return trade_updates
            raise

        for start in range(0, len(trade_ids), CONSTANTS.QUERY_TRADES_MAX_IDS_PER_REQUEST):
            batch = trade_ids[start:start + CONSTANTS.QUERY_TRADES_MAX_IDS_PER_REQUEST]
            all_fills_response = await self._api_request_with_retry(
                method=RESTMethod.POST,
                path_url=CONSTANTS.QUERY_TRADES_PATH_URL,
                data={"txid": ",".join(batch)},
                is_auth_required=True)

            for trade_id, trade_fill in all_fills_response.items():
                trade: Dict[str, Any] = dict(trade_fill)
                trade["trade_id"] = trade_id
                trade_update = self._create_trade_update_with_order_fill_data(
                    order_fill=trade,
                    order=order)
                trade_updates.append(trade_update)
        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        updated_order_data = await self._api_request_with_retry(
            method=RESTMethod.POST,
            path_url=CONSTANTS.QUERY_ORDERS_PATH_URL,
            data={"txid": exchange_order_id},
            is_auth_required=True)

        update = updated_order_data.get(exchange_order_id)
        if update is None:
            raise IOError(f"{CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE} {exchange_order_id}")
        new_state = CONSTANTS.ORDER_STATE[update["status"]]

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=new_state,
        )

        return order_update

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()
        balances = await self._api_request_with_retry(RESTMethod.POST, CONSTANTS.BALANCE_PATH_URL,
                                                      is_auth_required=True)
        open_orders = await self._api_request_with_retry(RESTMethod.POST, CONSTANTS.OPEN_ORDERS_PATH_URL,
                                                         is_auth_required=True)

        locked = defaultdict(Decimal)

        for order in open_orders.get("open").values():
            if order.get("status") == "open":
                details = order.get("descr")
                if details.get("ordertype") == "limit":
                    pair = convert_from_exchange_trading_pair(
                        details.get("pair"), tuple((await self.get_asset_pairs()).keys())
                    )
                    if pair is None:
                        # A single unrecognized open-order pair (delisted / filtered out of asset pairs)
                        # must not crash account-wide balance polling; skip its locked contribution.
                        self.logger().warning(
                            f"Could not resolve trading pair '{details.get('pair')}' for an open order; "
                            "skipping its locked-balance contribution.")
                        continue
                    (base, quote) = self.split_trading_pair(pair)
                    vol_locked = Decimal(order.get("vol", 0)) - Decimal(order.get("vol_exec", 0))
                    if details.get("type") == "sell":
                        locked[convert_from_exchange_symbol(base)] += vol_locked
                    elif details.get("type") == "buy":
                        locked[convert_from_exchange_symbol(quote)] += vol_locked * Decimal(details.get("price"))

        # KRK-2: totals are accumulated in a fresh local dict computed ONLY from this response, and the
        # Flex/earn (".F") fold happens during accumulation. Folding in-place on self._account_* (the
        # previous implementation) double-counted against stale prior-poll state and never added the
        # folded target to remote_asset_names, so a flex-only asset (e.g. "XBT.F" with no co-held
        # "XXBT") oscillated present -> absent on alternating polls.
        total_balances: Dict[str, Decimal] = {}
        for asset_name, balance in balances.items():
            # Skip Kraken non-spot sub-balances: staked (".S"), bonded / opt-in rewards (".B"),
            # on-hold (".HOLD") and any other ".<suffix>" that is not Flex/earn (".F"). These funds are
            # not spot-tradable, so counting them would surface phantom assets (e.g. "SOL03.S") and
            # overstate available balances. Flex (".F") IS spot-liquid and folds into its spot asset.
            if "." in asset_name and not asset_name.endswith(".F"):
                continue
            if asset_name.endswith(".F"):
                cleaned_name = convert_from_exchange_symbol(asset_name.split(".")[0]).upper()
            else:
                cleaned_name = convert_from_exchange_symbol(asset_name).upper()
            total_balances[cleaned_name] = total_balances.get(cleaned_name, Decimal("0")) + Decimal(balance)

        for cleaned_name, total_balance in total_balances.items():
            self._account_available_balances[cleaned_name] = total_balance - Decimal(locked[cleaned_name])
            self._account_balances[cleaned_name] = total_balance
            remote_asset_names.add(cleaned_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(web_utils.is_exchange_information_valid, exchange_info.values()):
            # Guard per entry: a single AssetPairs row missing altname/wsname (or unresolvable) must not
            # abort the entire symbol-map build and leave the connector with zero tradable pairs.
            altname = symbol_data.get("altname")
            wsname = symbol_data.get("wsname")
            if not altname or not wsname:
                continue
            hb_pair = convert_from_exchange_trading_pair(wsname)
            if hb_pair:
                mapping[altname] = hb_pair
        self._set_trading_pair_symbol_map(mapping)

    async def get_last_traded_prices(self, trading_pairs: List[str] = None) -> Dict[str, float]:
        """
        Gets the last traded price for multiple trading pairs in a single API call.
        Assumes trading_pairs is always provided based on exchange_base implementation.
        """
        if not trading_pairs:
            return {}
        if len(trading_pairs) == 1:
            return {trading_pairs[0]: await self._get_last_traded_price(trading_pairs[0])}

        # For multiple trading pairs, get all tickers in one call and filter
        resp_json = await self._get_ticker_data()
        exchange_symbols = [await self.exchange_symbol_associated_to_pair(tp) for tp in trading_pairs]
        # Create a mapping from exchange symbols to trading pairs to avoid repeated async calls
        symbol_to_pair = {symbol: tp for symbol, tp in zip(exchange_symbols, trading_pairs)}
        # KRK-7 (live-confirmed): the all-pairs Ticker response keys by CANONICAL pair name (e.g.
        # XXBTZUSD) while the symbol map holds altnames (XBTUSD), silently omitting legacy pairs.
        # Translate response keys through the canonical->altname map built from AssetPairs. If the
        # map cannot be refreshed, degrade to raw-key matching (correct for non-legacy pairs).
        try:
            await self.get_asset_pairs()
        except Exception:
            self.logger().warning(
                "Could not refresh asset pairs for Ticker key translation; matching raw keys only.")
        results: Dict[str, float] = {}
        for symbol, data in resp_json.items():
            trading_pair = symbol_to_pair.get(symbol)
            if trading_pair is None:
                altname = self._canonical_to_altname.get(symbol)
                trading_pair = symbol_to_pair.get(altname) if altname is not None else None
            if trading_pair is not None:
                results[trading_pair] = float(data["c"][0])
        return results

    async def _get_ticker_data(self, trading_pair: str = None) -> Dict[str, Any]:
        """
        Shared method to fetch ticker data from Kraken, for one or all trading pairs.
        """
        params = {}
        if trading_pair:
            params["pair"] = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        return await self._api_request_with_retry(
            method=RESTMethod.GET,
            path_url=CONSTANTS.TICKER_PATH_URL,
            params=params
        )

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        resp_json = await self._get_ticker_data(trading_pair=trading_pair)
        record = list(resp_json.values())[0]
        return float(record["c"][0])
