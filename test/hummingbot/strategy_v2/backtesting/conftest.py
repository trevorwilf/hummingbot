"""
Shared fixtures for backtesting tests.

All fixtures produce deterministic, hardcoded data — no network, no randomness.
"""
import pytest
import pandas as pd
import numpy as np
from decimal import Decimal
from unittest.mock import MagicMock
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.core.data_type.common import TradeType


@pytest.fixture
def sample_candles_df():
    """A small deterministic candle DataFrame for backtesting tests."""
    timestamps = list(range(1000000, 1000000 + 20 * 60, 60))  # 20 one-minute candles
    closes = [100.0 + i * 0.5 for i in range(20)]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [c - 0.1 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0 + i * 10 for i in range(20)],
    })


@pytest.fixture
def sample_features_df():
    """Deterministic features DataFrame for merge_asof tests."""
    # Features at slightly offset timestamps (simulating slower feature cadence)
    timestamps = list(range(1000000, 1000000 + 20 * 60, 180))  # every 3 minutes
    n = len(timestamps)
    return pd.DataFrame({
        "timestamp": timestamps,
        "reference_price": [100.0 + i * 1.5 for i in range(n)],
        "spread_multiplier": [0.01 + i * 0.001 for i in range(n)],
        "signal": [(-1) ** i for i in range(n)],
    })


@pytest.fixture
def mock_executor_info_list():
    """Build a list of mock ExecutorInfo-like objects for summarize_results testing."""
    infos = []
    # 3 winning trades, 2 losing trades
    pnls = [Decimal("10.0"), Decimal("5.0"), Decimal("15.0"), Decimal("-8.0"), Decimal("-3.0")]
    sides = [TradeType.BUY, TradeType.SELL, TradeType.BUY, TradeType.BUY, TradeType.SELL]
    for i, (pnl, side) in enumerate(zip(pnls, sides)):
        info = MagicMock()
        info.net_pnl_quote = pnl
        info.filled_amount_quote = Decimal("100.0")
        info.timestamp = 1000000 + i * 60
        info.close_type = CloseType.TAKE_PROFIT if pnl > 0 else CloseType.STOP_LOSS
        info.status = RunnableStatus.TERMINATED
        info.custom_info = {"side": side}
        info.to_dict.return_value = {
            "net_pnl_quote": float(pnl),
            "filled_amount_quote": float(Decimal("100.0")),
            "timestamp": 1000000 + i * 60,
            "close_type": CloseType.TAKE_PROFIT if pnl > 0 else CloseType.STOP_LOSS,
            "side": side,
        }
        infos.append(info)
    return infos
