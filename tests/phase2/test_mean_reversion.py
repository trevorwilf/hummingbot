"""
Phase 2 correctness tests for mean_reversion_bb_rsi_v1.
No network, no bot runtime. Mocks market_data_provider and executors_info only.
"""
import pathlib
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.core.data_type.common import TradeType  # noqa: E402

from controllers.directional_trading.mean_reversion_bb_rsi_v1 import (  # noqa: E402
    MeanReversionBBRSIV1,
    MeanReversionBBRSIV1Config,
)


def _make_config(**overrides):
    base = dict(
        id="test_mr",
        controller_name="mean_reversion_bb_rsi_v1",
        controller_type="directional_trading",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("300"),
        max_executors_per_side=1,
        cooldown_time=3600,
        leverage=1,
        position_mode="ONEWAY",
        stop_loss=Decimal("0.04"),
        take_profit=Decimal("0.03"),
        time_limit=172800,
        trailing_stop="",
        candles_connector="",
        candles_trading_pair="",
        interval="5m",
        bb_length=80,
        bb_std=2.0,
        bbp_entry_threshold=0.20,
        rsi_length=14,
        rsi_entry_threshold=40.0,
        use_trend_filter=True,
        trend_ema_length=200,
        min_trend_slope=0.0,
        atr_length=14,
        max_atr_pct_for_entry=0.10,
        volume_filter_window=288,
        min_volume_quantile=0.30,
        max_spread_pct=0.006,
        max_trades_per_day=6,
    )
    base.update(overrides)
    return MeanReversionBBRSIV1Config(**base)


def _stub_controller(config, now=1_000_000.0, executors_info=None, bid=1.0, ask=1.001):
    """Create an MR controller with its base __init__ skipped — we only exercise
    the small set of methods the tests need, avoiding the full controller runtime
    which expects MQTT/executor orchestration."""
    mr = MeanReversionBBRSIV1.__new__(MeanReversionBBRSIV1)
    mr.config = config
    mr.max_records = config.required_records
    mr.executors_info = executors_info or []
    mr.processed_data = {}

    mdp = MagicMock()
    mdp.time.return_value = now

    def _price_by_type(_conn, _pair, price_type):
        from hummingbot.core.data_type.common import PriceType
        if price_type == PriceType.BestBid:
            return Decimal(str(bid))
        if price_type == PriceType.BestAsk:
            return Decimal(str(ask))
        return Decimal(str((bid + ask) / 2))
    mdp.get_price_by_type.side_effect = _price_by_type
    mr.market_data_provider = mdp
    return mr


def _make_executor(ts, side, is_active, close_ts=None):
    """Minimal ExecutorInfo-like stub for the fields our fix reads."""
    return SimpleNamespace(
        timestamp=ts,
        side=side,
        is_active=is_active,
        close_timestamp=close_ts,
    )


# ----------------------- Task 1: unified depth -----------------------

def test_required_records_used_in_both_places():
    cfg = _make_config()
    expected = max(cfg.bb_length, cfg.trend_ema_length, cfg.rsi_length, cfg.atr_length) + 500
    assert cfg.required_records == expected
    assert cfg.candles_config[0].max_records == expected
    mr = _stub_controller(cfg)
    assert mr.max_records == expected


# ----------------------- Task 2: cooldown -----------------------

def test_cooldown_blocks_within_window_after_close():
    cfg = _make_config(cooldown_time=3600)
    now = 1_000_000.0
    closed_recently = _make_executor(ts=now - 7200, side=TradeType.BUY, is_active=False, close_ts=now - 600)
    mr = _stub_controller(cfg, now=now, executors_info=[closed_recently])
    assert mr.can_create_executor(signal=1) is False


