"""
Phase 5 integration test: MarketDataProvider must reuse an existing candle
feed when a same-or-smaller max_records is subsequently requested.
"""
import pathlib
import sys
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig  # noqa: E402
from hummingbot.data_feed.market_data_provider import MarketDataProvider  # noqa: E402


class _FakeFeed:
    def __init__(self, trading_pair, interval, max_records):
        self.trading_pair = trading_pair
        self.interval = interval
        self.max_records = max_records
        self._started = False
        self._stopped = False

    def start(self):
        self._started = True

    def stop(self):
        self._stopped = True


def _fresh_provider():
    p = MarketDataProvider(connectors={})
    p.candles_feeds = {}
    return p


def test_identical_request_reuses_existing_feed():
    p = _fresh_provider()
    cfg = CandlesConfig(connector="nonkyc", trading_pair="XMR-USDT", interval="5m", max_records=700)
    with patch("hummingbot.data_feed.market_data_provider.CandlesFactory.get_candle",
               side_effect=lambda c: _FakeFeed(c.trading_pair, c.interval, c.max_records)):
        feed1 = p.get_candles_feed(cfg)
        feed2 = p.get_candles_feed(cfg)
    assert feed1 is feed2
    assert feed1._stopped is False


def test_smaller_subsequent_request_reuses_existing_feed():
    p = _fresh_provider()
    cfg_big = CandlesConfig(connector="nonkyc", trading_pair="XMR-USDT", interval="5m", max_records=700)
    cfg_small = CandlesConfig(connector="nonkyc", trading_pair="XMR-USDT", interval="5m", max_records=300)
    with patch("hummingbot.data_feed.market_data_provider.CandlesFactory.get_candle",
               side_effect=lambda c: _FakeFeed(c.trading_pair, c.interval, c.max_records)):
        feed1 = p.get_candles_feed(cfg_big)
        feed2 = p.get_candles_feed(cfg_small)
    assert feed1 is feed2


def test_larger_subsequent_request_stops_old_and_creates_new():
    p = _fresh_provider()
    cfg_small = CandlesConfig(connector="nonkyc", trading_pair="XMR-USDT", interval="5m", max_records=300)
    cfg_big = CandlesConfig(connector="nonkyc", trading_pair="XMR-USDT", interval="5m", max_records=700)
    with patch("hummingbot.data_feed.market_data_provider.CandlesFactory.get_candle",
               side_effect=lambda c: _FakeFeed(c.trading_pair, c.interval, c.max_records)):
        feed1 = p.get_candles_feed(cfg_small)
        feed2 = p.get_candles_feed(cfg_big)
    assert feed1 is not feed2
    assert feed1._stopped is True
    assert feed2._started is True
