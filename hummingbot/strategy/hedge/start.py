from decimal import Decimal

from hummingbot.strategy.hedge.hedge import HedgeStrategy
from hummingbot.strategy.hedge.hedge_config_map_pydantic import MAX_CONNECTOR, HedgeConfigMap
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple


async def start(self):
    c_map: HedgeConfigMap = self.strategy_config_map
    hedge_connector = c_map.hedge_connector.lower()
    hedge_markets = c_map.hedge_markets
    hedge_offsets = c_map.hedge_offsets
    # ARB-10: keep offsets aligned to each connector SLOT (the hedge connector may also appear
    # as a monitored connector - a name-keyed dict silently overwrote the hedge offsets)
    initialize_markets = [(hedge_connector, hedge_markets)]
    offsets_by_slot = [hedge_offsets]
    for i in range(MAX_CONNECTOR):
        connector_config = getattr(c_map, f"connector_{i}")
        connector = connector_config.connector
        if not connector:
            continue
        connector = connector.lower()
        markets = connector_config.markets
        initialize_markets.append((connector, markets))
        offsets_by_slot.append(connector_config.offsets)
    await self.initialize_markets(initialize_markets)
    self.market_trading_pair_tuples = []
    offsets_market_dict = {}
    for (connector, markets), offsets in zip(initialize_markets, offsets_by_slot):
        # ARB-10: pad missing offsets with zeros - zip() used to silently DROP the excess
        # markets from monitoring entirely, leaving their exposure unhedged
        padded_offsets = list(offsets) + [Decimal("0")] * (len(markets) - len(offsets))
        for market, offset in zip(markets, padded_offsets):
            base, quote = market.split("-")
            market_info = MarketTradingPairTuple(self.markets[connector], market, base, quote)
            self.market_trading_pair_tuples.append(market_info)
            offsets_market_dict[market_info] = offset
    index = len(hedge_markets)
    hedge_market_pairs = self.market_trading_pair_tuples[0:index]
    market_pairs = self.market_trading_pair_tuples[index:]
    self.strategy = HedgeStrategy(
        config_map=c_map, hedge_market_pairs=hedge_market_pairs, market_pairs=market_pairs, offsets=offsets_market_dict
    )