def test_cooldown_allows_after_window_elapsed():
    cfg = _make_config(cooldown_time=3600)
    now = 1_000_000.0
    closed_long_ago = _make_executor(ts=now - 10_000, side=TradeType.BUY, is_active=False, close_ts=now - 7200)
    mr = _stub_controller(cfg, now=now, executors_info=[closed_long_ago])
    assert mr.can_create_executor(signal=1) is True


def test_cooldown_ignores_opposite_side_history():
    cfg = _make_config(cooldown_time=3600)
    now = 1_000_000.0
    opposite = _make_executor(ts=now - 7200, side=TradeType.SELL, is_active=False, close_ts=now - 600)
    mr = _stub_controller(cfg, now=now, executors_info=[opposite])
    assert mr.can_create_executor(signal=1) is True


def test_active_executor_blocks_via_super_not_our_cooldown():
    cfg = _make_config(cooldown_time=3600, max_executors_per_side=1)
    now = 1_000_000.0
    active = _make_executor(ts=now - 60, side=TradeType.BUY, is_active=True, close_ts=None)
    mr = _stub_controller(cfg, now=now, executors_info=[active])
    assert mr.can_create_executor(signal=1) is False


def test_no_history_allows_first_entry():
    cfg = _make_config(cooldown_time=3600)
    mr = _stub_controller(cfg, executors_info=[])
    assert mr.can_create_executor(signal=1) is True


# ----------------------- Task 3: last-candle drop -----------------------

def _mk_df(timestamps, close=100.0, high=101.0, low=99.0, open_=100.0, vol=10.0):
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [open_] * len(timestamps),
        "high": [high] * len(timestamps),
        "low": [low] * len(timestamps),
        "close": [close] * len(timestamps),
        "volume": [vol] * len(timestamps),
    })


@pytest.mark.asyncio
async def test_last_candle_dropped_when_still_forming():
    cfg = _make_config(interval="5m")
    now = 1_000_000.0
    # Latest bar's start is 60s ago -> bar window is [now-60, now-60+300); still forming.
    ts = [now - 900, now - 600, now - 300, now - 60]
    df = _mk_df(ts)

    mr = _stub_controller(cfg, now=now)
    mr.market_data_provider.get_candles_df = MagicMock(return_value=df)
    await mr.update_processed_data()
    feats = mr.processed_data["features"]
    assert feats["timestamp"].max() == now - 300


@pytest.mark.asyncio
async def test_last_candle_kept_when_already_closed_on_sparse_feed():
    cfg = _make_config(interval="5m")
    now = 1_000_000.0
    # Latest bar's start is 7 minutes ago -> bar closed 2 min ago. Keep it.
    ts = [now - 1800, now - 1500, now - 1200, now - 420]
    df = _mk_df(ts)
    mr = _stub_controller(cfg, now=now)
    mr.market_data_provider.get_candles_df = MagicMock(return_value=df)
    await mr.update_processed_data()
    feats = mr.processed_data["features"]
    assert feats["timestamp"].max() == now - 420


# ----------------------- Task 4: spread gate logging -----------------------

def test_spread_gate_logs_when_bid_ask_missing(caplog):
    import logging
    cfg = _make_config(max_spread_pct=0.006)
    mr = _stub_controller(cfg)
    mr.market_data_provider.get_price_by_type.side_effect = RuntimeError("book empty")
    with caplog.at_level(logging.WARNING):
        assert mr._safe_spread_ok() is False
    assert any("spread gate" in r.message.lower() for r in caplog.records)


def test_spread_gate_passes_when_spread_narrow_enough():
    cfg = _make_config(max_spread_pct=0.006)  # 60 bps
    mr = _stub_controller(cfg, bid=100.0, ask=100.3)  # 30 bps
    assert mr._safe_spread_ok() is True


def test_spread_gate_fails_when_spread_too_wide():
    cfg = _make_config(max_spread_pct=0.001)  # 10 bps
    mr = _stub_controller(cfg, bid=100.0, ask=100.5)  # 50 bps
    assert mr._safe_spread_ok() is False
