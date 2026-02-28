import asyncio
import json
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses
from aioresponses.core import RequestCall

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS, nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_client_order_id
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase


class NonkycExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):

    @property
    def all_symbols_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.MARKETS_INFO_PATH_URL)

    @property
    def latest_prices_url(self):
        symbol = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        url = web_utils.public_rest_url(path_url=f"{CONSTANTS.TICKER_INFO_PATH_URL}/{symbol}")
        return url

    @property
    def network_status_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.PING_PATH_URL)

    @property
    def trading_rules_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.MARKETS_INFO_PATH_URL)

    @property
    def order_creation_url(self):
        return web_utils.private_rest_url(path_url=CONSTANTS.CREATE_ORDER_PATH_URL)

    @property
    def balance_url(self):
        return web_utils.private_rest_url(path_url=CONSTANTS.USER_BALANCES_PATH_URL)

    @property
    def all_symbols_request_mock_response(self):
        return [
            {
                "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "primaryTicker": self.base_asset,
                "primaryName": "CoinAlpha",
                "priceDecimals": 4,
                "quantityDecimals": 2,
                "isActive": True,
                "isPaused": False,
                "minimumQuantity": 2,
                "minQuote": 1,
                "isMinQuoteActive": True,
                "allowMarketOrders": True,
                "allowTriggerOrders": True,
                "bestAsk": "10001.0000",
                "bestBid": "9999.0000",
                "lastPrice": "9999.9000",
                "volume": "112345.8300",
                "id": "643bfabe4f63469320902710",
            }
        ]

    @property
    def latest_prices_request_mock_response(self):
        return {
            "ticker_id": f"{self.base_asset}_{self.quote_asset}",
            "base_currency": self.base_asset,
            "target_currency": self.quote_asset,
            "last_price": str(self.expected_latest_price),
            "bid": "9999.0000",
            "ask": "10001.0000",
            "high": "10100.0000",
            "low": "9900.0000",
            "base_volume": "112345.8300",
            "target_volume": "18969.8608",
        }

    @property
    def all_symbols_including_invalid_pair_mock_response(self) -> Tuple[str, Any]:
        response = [
            {
                "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "primaryTicker": self.base_asset,
                "primaryName": "CoinAlpha",
                "priceDecimals": 4,
                "quantityDecimals": 2,
                "isActive": True,
                "isPaused": False,
                "minimumQuantity": 2,
                "minQuote": 1,
                "isMinQuoteActive": True,
                "allowMarketOrders": True,
                "allowTriggerOrders": True,
                "bestAsk": "10001.0000",
                "bestBid": "9999.0000",
                "lastPrice": "9999.9000",
                "volume": "112345.8300",
                "id": "643bfabe4f63469320902710",
            },
            {
                "symbol": self.exchange_symbol_for_tokens("INVALID", "PAIR"),
                "primaryTicker": "INVALID",
                "primaryName": "InvalidCoin",
                "priceDecimals": 4,
                "quantityDecimals": 2,
                "isActive": False,
                "isPaused": True,
                "minimumQuantity": 1,
                "minQuote": 1,
                "isMinQuoteActive": False,
                "allowMarketOrders": False,
                "allowTriggerOrders": False,
                "bestAsk": "0",
                "bestBid": "0",
                "lastPrice": "0",
                "volume": "0",
                "id": "invalid123",
            },
        ]
        return "INVALID-PAIR", response

    @property
    def network_status_request_successful_mock_response(self):
        return {"serverTime": 1772170404982}

    @property
    def trading_rules_request_mock_response(self):
        return self.all_symbols_request_mock_response

    @property
    def trading_rules_request_erroneous_mock_response(self):
        return [
            {
                "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "primaryTicker": self.base_asset,
                "isActive": True,
            }
        ]

    @property
    def order_creation_request_successful_mock_response(self):
        return {
            "id": self.expected_exchange_order_id,
            "userProvidedId": "OID1",
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": "buy",
            "status": "New",
            "type": "limit",
            "quantity": "100",
            "price": "10000",
            "executedQuantity": "0",
            "createdAt": 1640000000000,
            "updatedAt": 1640000000000,
        }

    @property
    def balance_request_mock_response_for_base_and_quote(self):
        return [
            {"asset": self.base_asset, "available": "10.0", "held": "5.0"},
            {"asset": self.quote_asset, "available": "2000", "held": "0.00000000"},
        ]

    @property
    def balance_request_mock_response_only_base(self):
        return [
            {"asset": self.base_asset, "available": "10.0", "held": "5.0"},
        ]

    @property
    def balance_event_websocket_update(self):
        return {
            "jsonrpc": "2.0",
            "method": "currentBalances",
            "timestamp": 1772170422567,
            "portfolioData": {"totalUsdValue": "5.82", "totalBtcValue": "0.00008593"},
            "result": [
                {
                    "assetId": "abc123",
                    "ticker": self.base_asset,
                    "available": "10",
                    "held": "5",
                    "changePercent": 0,
                }
            ],
        }

    @property
    def expected_latest_price(self):
        return 9999.9

    @property
    def expected_supported_order_types(self):
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    @property
    def expected_trading_rule(self):
        return TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal("2"),
            max_order_size=Decimal("Inf"),
            min_price_increment=Decimal("0.0001"),
            min_base_amount_increment=Decimal("0.01"),
            min_notional_size=Decimal("1"),
            supports_market_orders=True,
        )

    @property
    def expected_logged_error_for_erroneous_trading_rule(self):
        erroneous_rule = self.trading_rules_request_erroneous_mock_response[0]
        return f"Error parsing the trading pair rule {erroneous_rule}. Skipping."

    @property
    def expected_exchange_order_id(self):
        return "643bfabe4f63469320902710"

    @property
    def is_order_fill_http_update_included_in_status_update(self) -> bool:
        return False

    @property
    def is_order_fill_http_update_executed_during_websocket_order_event_processing(self) -> bool:
        return False

    @property
    def expected_partial_fill_price(self) -> Decimal:
        return Decimal("10500")

    @property
    def expected_partial_fill_amount(self) -> Decimal:
        return Decimal("0.5")

    @property
    def expected_fill_fee(self) -> TradeFeeBase:
        return DeductedFromReturnsTradeFee(
            percent_token=self.quote_asset,
            flat_fees=[TokenAmount(token=self.quote_asset, amount=Decimal("30"))],
        )

    @property
    def expected_fill_trade_id(self) -> str:
        return "69914db4b3e90040c2b8f0d8"

    def exchange_symbol_for_tokens(self, base_token: str, quote_token: str) -> str:
        return f"{base_token}/{quote_token}"

    def create_exchange_instance(self):
        return NonkycExchange(
            nonkyc_api_key="testAPIKey",
            nonkyc_api_secret="testSecret",
            trading_pairs=[self.trading_pair],
        )

    def validate_auth_credentials_present(self, request_call: RequestCall):
        request_headers = request_call.kwargs.get("headers", {})
        self.assertIn("X-API-KEY", request_headers)
        self.assertIn("X-API-NONCE", request_headers)
        self.assertIn("X-API-SIGN", request_headers)

    def validate_order_creation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = json.loads(request_call.kwargs["data"])
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_data["symbol"])
        self.assertEqual(order.trade_type.name.lower(), request_data["side"])
        self.assertEqual(Decimal("100"), Decimal(request_data["quantity"]))
        self.assertEqual(Decimal("10000"), Decimal(request_data["price"]))
        self.assertEqual(order.client_order_id, request_data["userProvidedId"])

    def validate_order_cancelation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = json.loads(request_call.kwargs["data"])
        self.assertEqual(order.exchange_order_id, request_data["id"])

    def validate_order_status_request(self, order: InFlightOrder, request_call: RequestCall):
        # NonKYC uses URL path for order status: /getorder/{client_order_id}
        # No additional params to validate beyond auth
        pass

    def validate_trades_request(self, order: InFlightOrder, request_call: RequestCall):
        request_params = request_call.kwargs.get("params", {})
        self.assertEqual(
            self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            request_params.get("symbol"),
        )

    def configure_successful_cancelation_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_cancelation_request_successful_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_cancelation_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_url, status=400, callback=callback)
        return url

    def configure_order_not_found_error_cancelation_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"code": CONSTANTS.UNKNOWN_ORDER_ERROR_CODE, "msg": CONSTANTS.UNKNOWN_ORDER_MESSAGE}
        mock_api.post(regex_url, status=400, body=json.dumps(response), callback=callback)
        return url

    def configure_one_successful_one_erroneous_cancel_all_response(
        self,
        successful_order: InFlightOrder,
        erroneous_order: InFlightOrder,
        mock_api: aioresponses,
    ) -> List[str]:
        all_urls = []
        url = self.configure_successful_cancelation_response(order=successful_order, mock_api=mock_api)
        all_urls.append(url)
        url = self.configure_erroneous_cancelation_response(order=erroneous_order, mock_api=mock_api)
        all_urls.append(url)
        return all_urls

    def configure_completely_filled_order_status_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_completely_filled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_canceled_order_status_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_canceled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_open_order_status_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_open_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_http_error_order_status_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=401, callback=callback)
        return url

    def configure_partially_filled_order_status_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_partially_filled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_order_not_found_error_order_status_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> List[str]:
        url = web_utils.private_rest_url(f"{CONSTANTS.ORDER_INFO_PATH_URL}/{order.client_order_id}")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"code": CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE, "msg": CONSTANTS.ORDER_NOT_EXIST_MESSAGE}
        mock_api.get(regex_url, body=json.dumps(response), status=400, callback=callback)
        return [url]

    def configure_partial_fill_trade_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_fills_request_partial_fill_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_http_fill_trade_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=400, callback=callback)
        return url

    def configure_full_fill_trade_response(
        self,
        order: InFlightOrder,
        mock_api: aioresponses,
        callback: Optional[Callable] = None,
    ) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.ACCOUNT_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_fills_request_full_fill_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def order_event_for_new_order_websocket_update(self, order: InFlightOrder):
        return {
            "jsonrpc": "2.0",
            "method": "report",
            "params": {
                "id": order.exchange_order_id,
                "userProvidedId": order.client_order_id,
                "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "side": order.trade_type.name.lower(),
                "status": "New",
                "type": "limit",
                "quantity": str(order.amount),
                "price": str(order.price),
                "executedQuantity": "0",
                "reportType": "new",
                "createdAt": 1640000000000,
                "updatedAt": 1640000000000,
            },
        }

    def order_event_for_canceled_order_websocket_update(self, order: InFlightOrder):
        return {
            "jsonrpc": "2.0",
            "method": "report",
            "params": {
                "id": order.exchange_order_id,
                "userProvidedId": order.client_order_id,
                "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "side": order.trade_type.name.lower(),
                "status": "Cancelled",
                "type": "limit",
                "quantity": str(order.amount),
                "price": str(order.price),
                "executedQuantity": "0",
                "reportType": "canceled",
                "createdAt": 1640000000000,
                "updatedAt": 1640000000000,
            },
        }

    def order_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return {
            "jsonrpc": "2.0",
            "method": "report",
            "params": {
                "id": order.exchange_order_id,
                "userProvidedId": order.client_order_id,
                "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "side": order.trade_type.name.lower(),
                "status": "Filled",
                "type": "limit",
                "quantity": str(order.amount),
                "price": str(order.price),
                "executedQuantity": str(order.amount),
                "reportType": "trade",
                "tradeId": self.expected_fill_trade_id,
                "tradeQuantity": str(order.amount),
                "tradePrice": str(order.price),
                "tradeFee": str(self.expected_fill_fee.flat_fees[0].amount),
                "createdAt": 1640000000000,
                "updatedAt": 1640000000000,
            },
        }

    def trade_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return None

    def _order_cancelation_request_successful_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id or "dummyOrdId",
            "userProvidedId": order.client_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": "buy",
            "status": "Cancelled",
            "type": "limit",
            "quantity": str(order.amount),
            "price": str(order.price),
            "executedQuantity": "0",
            "createdAt": 1640000000000,
            "updatedAt": 1640000000000,
        }

    def _order_status_request_completely_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "userProvidedId": order.client_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": order.trade_type.name.lower(),
            "status": "Filled",
            "type": "limit",
            "quantity": str(order.amount),
            "price": str(order.price),
            "executedQuantity": str(order.amount),
            "createdAt": 1640000000000,
            "updatedAt": 1640000000000,
        }

    def _order_status_request_canceled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "userProvidedId": order.client_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": order.trade_type.name.lower(),
            "status": "Cancelled",
            "type": "limit",
            "quantity": str(order.amount),
            "price": str(order.price),
            "executedQuantity": "0",
            "createdAt": 1640000000000,
            "updatedAt": 1640000000000,
        }

    def _order_status_request_open_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "userProvidedId": order.client_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": order.trade_type.name.lower(),
            "status": "New",
            "type": "limit",
            "quantity": str(order.amount),
            "price": str(order.price),
            "executedQuantity": "0",
            "createdAt": 1640000000000,
            "updatedAt": 1640000000000,
        }

    def _order_status_request_partially_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "userProvidedId": order.client_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": order.trade_type.name.lower(),
            "status": "Partly Filled",
            "type": "limit",
            "quantity": str(order.amount),
            "price": str(order.price),
            "executedQuantity": str(self.expected_partial_fill_amount),
            "createdAt": 1640000000000,
            "updatedAt": 1640000000000,
        }

    def _order_fills_request_partial_fill_mock_response(self, order: InFlightOrder):
        return [
            {
                "id": self.expected_fill_trade_id,
                "market": {
                    "id": "66be1b9ae1d5ae4126f68be4",
                    "symbol": self.exchange_symbol_for_tokens(order.base_asset, order.quote_asset),
                },
                "orderid": order.exchange_order_id,
                "side": "Buy",
                "triggeredBy": "sell",
                "price": str(self.expected_partial_fill_price),
                "quantity": str(self.expected_partial_fill_amount),
                "fee": str(self.expected_fill_fee.flat_fees[0].amount),
                "totalWithFee": "5280.30",
                "timestamp": 1771130292620,
            }
        ]

    def _order_fills_request_full_fill_mock_response(self, order: InFlightOrder):
        return [
            {
                "id": self.expected_fill_trade_id,
                "market": {
                    "id": "66be1b9ae1d5ae4126f68be4",
                    "symbol": self.exchange_symbol_for_tokens(order.base_asset, order.quote_asset),
                },
                "orderid": order.exchange_order_id,
                "side": "Buy",
                "triggeredBy": "sell",
                "price": str(order.price),
                "quantity": str(order.amount),
                "fee": str(self.expected_fill_fee.flat_fees[0].amount),
                "totalWithFee": str(order.amount * order.price),
                "timestamp": 1771130292620,
            }
        ]
