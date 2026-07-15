import unittest.mock
from decimal import Decimal

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange_base import ExchangeBase
from hummingbot.strategy.hedge.hedge_config_map_pydantic import EmptyMarketConfigMap, HedgeConfigMap, MarketConfigMap


class HedgeStartTest(unittest.TestCase):

    def setUp(self) -> None:
        super().setUp()
        self.strategy = None
        self.client_config_map = ClientConfigAdapter(ClientConfigMap())
        self.client_config_map.strategy_report_interval = 60.
        self.markets = {
            "binance": ExchangeBase(),
            "kucoin": ExchangeBase(),
            "ascend_ex": ExchangeBase()
        }
        self.notifications = []
        self.log_errors = []

        config_map_raw = HedgeConfigMap(
            value_mode=True,
            hedge_ratio=Decimal("1"),
            hedge_interval=60,
            min_trade_size=Decimal("0"),
            slippage=Decimal("0.02"),
            hedge_connector="binance",
            hedge_markets=["BTC-USDT"],
            hedge_offsets=[Decimal("0.01")],
            hedge_leverage=1,
            hedge_position_mode="ONEWAY",
            connector_0=MarketConfigMap(
                connector="kucoin",
                markets=["ETH-USDT"],
                offsets=[Decimal("0.02")],
            ),
            connector_1=MarketConfigMap(
                connector="ascend_ex",
                markets=["ETH-USDT", "BTC-USDT"],
                offsets=[Decimal("0.03")],
            ),
            connector_2=EmptyMarketConfigMap(),
            connector_3=EmptyMarketConfigMap(),
            connector_4=EmptyMarketConfigMap(),

        )
        self.strategy_config_map = ClientConfigAdapter(config_map_raw)

    def _initialize_markets(self, market_names):
        pass

    async def initialize_markets(self, market_names):
        pass

    def _notify(self, message):
        self.notifications.append(message)

    def logger(self):
        return self

    def error(self, message, exc_info):
        self.log_errors.append(message)

    def test_start_pads_missing_offsets_instead_of_dropping_markets(self):
        """Would have caught ARB-10: zip(markets, offsets) silently dropped the ascend_ex
        BTC-USDT market (2 markets, 1 offset) from monitoring entirely."""
        import asyncio

        import hummingbot.strategy.hedge.start as hedge_start

        asyncio.new_event_loop().run_until_complete(hedge_start.start(self))
        strategy = self.strategy
        self.assertEqual(1, len(strategy._hedge_market_pairs))
        # kucoin ETH-USDT + ascend_ex ETH-USDT + ascend_ex BTC-USDT (dropped before the fix)
        self.assertEqual(3, len(strategy._market_pairs))
        ascend_pairs = {mp.trading_pair: strategy._offsets[mp]
                        for mp in strategy._market_pairs if mp.market is self.markets["ascend_ex"]}
        self.assertEqual({"ETH-USDT": Decimal("0.03"), "BTC-USDT": Decimal("0")}, ascend_pairs)

    def test_start_keys_offsets_by_slot_when_hedge_connector_is_monitored(self):
        """Would have caught ARB-10: a name-keyed offsets dict let a monitored connector entry
        overwrite the hedge connector's offsets when both used the same connector."""
        import asyncio

        config_map_raw = HedgeConfigMap(
            value_mode=True,
            hedge_ratio=Decimal("1"),
            hedge_interval=60,
            min_trade_size=Decimal("0"),
            slippage=Decimal("0.02"),
            hedge_connector="binance",
            hedge_markets=["BTC-USDT"],
            hedge_offsets=[Decimal("0.01")],
            hedge_leverage=1,
            hedge_position_mode="ONEWAY",
            connector_0=MarketConfigMap(
                connector="binance",
                markets=["ETH-USDT"],
                offsets=[Decimal("0.05")],
            ),
            connector_1=EmptyMarketConfigMap(),
            connector_2=EmptyMarketConfigMap(),
            connector_3=EmptyMarketConfigMap(),
            connector_4=EmptyMarketConfigMap(),
        )
        self.strategy_config_map = ClientConfigAdapter(config_map_raw)

        import hummingbot.strategy.hedge.start as hedge_start

        asyncio.new_event_loop().run_until_complete(hedge_start.start(self))
        strategy = self.strategy
        hedge_pair = strategy._hedge_market_pairs[0]
        monitored_pair = strategy._market_pairs[0]
        # The hedge market keeps ITS offset (pre-fix it was overwritten with 0.05)
        self.assertEqual(Decimal("0.01"), strategy._offsets[hedge_pair])
        self.assertEqual(Decimal("0.05"), strategy._offsets[monitored_pair])
