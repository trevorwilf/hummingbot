"""
Phase 4 NonKYC validator tests. Pure logic, no network.
"""
import pathlib
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.data_feed.candles_feed.nonkyc_spot_candles.nonkyc_spot_candles import NonKYCSpotCandles  # noqa: E402


@pytest.fixture
def adapter():
    a = NonKYCSpotCandles(trading_pair="XMR-USDT", interval="5m", max_records=100)
    a._reset_candles = MagicMock()
    return a


def _mk(ts_list):
    arr = np.zeros((len(ts_list), 10), dtype=float)
    arr[:, 0] = ts_list
    return arr


def test_empty_and_single_row_skip_without_reset(adapter):
    adapter.check_candles_sorted_and_equidistant(_mk([]))
    adapter.check_candles_sorted_and_equidistant(_mk([1_700_000_000]))
    assert adapter._reset_candles.call_count == 0


def test_strictly_ascending_aligned_timestamps_ok(adapter):
    ts = [1_700_000_000 + 300 * i for i in range(10)]
    adapter.check_candles_sorted_and_equidistant(_mk(ts))
    assert adapter._reset_candles.call_count == 0


def test_gap_of_multiple_intervals_is_accepted(adapter):
    ts = [1_700_000_000, 1_700_000_300, 1_700_001_500, 1_700_001_800]  # 5m, 20m, 5m
    adapter.check_candles_sorted_and_equidistant(_mk(ts))
    assert adapter._reset_candles.call_count == 0


def test_duplicate_timestamp_is_rejected(adapter):
    ts = [1_700_000_000, 1_700_000_300, 1_700_000_300, 1_700_000_600]
    adapter.check_candles_sorted_and_equidistant(_mk(ts))
    assert adapter._reset_candles.call_count == 1


def test_out_of_order_timestamp_is_rejected(adapter):
    ts = [1_700_000_000, 1_700_000_600, 1_700_000_300]
    adapter.check_candles_sorted_and_equidistant(_mk(ts))
    assert adapter._reset_candles.call_count == 1


def test_misaligned_timestamp_is_rejected(adapter):
    ts = [1_700_000_000, 1_700_000_180, 1_700_000_480]
    adapter.check_candles_sorted_and_equidistant(_mk(ts))
    assert adapter._reset_candles.call_count == 1
