import os
from decimal import Decimal
from typing import Dict, List

import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict, PriceType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class KrakenWsMarketDataConfig(StrategyV2ConfigBase):
    """
    Configuration for the Kraken WebSocket market-data example.

    Demonstrates consuming Kraken REAL-TIME data over WebSockets in a v2 strategy:
      * order book best bid / ask / mid / spread (Kraken public ``book`` WS channel, via the connector's
        WS-fed order book tracker), and
      * OHLC candles (Kraken public ``ohlc`` WS channel, via the MarketDataProvider / CandlesFactory).

    This script is READ-ONLY: it never places, edits or cancels orders. It only displays live data.
    """
    script_file_name: str = os.path.basename(__file__)
    controllers_config: List[str] = Field(default=[], exclude=True)

    # Kraken spot connector + the pairs whose live order book you want to read over WebSockets.
    connector_name: str = Field(
        default="kraken",
        json_schema_extra={"prompt": "Kraken spot connector name", "prompt_on_new": True},
    )
    trading_pairs: List[str] = Field(
        default=["BTC-USDT"],
        json_schema_extra={
            "prompt": "Trading pairs for the live order book (comma separated, e.g. BTC-USDT,ETH-USDT)",
            "prompt_on_new": True,
        },
    )

    # WS candle feeds (interval and depth per pair). Kraken supports up to 720 records per feed.
    candles_config: List[CandlesConfig] = Field(
        default_factory=lambda: [
            CandlesConfig(connector="kraken", trading_pair="BTC-USDT", interval="1m", max_records=200),
        ],
        json_schema_extra={
            "prompt": "Candles configs (connector.pair.interval.max_records, ':'-separated)",
            "prompt_on_new": True,
        },
    )

    @staticmethod
    def _parse_pairs(v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("trading_pairs", mode="before")
    @classmethod
    def _validate_trading_pairs(cls, v):
        # Accept a comma-separated string (as entered at the config prompt) or a list.
        return cls._parse_pairs(v)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        # Register the Kraken connector + pairs so the framework spins up the WS-fed order book tracker.
        markets.add_or_update(self.connector_name, *self._parse_pairs(self.trading_pairs))
        return markets


class KrakenWsMarketData(StrategyV2Base):
    """
    Live Kraken market-data viewer (read-only) built entirely on WebSocket feeds.

    Order book pricing comes from ``market_data_provider.get_price_by_type`` (the connector's order book
    is streamed over Kraken's public ``book`` WS channel). Candles come from
    ``market_data_provider.get_candles_df`` (Kraken's public ``ohlc`` WS channel, backfilled once over REST).
    No trading markets are required for candles; the order-book section needs the Kraken connector active.

    Available intervals: |1m|5m|15m|30m|1h|4h|1d|1w|
    """

    def __init__(self, connectors: Dict[str, ConnectorBase], config: KrakenWsMarketDataConfig):
        super().__init__(connectors, config)
        self.config = config
        for candles_config in self.config.candles_config:
            self.market_data_provider.initialize_candles_feed(candles_config)
        self.logger().info(
            f"Initialized {len(self.config.candles_config)} Kraken WS candle feed(s) and "
            f"{len(config._parse_pairs(config.trading_pairs))} order-book pair(s).")

    @property
    def all_candles_ready(self) -> bool:
        for candle in self.config.candles_config:
            feed = self.market_data_provider.get_candles_feed(candle)
            if not feed.ready or feed.candles_df.empty:
                return False
        return True

    def _order_book_lines(self) -> List[str]:
        lines = ["", "LIVE ORDER BOOK (Kraken public 'book' WS channel)", "-" * 80]
        for pair in self.config._parse_pairs(self.config.trading_pairs):
            try:
                bid = self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.BestBid)
                ask = self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.BestAsk)
                mid = self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.MidPrice)
                if bid is None or ask is None or bid.is_nan() or ask.is_nan():
                    lines.append(f"  {pair:12} waiting for order book...")
                    continue
                spread_bps = (ask - bid) / mid * Decimal("10000") if mid and not mid.is_nan() else Decimal("0")
                lines.append(f"  {pair:12} bid={bid:.6g}  ask={ask:.6g}  mid={mid:.6g}  spread={spread_bps:.2f} bps")
            except Exception as e:
                lines.append(f"  {pair:12} order book unavailable: {type(e).__name__}")
        return lines

    def _candles_lines(self) -> List[str]:
        lines = ["", "LIVE CANDLES (Kraken public 'ohlc' WS channel)", "-" * 80]
        if not self.all_candles_ready:
            for c in self.config.candles_config:
                feed = self.market_data_provider.get_candles_feed(c)
                ready = feed.ready and not feed.candles_df.empty
                lines.append(f"  [{'ready' if ready else 'loading'}] "
                             f"{c.connector}.{c.trading_pair}.{c.interval}")
            return lines
        for c in self.config.candles_config:
            df = self.market_data_provider.get_candles_df(
                connector_name=c.connector, trading_pair=c.trading_pair,
                interval=c.interval, max_records=50)
            if df is None or df.empty:
                lines.append(f"  {c.connector}.{c.trading_pair}.{c.interval}: no data yet")
                continue
            df = df.copy()
            if len(df) >= 20:
                df.ta.rsi(length=14, append=True)
                df.ta.ema(length=14, append=True)
            last = df.iloc[-1]
            extra = ""
            if "RSI_14" in df.columns and pd.notna(last.get("RSI_14")):
                extra += f" | RSI14={last['RSI_14']:.1f}"
            if "EMA_14" in df.columns and pd.notna(last.get("EMA_14")):
                extra += f" | EMA14={last['EMA_14']:.4f}"
            lines.append(f"  {c.connector}.{c.trading_pair}.{c.interval}: "
                         f"close={last['close']:.4f} vol={last['volume']:.4f} "
                         f"n_trades={int(last['n_trades'])}{extra}")
        return lines

    def format_status(self) -> str:
        lines = ["", "=" * 80, "          KRAKEN WEBSOCKET MARKET DATA (read-only)", "=" * 80]
        lines += self._order_book_lines()
        lines += self._candles_lines()
        lines += ["", "=" * 80]
        return "\n".join(lines)
