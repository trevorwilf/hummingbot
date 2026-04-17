"""
Phase 4 MEXC adapter tests.
Pure logic - no network. Validates param construction only.
"""
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.data_feed.candles_feed.mexc_spot_candles import constants as CONSTANTS  # noqa: E402
from hummingbot.data_feed.candles_feed.mexc_spot_candles.mexc_spot_candles import MexcSpotCandles  # noqa: E402


@pytest.fixture
def adapter():
    return MexcSpotCandles(trading_pair="BTC-USDT", interval="5m", max_records=600)


def test_constant_matches_live_api_ceiling():
    assert CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST == 500


def test_params_include_start_end_and_clamped_limit(adapter):
    params = adapter._get_rest_candles_params(
        start_time=1_700_000_000, end_time=1_700_120_000, limit=400
    )
    assert params["symbol"] == "BTCUSDT"
    assert params["interval"] == "5m"
    assert params["limit"] == 400
    assert params["startTime"] == 1_700_000_000 * 1000
    assert params["endTime"] == 1_700_120_000 * 1000


def test_limit_clamped_to_500_when_caller_requests_more(adapter):
    params = adapter._get_rest_candles_params(
        start_time=1_700_000_000, end_time=1_700_120_000, limit=1000
    )
    assert params["limit"] == 500


def test_exact_500_bars_is_accepted_not_rejected(adapter):
    params = adapter._get_rest_candles_params(
        start_time=1_700_000_000, end_time=1_700_000_000 + 500 * 300, limit=500
    )
    assert params["limit"] == 500


def test_deep_history_request_no_longer_rejected(adapter):
    params = adapter._get_rest_candles_params(
        start_time=1_700_000_000 - 6000 * 300,
        end_time=1_700_000_000,
        limit=500,
    )
    assert "startTime" in params and "endTime" in params


def test_start_time_only_is_accepted(adapter):
    params = adapter._get_rest_candles_params(start_time=1_700_000_000, limit=100)
    assert params["startTime"] == 1_700_000_000 * 1000
    assert "endTime" not in params


def test_end_time_only_is_accepted(adapter):
    params = adapter._get_rest_candles_params(end_time=1_700_000_000, limit=100)
    assert params["endTime"] == 1_700_000_000 * 1000
    assert "startTime" not in params


def test_none_limit_falls_back_to_ceiling(adapter):
    params = adapter._get_rest_candles_params(start_time=1_700_000_000, end_time=1_700_001_000, limit=None)
    assert params["limit"] == CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST
