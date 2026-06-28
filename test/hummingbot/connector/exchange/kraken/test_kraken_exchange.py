import asyncio
import json
import logging
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses
from aioresponses.core import RequestCall
from bidict import bidict

from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS, kraken_web_utils as web_utils
from hummingbot.connector.exchange.kraken.kraken_exchange import KrakenExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.event.events import MarketOrderFailureEvent
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.web_assistant.connections.data_types import RESTMethod


class KrakenExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):
    _logger = logging.getLogger(__name__)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.api_key = "someKey"
        cls.api_secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="  # noqa: mock
        cls.base_asset = "ETH"
        cls.ex_base_asset = "ETH"
        cls.quote_asset = "USDT"  # linear
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = cls.ex_base_asset + cls.quote_asset
        cls.ws_ex_trading_pairs = cls.ex_base_asset + "/" + cls.quote_asset

        # API response constants - based on Kraken Ticker API schema

        # For ETH pair
        cls.test_volume_eth = "2.45066029"  # Volume for ETH trading pair

        # For BTC pair
        cls.test_volume_btc = "1.12345678"  # Volume for BTC trading pair
        cls.btc_price_multiplier = 4.0  # BTC price is 4x the ETH price in tests

        # a - Ask array [<price>, <whole lot volume>, <lot volume>]
        cls.test_ask_price = "30300.10000"
        cls.test_ask_whole_lot_volume = "1"
        cls.test_ask_lot_volume = "1.000"

        # b - Bid array [<price>, <whole lot volume>, <lot volume>]
        cls.test_bid_price = "30300.00000"
        cls.test_bid_whole_lot_volume = "1"
        cls.test_bid_lot_volume = "1.000"

        # c - Last trade closed array [<price>, <lot volume>]
        # expected_latest_price already exists in parent class
        cls.test_latest_volume = "0.00067643"

        # v - Volume array [<today>, <last 24 hours>]
        cls.test_volume_today = "4083.67001100"
        cls.test_volume_24h = "4412.73601799"

        # p - Volume weighted average price array [<today>, <last 24 hours>]
        cls.test_vwap_today = "30706.77771"
        cls.test_vwap_24h = "30689.13205"

        # t - Number of trades array [<today>, <last 24 hours>]
        cls.test_trades_today = 34619
        cls.test_trades_24h = 38907

        # l - Low array [<today>, <last 24 hours>]
        cls.test_low_today = "29868.30000"
        cls.test_low_24h = "29868.30000"

        # h - High array [<today>, <last 24 hours>]
        cls.test_high_today = "31631.00000"
        cls.test_high_24h = "31631.00000"

        # o - Today's opening price
        cls.test_opening_price = "30502.80000"

    @property
    def all_symbols_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.ASSET_PAIRS_PATH_URL)

    @property
    def latest_prices_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.TICKER_PATH_URL)
        url = f"{url}?pair={self.ex_trading_pair}"
        return url

    @property
    def network_status_url(self):
        url = web_utils.private_rest_url(CONSTANTS.TICKER_PATH_URL)
        return url

    @property
    def trading_rules_url(self):
        url = web_utils.private_rest_url(CONSTANTS.ASSET_PAIRS_PATH_URL)
        return url

    @property
    def order_creation_url(self):
        url = web_utils.private_rest_url(CONSTANTS.ADD_ORDER_PATH_URL)
        return url

    @property
    def balance_url(self):
        url = web_utils.private_rest_url(CONSTANTS.BALANCE_PATH_URL)
        return url

    @property
    def latest_prices_request_mock_response(self):
        return {
            "error": [],
            "result": {
                self.ex_trading_pair: {
                    "a": [
                        self.test_ask_price,
                        self.test_ask_whole_lot_volume,
                        self.test_ask_lot_volume
                    ],
                    "b": [
                        self.test_bid_price,
                        self.test_bid_whole_lot_volume,
                        self.test_bid_lot_volume
                    ],
                    "c": [
                        self.expected_latest_price,
                        self.test_latest_volume
                    ],
                    "v": [
                        self.test_volume_today,
                        self.test_volume_24h
                    ],
                    "p": [
                        self.test_vwap_today,
                        self.test_vwap_24h
                    ],
                    "t": [
                        self.test_trades_today,
                        self.test_trades_24h
                    ],
                    "l": [
                        self.test_low_today,
                        self.test_low_24h
                    ],
                    "h": [
                        self.test_high_today,
                        self.test_high_24h
                    ],
                    "o": self.test_opening_price
                }
            }
        }

    @property
    def balance_event_websocket_update(self):
        pass

    @property
    def all_symbols_request_mock_response(self):
        response = {
            self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset): {
                "altname": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "wsname": f"{self.base_asset}/{self.quote_asset}",
                "aclass_base": "currency",
                "base": self.base_asset,
                "aclass_quote": "currency",
                "quote": self.quote_asset,
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
        result = {
            "error": [],
            "result": response
        }
        return result

    @property
    def all_symbols_including_invalid_pair_mock_response(self) -> Tuple[str, Any]:
        response = {
            self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset): {
                "altname": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "wsname": f"{self.base_asset}/{self.quote_asset}",
                "aclass_base": "currency",
                "base": self.base_asset,
                "aclass_quote": "currency",
                "quote": self.quote_asset,
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
            },
            "ETHUSDT.d": {
                "altname": "ETHUSDT.d",
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
        return "INVALID-PAIR", response

    @property
    def network_status_request_successful_mock_response(self):
        return {
            "error": [],
            "result": {
                "a": [
                    "30300.10000",
                    "1",
                    "1.000"
                ],
                "b": [
                    "30300.00000",
                    "1",
                    "1.000"
                ],
                "c": [
                    "30303.20000",
                    "0.00067643"
                ],
                "v": [
                    "4083.67001100",
                    "4412.73601799"
                ],
                "p": [
                    "30706.77771",
                    "30689.13205"
                ],
                "t": [
                    34619,
                    38907
                ],
                "l": [
                    "29868.30000",
                    "29868.30000"
                ],
                "h": [
                    "31631.00000",
                    "31631.00000"
                ],
                "o": "30502.80000"
            }
        }

    @property
    def trading_rules_request_mock_response(self):
        return {
            "error": [],
            "result": {
                self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset): {
                    "altname": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                    "wsname": f"{self.base_asset}/{self.quote_asset}",
                    "aclass_base": "currency",
                    "base": self.base_asset,
                    "aclass_quote": "currency",
                    "quote": self.quote_asset,
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
        }

    @property
    def trading_rules_request_erroneous_mock_response(self):
        return {
            "error": [],
            "result": {
                "XBTUSDT": {
                    "altname": "XBTUSDT",
                    "wsname": "XBT/USDT",
                    "aclass_base": "currency",
                    "base": "XXBT",
                    "aclass_quote": "currency",
                    "quote": "USDT",
                    "lot": "unit",
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
                }
            }
        }

    @property
    def order_creation_request_successful_mock_response(self):
        return {
            "error": [],
            "result": {
                "descr": {
                    "order": "",
                },
                "txid": [
                    self.expected_exchange_order_id,
                ]
            }
        }

    @property
    def balance_request_mock_response_for_base_and_quote(self):
        return {
            "error": [],
            "result": {
                self.base_asset: str(10),
                self.quote_asset: str(2000),
            }
        }

    @property
    def balance_request_mock_response_only_base(self):
        return {
            self.base_asset: str(10),
        }

    @property
    def expected_latest_price(self):
        return 9999.9

    @property
    def expected_supported_order_types(self):
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    @property
    def expected_trading_rule(self):
        rule = list(self.trading_rules_request_mock_response["result"].values())[0]
        min_order_size = Decimal(rule.get('ordermin', 0))
        min_price_increment = Decimal(f"1e-{rule.get('pair_decimals')}")
        min_base_amount_increment = Decimal(f"1e-{rule.get('lot_decimals')}")
        return TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=min_order_size,
            min_price_increment=min_price_increment,
            min_base_amount_increment=min_base_amount_increment,
        )

    @property
    def expected_logged_error_for_erroneous_trading_rule(self):
        erroneous_rule = list(self.trading_rules_request_erroneous_mock_response["result"].values())[0]
        return f"Error parsing the trading pair rule {erroneous_rule}. Skipping."

    @property
    def expected_exchange_order_id(self):
        return 28

    @property
    def is_order_fill_http_update_included_in_status_update(self) -> bool:
        return False

    @property
    def is_order_fill_http_update_executed_during_websocket_order_event_processing(self) -> bool:
        return False

    @property
    def expected_partial_fill_price(self) -> Decimal:
        return Decimal(10500)

    @property
    def expected_partial_fill_amount(self) -> Decimal:
        return Decimal("0.5")

    @property
    def expected_fill_fee(self) -> TradeFeeBase:
        return AddedToCostTradeFee(
            percent_token=self.quote_asset,
            flat_fees=[TokenAmount(token=self.quote_asset, amount=Decimal("30"))])

    @property
    def expected_fill_trade_id(self) -> str:
        return str(30000)

    def exchange_symbol_for_tokens(self, base_token: str, quote_token: str) -> str:
        return f"{base_token}{quote_token}"

    def create_exchange_instance(self):
        return KrakenExchange(
            kraken_api_key=self.api_key,
            kraken_secret_key=self.api_secret,
            trading_pairs=[self.trading_pair],
        )

    def validate_auth_credentials_present(self, request_call: RequestCall):
        self._validate_auth_credentials_taking_parameters_from_argument(
            request_call_tuple=request_call,
            params=request_call.kwargs["data"]
        )

    def validate_order_creation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = request_call.kwargs["data"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_data["pair"])
        self.assertEqual(order.trade_type.name.lower(), request_data["type"])
        self.assertEqual(KrakenExchange.kraken_order_type(OrderType.LIMIT), request_data["ordertype"])
        self.assertEqual(Decimal("100"), Decimal(request_data["volume"]))
        self.assertEqual(Decimal("10000"), Decimal(request_data["price"]))

    def validate_order_cancelation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = request_call.kwargs["data"]
        self.assertEqual(order.exchange_order_id, request_data["txid"])

    def validate_order_status_request(self, order: InFlightOrder, request_call: RequestCall):
        request_params = request_call.kwargs["data"]
        self.assertEqual(order.exchange_order_id, request_params["txid"])

    def validate_trades_request(self, order: InFlightOrder, request_call: RequestCall):
        request_params = request_call.kwargs["data"]
        self.assertEqual(order.exchange_order_id, str(request_params["txid"]))

    def configure_order_not_found_error_cancelation_response(
            self, order: InFlightOrder, mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None
    ) -> str:
        # Implement the expected not found response when enabling test_cancel_order_not_found_in_the_exchange
        raise NotImplementedError

    def configure_successful_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_cancelation_request_successful_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_url, status=400, callback=callback)
        return url

    def configure_one_successful_one_erroneous_cancel_all_response(
            self,
            successful_order: InFlightOrder,
            erroneous_order: InFlightOrder,
            mock_api: aioresponses) -> List[str]:
        """
        :return: a list of all configured URLs for the cancelations
        """
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
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?") + ".*")
        response = self._order_status_request_completely_filled_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_canceled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?") + ".*")
        response = self._order_status_request_canceled_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_http_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.QUERY_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_url, status=400, callback=callback)
        return url

    def configure_open_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        """
        :return: the URL configured
        """
        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_open_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_http_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_url, status=401, callback=callback)
        return url

    def configure_partially_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_partially_filled_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_order_not_found_error_order_status_response(
            self, order: InFlightOrder, mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None
    ) -> List[str]:
        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"code": -2013, "msg": "Order does not exist."}
        mock_api.post(regex_url, body=json.dumps(response), status=400, callback=callback)
        return [url]

    def configure_partial_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.QUERY_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_fills_request_partial_fill_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_full_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.QUERY_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_fills_request_full_fill_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def order_event_for_new_order_websocket_update(self, order: InFlightOrder):
        return [
            [
                {
                    order.exchange_order_id: {
                        "avg_price": "34.50000",
                        "cost": "0.00000",
                        "descr": {
                            "close": "",
                            "leverage": "0:1",
                            "order": "sell 10.00345345 XBT/EUR @ limit 34.50000 with 0:1 leverage",
                            "ordertype": "limit",
                            "pair": self.ws_ex_trading_pairs,
                            "price": str(order.price),
                            "price2": "0.00000",
                            "type": "sell"
                        },
                        "expiretm": "0.000000",
                        "fee": "0.00000",
                        "limitprice": "34.50000",
                        "misc": "",
                        "oflags": "fcib",
                        "opentm": "0.000000",
                        "refid": "OKIVMP-5GVZN-Z2D2UA",
                        "starttm": "0.000000",
                        "status": "open",
                        "stopprice": "0.000000",
                        "userref": order.client_order_id,
                        "vol": str(order.amount, ),
                        "vol_exec": "0.00000000"
                    }
                }
            ],
            "openOrders",
            {
                "sequence": 234
            }
        ]

    def order_event_for_canceled_order_websocket_update(self, order: InFlightOrder):
        return [
            [
                {
                    order.exchange_order_id: {
                        "avg_price": "34.50000",
                        "cost": "0.00000",
                        "descr": {
                            "close": "",
                            "leverage": "0:1",
                            "order": "sell 10.00345345 XBT/EUR @ limit 34.50000 with 0:1 leverage",
                            "ordertype": "limit",
                            "pair": "XBT/EUR",
                            "price": "34.50000",
                            "price2": "0.00000",
                            "type": "sell"
                        },
                        "expiretm": "0.000000",
                        "fee": "0.00000",
                        "limitprice": "34.50000",
                        "misc": "",
                        "oflags": "fcib",
                        "opentm": "0.000000",
                        "refid": "OKIVMP-5GVZN-Z2D2UA",
                        "starttm": "0.000000",
                        "status": "canceled",
                        "stopprice": "0.000000",
                        "userref": order.client_order_id,
                        "vol": "10.00345345",
                        "vol_exec": "0.00000000"
                    }
                }
            ],
            "openOrders",
            {
                "sequence": 234
            }
        ]

    def order_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return [
            [
                {
                    order.exchange_order_id: {
                        "avg_price": "34.50000",
                        "cost": "0.00000",
                        "descr": {
                            "close": "",
                            "leverage": "0:1",
                            "order": "sell 10.00345345 XBT/EUR @ limit 34.50000 with 0:1 leverage",
                            "ordertype": "limit",
                            "pair": "XBT/EUR",
                            "price": order.price,
                            "price2": "0.00000",
                            "type": "sell"
                        },
                        "expiretm": "0.000000",
                        "fee": "0.00000",
                        "limitprice": "34.50000",
                        "misc": "",
                        "oflags": "fcib",
                        "opentm": "0.000000",
                        "refid": "OKIVMP-5GVZN-Z2D2UA",
                        "starttm": "0.000000",
                        "status": "closed",
                        "stopprice": "0.000000",
                        "userref": order.client_order_id,
                        "vol": order.amount,
                        "vol_exec": "0.00000000"
                    }
                }
            ],
            "openOrders",
            {
                "sequence": 234
            }
        ]

    def trade_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return [
            [
                {
                    self.expected_fill_trade_id: {
                        "cost": "1000000.00000",
                        "fee": Decimal(self.expected_fill_fee.flat_fees[0].amount),
                        "margin": "0.00000",
                        "ordertxid": order.exchange_order_id,
                        "ordertype": "limit",
                        "pair": "XBT/EUR",
                        "postxid": "OGTT3Y-C6I3P-XRI6HX",
                        "price": str(order.price),
                        "time": "1560516023.070651",
                        "type": "sell",
                        "userref": order.client_order_id,
                        "vol": str(order.amount)
                    }
                }
            ],
            "ownTrades",
            {
                "sequence": 2948
            }
        ]

    @aioresponses()
    def test_lost_order_removed_if_not_found_during_order_status_update(self, mock_api):
        # Disabling this test because the connector has not been updated yet to validate
        # order not found during status update (check _is_order_not_found_during_status_update_error)
        pass

    @aioresponses()
    @patch("hummingbot.connector.time_synchronizer.TimeSynchronizer._current_seconds_counter")
    def test_update_time_synchronizer_successfully(self, mock_api, seconds_counter_mock):
        pass

    @aioresponses()
    def test_cancel_order_not_found_in_the_exchange(self, mock_api):
        # Disabling this test because the connector has not been updated yet to validate
        # order not found during cancellation (check _is_order_not_found_during_cancelation_error)
        pass

    def test_user_stream_balance_update(self):
        pass

    @aioresponses()
    def test_update_time_synchronizer_failure_is_logged(self, mock_api):
        pass

    @aioresponses()
    def test_update_time_synchronizer_raises_cancelled_error(self, mock_api):
        pass

    @aioresponses()
    def test_update_order_status_when_order_has_not_changed_and_one_partial_fill(self, mock_api):
        pass

    @aioresponses()
    def test_check_network_failure(self, mock_api):
        url = self.network_status_url
        mock_api.get(url, status=600)

        ret = self.async_run_with_timeout(coroutine=self.exchange.check_network())

        self.assertEqual(ret, NetworkStatus.NOT_CONNECTED)

    @aioresponses()
    def test_update_order_status_when_failed(self, mock_api):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="100234",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders["OID1"]

        url = web_utils.private_rest_url(CONSTANTS.QUERY_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        order_status = {
            order.exchange_order_id: {
                "refid": "None",
                "userref": order.client_order_id,
                "status": "expired",
                "opentm": 1499827319.559,
                "starttm": 0,
                "expiretm": 0,
                "descr": {},
                "vol": "1.0",
                "vol_exec": "0.0",
                "cost": "11253.7",
                "fee": "0.00000",
                "price": "10000.0",
                "stopprice": "0.00000",
                "limitprice": "0.00000",
                "misc": "",
                "oflags": "fciq",
                "trades": []
            }
        }

        mock_response = {
            "error": [],
            "result": order_status
        }
        mock_api.post(regex_url, body=json.dumps(mock_response))

        self.async_run_with_timeout(self.exchange._update_order_status())

        request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(request)
        request_params = request.kwargs["data"]
        self.assertEqual(order.exchange_order_id, request_params["txid"])

        failure_event: MarketOrderFailureEvent = self.order_failure_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, failure_event.timestamp)
        self.assertEqual(order.client_order_id, failure_event.order_id)
        self.assertEqual(order.order_type, failure_event.order_type)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
        self.assertTrue(
            self.is_logged(
                "INFO",
                f"Order {order.client_order_id} has failed. Order Update: OrderUpdate(trading_pair='{self.trading_pair}',"
                f" update_timestamp={self.exchange.current_timestamp}, new_state={repr(OrderState.FAILED)}, "
                f"client_order_id='{order.client_order_id}', exchange_order_id='{order.exchange_order_id}', "
                "misc_updates=None, exchange_timestamp_ms=None, received_timestamp_ms=None, source_channel=None)")
        )

    @patch("hummingbot.connector.exchange.kraken.kraken_exchange.get_new_numeric_client_order_id")
    def test_client_order_id_on_order(self, mock_ts):
        mock_ts.return_value = 7
        result = self.exchange.buy(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        )
        expected_client_order_id = str(7)

        self.assertEqual(result, expected_client_order_id)

        result = self.exchange.sell(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        )
        expected_client_order_id = str(7)

        self.assertEqual(result, expected_client_order_id)

    def _validate_auth_credentials_taking_parameters_from_argument(self,
                                                                   request_call_tuple: RequestCall,
                                                                   params: Dict[str, Any]):
        self.assertIn("nonce", params)
        request_headers = request_call_tuple.kwargs["headers"]
        self.assertIn("API-Sign", request_headers)
        self.assertIn("API-Key", request_headers)
        self.assertEqual("someKey", request_headers["API-Key"])

    def get_asset_pairs_mock(self) -> Dict:
        asset_pairs = {
            f"X{self.base_asset}{self.quote_asset}": {
                "altname": f"{self.base_asset}{self.quote_asset}",
                "wsname": f"{self.base_asset}/{self.quote_asset}",
                "aclass_base": "currency",
                "base": f"{self.base_asset}",
                "aclass_quote": "currency",
                "quote": f"{self.quote_asset}",
                "lot": "unit",
                "pair_decimals": 5,
                "lot_decimals": 8,
                "lot_multiplier": 1,
                "leverage_buy": [
                    2,
                    3,
                ],
                "leverage_sell": [
                    2,
                    3,
                ],
                "fees": [
                    [
                        0,
                        0.26
                    ],
                    [
                        50000,
                        0.24
                    ],
                ],
                "fees_maker": [
                    [
                        0,
                        0.16
                    ],
                    [
                        50000,
                        0.14
                    ],
                ],
                "fee_volume_currency": "ZUSD",
                "margin_call": 80,
                "margin_stop": 40,
                "ordermin": "0.005"
            },
        }
        result = {
            "error": [],
            "result": asset_pairs
        }
        return result

    def get_balances_mock(self, base_asset_balance: float, quote_asset_balance: float) -> Dict:
        balances = {
            "error": [],
            "result": {
                self.base_asset: str(base_asset_balance),
                f'{self.base_asset}.F': "1",
                self.quote_asset: str(quote_asset_balance),
                "USDT": "171288.6158",
            }
        }
        return balances

    def get_open_orders_mock(self, quantity: float, price: float, order_type: str) -> Dict:
        open_orders = {
            "open": {
                "OQCLML-BW3P3-BUCMWZ": self.get_order_status_mock(quantity, price, order_type, status="open"),
            }
        }
        result = {
            "error": [],
            "result": open_orders
        }
        return result

    def get_order_status_mock(self, quantity: float, price: float, order_type: str, status: str) -> Dict:
        order_status = {
            "refid": None,
            "userref": 0,
            "status": status,
            "opentm": 1616666559.8974,
            "starttm": 0,
            "expiretm": 0,
            "descr": {
                "pair": f"{self.base_asset}{self.quote_asset}",
                "type": order_type,
                "ordertype": "limit",
                "price": str(price),
                "price2": "0",
                "leverage": "none",
                "order": f"buy {quantity} {self.base_asset}{self.quote_asset} @ limit {price}",
                "close": ""
            },
            "vol": str(quantity),
            "vol_exec": "0",
            "cost": str(price * quantity),
            "fee": "0.00000",
            "price": str(price),
            "stopprice": "0.00000",
            "limitprice": "0.00000",
            "misc": "",
            "oflags": "fciq",
            "trades": [
                "TCCCTY-WE2O6-P3NB37"
            ]
        }
        return order_status

    @aioresponses()
    def test_update_balances(self, mocked_api):
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.ASSET_PAIRS_PATH_URL}"
        resp = self.get_asset_pairs_mock()
        mocked_api.get(url, body=json.dumps(resp))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.BALANCE_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        resp = self.get_balances_mock(base_asset_balance=10, quote_asset_balance=20)
        mocked_api.post(regex_url, body=json.dumps(resp))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.OPEN_ORDERS_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        resp = self.get_open_orders_mock(quantity=1, price=2, order_type="buy")
        mocked_api.post(regex_url, body=json.dumps(resp))

        self.async_run_with_timeout(self.exchange._update_balances())

        self.assertEqual(self.exchange.available_balances[self.quote_asset], Decimal("171286.6158"))
        self.assertEqual(self.exchange.available_balances[self.base_asset], Decimal("11"))

    def _order_cancelation_request_successful_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "error": [],
            "result": {
                "count": 1
            }
        }

    def _order_status_request_completely_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "error": [],
            "result": {
                order.exchange_order_id: {
                    "refid": "None",
                    "userref": order.client_order_id,
                    "status": "closed",
                    "opentm": 1688666559.8974,
                    "starttm": 0,
                    "expiretm": 0,
                    "descr": {},
                    "vol": str(order.amount),
                    "vol_exec": str(order.amount),
                    "cost": "11253.7",
                    "fee": "0.00000",
                    "price": str(order.price),
                    "stopprice": "0.00000",
                    "limitprice": "0.00000",
                    "misc": "",
                    "oflags": "fciq",
                    "trades": []
                }
            }
        }

    def _order_status_request_canceled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "error": [],
            "result": {
                order.exchange_order_id: {
                    "refid": "None",
                    "userref": order.client_order_id,
                    "status": "canceled",
                    "opentm": 1688666559.8974,
                    "starttm": 0,
                    "expiretm": 0,
                    "descr": {},
                    "vol": str(order.amount),
                    "vol_exec": "0",
                    "cost": "11253.7",
                    "fee": "0.00000",
                    "price": str(order.price),
                    "stopprice": "0.00000",
                    "limitprice": "0.00000",
                    "misc": "",
                    "oflags": "fciq",
                    "trades": []
                }
            }
        }

    def _order_status_request_open_mock_response(self, order: InFlightOrder) -> Any:
        return {
            order.exchange_order_id: {
                "refid": "None",
                "userref": order.client_order_id,
                "status": "open",
                "opentm": 1688666559.8974,
                "starttm": 0,
                "expiretm": 0,
                "descr": {},
                "vol": str(order.amount),
                "vol_exec": "0",
                "cost": "11253.7",
                "fee": "0.00000",
                "price": str(order.price),
                "stopprice": "0.00000",
                "limitprice": "0.00000",
                "misc": "",
                "oflags": "fciq",
                "trades": []
            }
        }

    def _order_status_request_partially_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            order.exchange_order_id: {
                "refid": "None",
                "userref": order.client_order_id,
                "status": "open",
                "opentm": 1688666559.8974,
                "starttm": 0,
                "expiretm": 0,
                "descr": {},
                "vol": str(order.amount),
                "vol_exec": str(order.amount / 2),
                "cost": "11253.7",
                "fee": "0.00000",
                "price": str(order.price),
                "stopprice": "0.00000",
                "limitprice": "0.00000",
                "misc": "",
                "oflags": "fciq",
                "trades": []
            }
        }

    def _order_fills_request_partial_fill_mock_response(self, order: InFlightOrder):
        return {
            self.expected_fill_trade_id: {
                "ordertxid": order.exchange_order_id,
                "postxid": "TKH2SE-M7IF5-CFI7LT",
                "pair": "XXBTZUSD",
                "time": 1499865549.590,
                "type": "buy",
                "ordertype": "limit",
                "price": str(self.expected_partial_fill_price),
                "cost": "600.20000",
                "fee": str(self.expected_fill_fee.flat_fees[0].amount),
                "vol": str(self.expected_partial_fill_amount),
                "margin": "0.00000",
                "misc": "",
                "trade_id": 93748276,
                "maker": "true"
            }
        }

    def _order_fills_request_full_fill_mock_response(self, order: InFlightOrder):
        return {
            "error": [],
            "result": {
                self.expected_fill_trade_id: {
                    "ordertxid": order.exchange_order_id,
                    "postxid": "TKH2SE-M7IF5-CFI7LT",
                    "pair": "XXBTZUSD",
                    "time": 1499865549.590,
                    "type": "buy",
                    "ordertype": "limit",
                    "price": str(order.price),
                    "cost": "600.20000",
                    "fee": str(self.expected_fill_fee.flat_fees[0].amount),
                    "vol": str(order.amount),
                    "margin": "0.00000",
                    "misc": "",
                    "trade_id": 93748276,
                    "maker": "true"
                }
            }
        }

    def test_is_order_not_found_during_cancelation_error(self):
        # Test for line 654 in kraken_exchange.py
        exception_with_unknown_order = Exception(f"Some text with {CONSTANTS.UNKNOWN_ORDER_MESSAGE} in it")
        exception_without_unknown_order = Exception("Some other error message")

        self.assertTrue(self.exchange._is_order_not_found_during_cancelation_error(exception_with_unknown_order))
        self.assertFalse(self.exchange._is_order_not_found_during_cancelation_error(exception_without_unknown_order))

    @aioresponses()
    async def test_get_last_traded_price_single_pair(self, mock_api):
        # Test for _get_last_traded_price method
        url = web_utils.public_rest_url(CONSTANTS.TICKER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        # Use data from latest_prices_request_mock_response but only include the 'c' field that's needed
        first_pair_data = self.latest_prices_request_mock_response["result"][self.ex_trading_pair]

        # Create more concise mock response with only required fields for this test
        mock_response = {
            "error": [],
            "result": {
                self.ex_trading_pair: {
                    "c": first_pair_data["c"]  # Only need the 'c' field for last traded price
                }
            }
        }
        mock_api.get(regex_url, body=json.dumps(mock_response))

        # Get last traded price for single pair
        price = await self.exchange._get_last_traded_price(self.trading_pair)

        # Verify the result
        self.assertEqual(float(self.expected_latest_price), price)

    @aioresponses()
    async def test_get_last_traded_prices_empty(self, mock_api):
        # Test get_last_traded_prices with empty list
        prices = await self.exchange.get_last_traded_prices([])

        # Should return empty dict
        self.assertEqual({}, prices)

    @aioresponses()
    @patch("hummingbot.connector.exchange.kraken.kraken_exchange.KrakenExchange.exchange_symbol_associated_to_pair")
    async def test_get_last_traded_prices_multiple_pairs(self, mock_api, mock_exchange_symbol):
        # Test get_last_traded_prices with multiple trading pairs
        trading_pairs = [self.trading_pair, "BTC-USDT"]
        exchange_symbols = [self.ex_trading_pair, "BTCUSDT"]

        mock_exchange_symbol.side_effect = exchange_symbols

        url = web_utils.public_rest_url(CONSTANTS.TICKER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        btc_price = self.expected_latest_price * self.btc_price_multiplier

        # Extract first trading pair data from latest_prices_request_mock_response
        first_pair_data = self.latest_prices_request_mock_response["result"][self.ex_trading_pair]

        mock_response = {
            "error": [],
            "result": {
                exchange_symbols[0]: first_pair_data,
                # Second pair - only include the 'c' field which is needed for this test
                exchange_symbols[1]: {
                    "c": [
                        str(btc_price),
                        self.test_latest_volume
                    ]
                }
            }
        }
        mock_api.get(regex_url, body=json.dumps(mock_response))

        prices = await self.exchange.get_last_traded_prices(trading_pairs)

        expected_prices = {
            trading_pairs[0]: float(self.expected_latest_price),
            trading_pairs[1]: float(btc_price)
        }
        self.assertEqual(expected_prices, prices)

        self.assertEqual(2, mock_exchange_symbol.call_count)
        mock_exchange_symbol.assert_any_call(trading_pairs[0])
        mock_exchange_symbol.assert_any_call(trading_pairs[1])

    @aioresponses()
    async def test_get_ticker_data(self, mock_api):
        # Test _get_ticker_data method
        url = web_utils.public_rest_url(CONSTANTS.TICKER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        # Use existing mock response from latest_prices_request_mock_response
        mock_api.get(regex_url, body=json.dumps(self.latest_prices_request_mock_response))

        # Get ticker data without specifying trading pair (all tickers)
        ticker_data = await self.exchange._get_ticker_data()

        # Verify the result
        self.assertEqual(self.latest_prices_request_mock_response["result"], ticker_data)

        # Get ticker data for specific trading pair
        with patch("hummingbot.connector.exchange.kraken.kraken_exchange.KrakenExchange.exchange_symbol_associated_to_pair",
                   return_value=self.ex_trading_pair):
            # Use a separate test for this part to avoid URL matching issues
            mock_api.get(f"{url}?pair={self.ex_trading_pair}", body=json.dumps(self.latest_prices_request_mock_response))
            ticker_data = await self.exchange._get_ticker_data(trading_pair=self.trading_pair)

            # Verify the result
            self.assertEqual(self.latest_prices_request_mock_response["result"], ticker_data)

    # ------------------------------------------------------------------
    # Regression tests for audited bug fixes
    # ------------------------------------------------------------------

    def _track_simple_order(self, order_id: str, exchange_order_id: str,
                            trade_type: TradeType = TradeType.BUY,
                            price: Decimal = Decimal("100"),
                            amount: Decimal = Decimal("1")) -> InFlightOrder:
        self.exchange.start_tracking_order(
            order_id=order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=trade_type,
            price=price,
            amount=amount,
        )
        return self.exchange.in_flight_orders[order_id]

    async def test_process_order_message_batch_skips_untracked_order(self):
        # ORDERS-1: the first openOrders snapshot includes every account order (incl. foreign ones). An
        # untracked order earlier in the batch must NOT drop the status update of a later tracked order.
        self.exchange._set_current_timestamp(1640780000)
        tracked = self._track_simple_order("OID-TRACKED", "OUR-TXID")
        self.exchange._set_current_timestamp(1640780100)

        # The foreign (untracked) order is FIRST in the batch; with the old `return` bug the tracked
        # order's update that follows it would be dropped and the order would stay PENDING_CREATE.
        message = [
            [
                {"FOREIGN-TXID": {"userref": 999999, "status": "open"}},
                {tracked.exchange_order_id: {"userref": tracked.client_order_id, "status": "open"}},
            ],
            "openOrders",
            {"sequence": 1},
        ]
        self.exchange._process_order_message(message)
        # process_order_update schedules the state change via safe_ensure_future; let it run.
        await asyncio.sleep(0.1)
        self.assertEqual(OrderState.OPEN, tracked.current_state)

    def test_process_trade_message_ws_fill_timestamp_is_float(self):
        # ORDERS-2: WS ownTrades 'time' arrives as a string; it must be cast to float so downstream
        # timestamp math/telemetry does not break and the order timestamp stays numeric.
        self.exchange._set_current_timestamp(1640780000)
        order = self._track_simple_order(
            "OID-FILL", "OUR-TXID-2", trade_type=TradeType.SELL,
            price=Decimal("34.5"), amount=Decimal("10.00345345"))
        trade_msg = self.trade_event_for_full_fill_websocket_update(order)

        self.exchange._process_trade_message(trade_msg[0])

        self.assertIsInstance(order.last_update_timestamp, float)
        self.assertEqual(1560516023.070651, order.last_update_timestamp)

    async def test_all_trade_updates_reraises_unexpected_error(self):
        # ORDERS-3: a transient error (non "Unknown order") must propagate, not be swallowed as "no fills".
        self.exchange._set_current_timestamp(1640780000)
        order = self._track_simple_order("OID-3", "TXID-3")
        with patch.object(self.exchange, "_api_request_with_retry",
                          new=AsyncMock(side_effect=IOError("Error, HTTP status is 503."))):
            with self.assertRaises(IOError):
                await self.exchange._all_trade_updates_for_order(order)

    async def test_all_trade_updates_returns_empty_on_unknown_order(self):
        # ORDERS-3 companion: an unknown/invalid order id legitimately has no fills -> empty list.
        self.exchange._set_current_timestamp(1640780000)
        order = self._track_simple_order("OID-3b", "TXID-3b")
        with patch.object(self.exchange, "_api_request_with_retry",
                          new=AsyncMock(side_effect=IOError("EOrder:Unknown order"))):
            result = await self.exchange._all_trade_updates_for_order(order)
        self.assertEqual([], result)

    async def test_request_order_status_raises_when_order_absent(self):
        # ORDERS-7: a QueryOrders result lacking our txid must raise a clean IOError, not a TypeError.
        self.exchange._set_current_timestamp(1640780000)
        order = self._track_simple_order("OID-7", "TXID-7")
        with patch.object(self.exchange, "_api_request_with_retry", new=AsyncMock(return_value={})):
            with self.assertRaises(IOError):
                await self.exchange._request_order_status(order)

    async def test_place_cancel_true_on_count_false_otherwise(self):
        # ORDERS-8: cancellation success is keyed solely on count >= 1 (the dead error clause is removed).
        self.exchange._set_current_timestamp(1640780000)
        order = self._track_simple_order("OID-8", "TXID-8")
        with patch.object(self.exchange, "_api_request_with_retry", new=AsyncMock(return_value={"count": 1})):
            self.assertTrue(await self.exchange._place_cancel("OID-8", order))
        with patch.object(self.exchange, "_api_request_with_retry", new=AsyncMock(return_value={"count": 0})):
            self.assertFalse(await self.exchange._place_cancel("OID-8", order))

    async def test_api_request_with_retry_accepts_empty_result(self):
        # AUTH-5: an empty-but-present result (Balance {} for a zero-balance account) is a success.
        with patch.object(self.exchange, "_api_request",
                          new=AsyncMock(return_value={"error": [], "result": {}})):
            result = await self.exchange._api_request_with_retry(
                method=RESTMethod.POST, path_url=CONSTANTS.BALANCE_PATH_URL, is_auth_required=True)
        self.assertEqual({}, result)

    async def test_api_request_with_retry_retries_on_invalid_nonce(self):
        # AUTH-3: invalid-nonce errors (a LIST) are detected via serialized matching and retried.
        api_request_mock = AsyncMock(side_effect=[
            {"error": ["EAPI:Invalid nonce"], "result": None},
            {"error": [], "result": {"ok": 1}},
        ])
        with patch.object(self.exchange, "_api_request", new=api_request_mock), \
                patch("hummingbot.connector.exchange.kraken.kraken_exchange.asyncio.sleep", new=AsyncMock()):
            result = await self.exchange._api_request_with_retry(
                method=RESTMethod.POST, path_url=CONSTANTS.BALANCE_PATH_URL, is_auth_required=True)
        self.assertEqual({"ok": 1}, result)
        self.assertEqual(2, api_request_mock.call_count)

    async def test_place_order_recovers_from_cloudflare_via_open_orders(self):
        # AUTH-2: on a Cloudflare 5xx during AddOrder, recover the (possibly live) order via OpenOrders and
        # return an AddOrder-shaped result so the caller reads result["txid"][0] instead of KeyError.
        client_order_id = "12345"
        recovered = {
            "open": {
                "OABC-123-XYZ": {
                    "userref": int(client_order_id),
                    "status": "open",
                    "descr": {"order": "buy 1 ETH/USDT @ limit 100"},
                },
            }
        }
        with patch.object(self.exchange, "_api_request",
                          new=AsyncMock(side_effect=IOError("Error, HTTP status is 520."))), \
                patch.object(self.exchange, "get_open_orders_with_userref",
                             new=AsyncMock(return_value=recovered)), \
                patch("hummingbot.connector.exchange.kraken.kraken_exchange.asyncio.sleep", new=AsyncMock()):
            result = await self.exchange._api_request_with_retry(
                method=RESTMethod.POST, path_url=CONSTANTS.ADD_ORDER_PATH_URL,
                data={"userref": client_order_id}, is_auth_required=True)
        self.assertEqual(["OABC-123-XYZ"], result["txid"])
        self.assertEqual("OABC-123-XYZ", result["txid"][0])

    @aioresponses()
    def test_update_balances_folds_flex_without_spot(self, mocked_api):
        # BAL-1/BAL-2: a Flex/earn (.F) balance held without a co-held spot balance must fold in cleanly
        # (no "dictionary changed size during iteration" crash) and leave no phantom .F entry behind.
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.ASSET_PAIRS_PATH_URL}"
        mocked_api.get(url, body=json.dumps(self.get_asset_pairs_mock()))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.BALANCE_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mocked_api.post(regex_url, body=json.dumps({"error": [], "result": {"XBT.F": "5", "USDT": "100"}}))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.OPEN_ORDERS_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mocked_api.post(regex_url, body=json.dumps({"error": [], "result": {"open": {}}}))

        self.async_run_with_timeout(self.exchange._update_balances())

        self.assertEqual(Decimal("5"), self.exchange.available_balances["BTC"])
        self.assertEqual(Decimal("5"), self.exchange.get_balance("BTC"))
        self.assertNotIn("XBT.F", self.exchange.available_balances)
        self.assertEqual(Decimal("100"), self.exchange.available_balances["USDT"])

    @aioresponses()
    def test_update_balances_excludes_non_spot_subbalances(self, mocked_api):
        # BAL-LIVE-1: staked (.S), bonded (.B) and on-hold (.HOLD) sub-balances are not spot-tradable
        # and must not surface as phantom available assets (observed live: SOL03.S, XBT.B, USD.HOLD).
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.ASSET_PAIRS_PATH_URL}"
        mocked_api.get(url, body=json.dumps(self.get_asset_pairs_mock()))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.BALANCE_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mocked_api.post(regex_url, body=json.dumps({"error": [], "result": {
            "SOL": "1.0", "SOL03.S": "5.0", "XBT.B": "0.5", "USD.HOLD": "10.0",
            "ZUSD": "100.0", "XBT.F": "2.0",
        }}))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.OPEN_ORDERS_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mocked_api.post(regex_url, body=json.dumps({"error": [], "result": {"open": {}}}))

        self.async_run_with_timeout(self.exchange._update_balances())
        avail = self.exchange.available_balances

        self.assertNotIn("SOL03.S", avail)
        self.assertNotIn("XBT.B", avail)
        self.assertNotIn("USD.HOLD", avail)
        self.assertEqual([], [a for a in avail if "." in a])  # no phantom dotted assets at all
        self.assertEqual(Decimal("1.0"), avail["SOL"])
        self.assertEqual(Decimal("100.0"), avail["USD"])
        self.assertEqual(Decimal("2.0"), avail["BTC"])  # liquid .F folds into base; .B does NOT

    @aioresponses()
    def test_update_balances_skips_unresolvable_open_order_pair(self, mocked_api):
        # UTILSCFG-3: a single open order whose pair cannot be resolved must not crash balance polling.
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.ASSET_PAIRS_PATH_URL}"
        mocked_api.get(url, body=json.dumps(self.get_asset_pairs_mock()))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.BALANCE_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mocked_api.post(regex_url, body=json.dumps({"error": [], "result": {"USDT": "100"}}))

        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.OPEN_ORDERS_PATH_URL}"
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        open_orders = {"error": [], "result": {"open": {
            "BADORDER": {"status": "open", "descr": {"ordertype": "limit", "pair": "ZZZUNKNOWNPAIR",
                                                     "type": "buy", "price": "1"}},
        }}}
        mocked_api.post(regex_url, body=json.dumps(open_orders))

        # Must not raise despite the unresolvable open-order pair.
        self.async_run_with_timeout(self.exchange._update_balances())
        self.assertEqual(Decimal("100"), self.exchange.available_balances["USDT"])

    @aioresponses()
    def test_get_asset_pairs_skips_entries_missing_base_or_quote(self, mocked_api):
        # BAL-7: AssetPairs entries lacking base/quote must be skipped, not raise KeyError.
        url = f"{CONSTANTS.BASE_URL}{CONSTANTS.ASSET_PAIRS_PATH_URL}"
        resp = {
            "error": [],
            "result": {
                "XBTUSDT": {"altname": "XBTUSDT", "wsname": "XBT/USDT", "base": "XXBT", "quote": "USDT"},
                "BADPAIR": {"altname": "BADPAIR", "wsname": "BAD/PAIR", "quote": "USDT"},  # missing base
            },
        }
        mocked_api.get(url, body=json.dumps(resp))
        result = self.async_run_with_timeout(self.exchange.get_asset_pairs())
        self.assertIn("XXBT-USDT", result)
        self.assertEqual(1, len(result))

    async def test_get_last_traded_prices_none_or_empty_returns_empty(self):
        # USERSTREAM-3: None (and []) must return {} instead of raising TypeError on len(None).
        self.assertEqual({}, await self.exchange.get_last_traded_prices(None))
        self.assertEqual({}, await self.exchange.get_last_traded_prices([]))

    async def test_format_trading_rules_uses_costmin_and_tick_size(self):
        # BAL-5/BAL-6: min_notional from costmin and min_price_increment from tick_size (with fallback).
        altname = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        self.exchange._set_trading_pair_symbol_map(bidict({altname: self.trading_pair}))
        exchange_info = {
            altname: {
                "altname": altname,
                "wsname": f"{self.base_asset}/{self.quote_asset}",
                "base": self.base_asset,
                "quote": self.quote_asset,
                "pair_decimals": 1,
                "lot_decimals": 8,
                "ordermin": "0.5",
                "costmin": "5",
                "tick_size": "0.01",
                "status": "online",
            }
        }
        rules = await self.exchange._format_trading_rules(exchange_info)
        self.assertEqual(1, len(rules))
        rule = rules[0]
        self.assertEqual(Decimal("0.5"), rule.min_order_size)
        self.assertEqual(Decimal("0.01"), rule.min_price_increment)
        self.assertEqual(Decimal("5"), rule.min_notional_size)
        self.assertEqual(Decimal("1e-8"), rule.min_base_amount_increment)

    async def test_format_trading_rules_falls_back_to_pair_decimals(self):
        # BAL-6 fallback: without tick_size/costmin, behavior matches the legacy pair_decimals path.
        altname = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        self.exchange._set_trading_pair_symbol_map(bidict({altname: self.trading_pair}))
        exchange_info = {
            altname: {
                "altname": altname,
                "wsname": f"{self.base_asset}/{self.quote_asset}",
                "base": self.base_asset,
                "quote": self.quote_asset,
                "pair_decimals": 2,
                "lot_decimals": 8,
                "ordermin": "0.5",
            }
        }
        rules = await self.exchange._format_trading_rules(exchange_info)
        self.assertEqual(Decimal("1e-2"), rules[0].min_price_increment)
        self.assertEqual(Decimal("0"), rules[0].min_notional_size)
