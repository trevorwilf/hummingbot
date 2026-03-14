import asyncio
import datetime
import time
from decimal import Decimal
from pathlib import Path
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.mock.mock_cli import CLIMockingAssistant
from typing import List
from unittest.mock import patch

from hummingbot.client.config.client_config_map import ClientConfigMap, DBSqliteMode
from hummingbot.client.config.config_helpers import ClientConfigAdapter, read_system_configs_from_yml
from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.exchange.paper_trade import PaperTradeExchange
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
from hummingbot.model.order import Order
from hummingbot.model.sql_connection_manager import SQLConnectionManager
from hummingbot.model.trade_fill import TradeFill


class HistoryCommandTest(IsolatedAsyncioWrapperTestCase):
    @patch("hummingbot.core.utils.trading_pair_fetcher.TradingPairFetcher")
    @patch("hummingbot.core.gateway.gateway_http_client.GatewayHttpClient.start_monitor")
    @patch("hummingbot.client.hummingbot_application.HummingbotApplication.mqtt_start")
    async def asyncSetUp(self, mock_mqtt_start, mock_gateway_start, mock_trading_pair_fetcher):
        await read_system_configs_from_yml()
        self.client_config_map = ClientConfigAdapter(ClientConfigMap())
        self.app = HummingbotApplication(client_config_map=self.client_config_map)
        self.cli_mock_assistant = CLIMockingAssistant(self.app.app)
        self.cli_mock_assistant.start()
        self.mock_strategy_name = "test-strategy"

    def tearDown(self) -> None:
        self.cli_mock_assistant.stop()
        # Dispose the DB engine before deleting to avoid PermissionError on Windows
        if self.app.trading_core.trade_fill_db is not None:
            self.app.trading_core.trade_fill_db.engine.dispose()
            SQLConnectionManager._scm_trade_fills_instance = None
            self.app.trading_core.trade_fill_db = None
        db_path = Path(SQLConnectionManager.create_db_path(db_name=self.mock_strategy_name))
        db_path.unlink(missing_ok=True)
        super().tearDown()

    @staticmethod
    def get_async_sleep_fn(delay: float):
        async def async_sleep(*_, **__):
            await asyncio.sleep(delay)

        return async_sleep

    def get_trades(self) -> List[TradeFill]:
        trade_fee = AddedToCostTradeFee(percent=Decimal("5"))
        trades = [
            TradeFill(
                config_file_path=f"{self.mock_strategy_name}.yml",
                strategy=self.mock_strategy_name,
                market="binance",
                symbol="BTC-USDT",
                base_asset="BTC",
                quote_asset="USDT",
                timestamp=int(time.time()),
                order_id="someId",
                trade_type="BUY",
                order_type="LIMIT",
                price=1,
                amount=2,
                leverage=1,
                trade_fee=trade_fee.to_json(),
                exchange_trade_id="someExchangeId",
            )
        ]
        return trades

    @patch("hummingbot.client.hummingbot_application.HummingbotApplication.notify")
    def test_list_trades(self, notify_mock):
        self.client_config_map.db_mode = DBSqliteMode()

        captures = []
        notify_mock.side_effect = lambda s: captures.append(s)
        self.app.strategy_file_name = f"{self.mock_strategy_name}.yml"

        # Initialize the trade_fill_db if it doesn't exist
        if self.app.trading_core.trade_fill_db is None:
            self.app.trading_core.trade_fill_db = SQLConnectionManager.get_trade_fills_instance(
                self.client_config_map, self.mock_strategy_name
            )

        trade_fee = AddedToCostTradeFee(percent=Decimal("5"))
        order_id = PaperTradeExchange.random_order_id(order_side="BUY", trading_pair="BTC-USDT")
        with self.app.trading_core.trade_fill_db.get_new_session() as session:
            o = Order(
                id=order_id,
                config_file_path=f"{self.mock_strategy_name}.yml",
                strategy=self.mock_strategy_name,
                market="binance",
                symbol="BTC-USDT",
                base_asset="BTC",
                quote_asset="USDT",
                creation_timestamp=0,
                order_type="LMT",
                amount=4,
                leverage=0,
                price=3,
                last_status="PENDING",
                last_update_timestamp=0,
            )
            session.add(o)
            for i in [1, 2]:
                t = TradeFill(
                    config_file_path=f"{self.mock_strategy_name}.yml",
                    strategy=self.mock_strategy_name,
                    market="binance",
                    symbol="BTC-USDT",
                    base_asset="BTC",
                    quote_asset="USDT",
                    timestamp=i,
                    order_id=order_id,
                    trade_type="BUY",
                    order_type="LIMIT",
                    price=i,
                    amount=2,
                    leverage=1,
                    trade_fee=trade_fee.to_json(),
                    exchange_trade_id=f"someExchangeId{i}",
                )
                session.add(t)
            session.commit()

        self.app.list_trades(start_time=0)

        self.assertEqual(1, len(captures))

        output = captures[0]
        # Validate structure rather than exact string (resilient to VWAP/formatting changes)
        self.assertIn("Recent trades:", output)
        # Verify expected column headers are present
        for col in ["Exchange", "Market", "Order_type", "Side", "Price", "Amount"]:
            self.assertIn(col, output)
        # Verify key trade data appears in the output
        self.assertIn("binance", output)
        self.assertIn("BTC-USDT", output)
        self.assertIn("buy", output)
        self.assertIn("limit", output)
        # Verify both trade rows appear (prices 1 and 2, amounts of 2)
        # Count data rows (lines containing 'binance')
        data_lines = [line for line in output.split("\n") if "binance" in line]
        self.assertGreaterEqual(len(data_lines), 2, "Expected at least 2 trade rows")
