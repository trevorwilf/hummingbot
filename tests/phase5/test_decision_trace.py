"""
Phase 5 tests: decision-trace INFO log lines are emitted on every tick
for MR and EMA, carrying the fields operators need for live debugging.
"""
import logging
import pathlib
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from controllers.directional_trading.mean_reversion_bb_rsi_v1 import (  # noqa: E402
    MeanReversionBBRSIV1,
    MeanReversionBBRSIV1Config,
)
from controllers.directional_trading.ema_regime_hold_v1 import (  # noqa: E402
    EMARegimeHoldV1,
    EMARegimeHoldV1Config,
)


# --- MR trace ---

def _mr_config(**overrides):
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
        bb_length=80, bb_std=2.0, bbp_entry_threshold=0.20,
        rsi_length=14, rsi_entry_threshold=40.0,
        use_trend_filter=True, trend_ema_length=200, min_trend_slope=0.0,
        atr_length=14, max_atr_pct_for_entry=0.10,
        volume_filter_window=288, min_volume_quantile=0.30,
        max_spread_pct=0.006, max_trades_per_day=6,
    )
    base.update(overrides)
    return MeanReversionBBRSIV1Config(**base)


def _mr_stub(config, now=1_000_000.0):
    mr = MeanReversionBBRSIV1.__new__(MeanReversionBBRSIV1)
    mr.config = config
    mr.max_records = config.required_records
    mr.executors_info = []
    mr.processed_data = {}
    mdp = MagicMock()
    mdp.time.return_value = now
    mr.market_data_provider = mdp
    return mr


@pytest.mark.asyncio
async def test_mr_emits_decision_trace_on_non_empty_features(caplog):
    cfg = _mr_config()
    now = 1_000_000.0
    ts = np.arange(now - 500 * 300, now, 300, dtype=float)
    close = np.linspace(100.0, 110.0, len(ts))
    df = pd.DataFrame({
        "timestamp": ts,
        "open": close, "high": close + 0.1, "low": close - 0.1,
        "close": close, "volume": [10.0] * len(ts),
    })
    mr = _mr_stub(cfg, now=now)
    mr.market_data_provider.get_candles_df = MagicMock(return_value=df)
    with caplog.at_level(logging.INFO):
        await mr.update_processed_data()
    trace_lines = [r.message for r in caplog.records if r.message.startswith("MR tick:")]
    assert len(trace_lines) == 1, f"Expected exactly 1 MR trace, got {len(trace_lines)}"
    line = trace_lines[0]
    for token in ["signal=", "bar_ts=", "bar_age_s=", "bbp=", "rsi=", "atr_pct=", "volume_ok=", "rows="]:
        assert token in line, f"Missing field {token!r} in MR trace line: {line}"


@pytest.mark.asyncio
async def test_mr_emits_decision_trace_on_empty_features(caplog):
    cfg = _mr_config()
    mr = _mr_stub(cfg)
    mr.market_data_provider.get_candles_df = MagicMock(return_value=pd.DataFrame())
    with caplog.at_level(logging.INFO):
        await mr.update_processed_data()
    trace_lines = [r.message for r in caplog.records if r.message.startswith("MR tick:")]
    assert len(trace_lines) == 1
    assert "features_empty=true" in trace_lines[0]
    assert "signal=0" in trace_lines[0]


# --- EMA trace ---

def _ema_config(**overrides):
    base = dict(
        id="test_ema",
        controller_name="ema_regime_hold_v1",
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
        signal_interval="5m",
        regime_interval="4h",
        regime_ema_fast=50, regime_ema_slow=200,
        regime_adx_length=14, regime_adx_threshold=20.0,
        volume_filter_window=288, min_volume_quantile=0.30,
        hold_mode="reentry",
    )
    base.update(overrides)
    return EMARegimeHoldV1Config(**base)


def _ema_stub(config, now=1_000_000.0):
    ctrl = EMARegimeHoldV1.__new__(EMARegimeHoldV1)
    ctrl.config = config
    ctrl.executors_info = []
    ctrl.processed_data = {}
    mdp = MagicMock()
    mdp.time.return_value = now
    ctrl.market_data_provider = mdp
    return ctrl


@pytest.mark.asyncio
async def test_ema_emits_decision_trace_on_empty_frames(caplog):
    cfg = _ema_config()
    ctrl = _ema_stub(cfg)
    ctrl.market_data_provider.get_candles_df = MagicMock(return_value=pd.DataFrame())
    with caplog.at_level(logging.INFO):
        await ctrl.update_processed_data()
    trace_lines = [r.message for r in caplog.records if r.message.startswith("EMA tick:")]
    assert len(trace_lines) == 1
    assert "features_empty=true" in trace_lines[0]
    assert "hold_mode=reentry" in trace_lines[0]


@pytest.mark.asyncio
async def test_ema_emits_decision_trace_with_regime_fields(caplog):
    cfg = _ema_config(volume_filter_window=0, min_volume_quantile=0.0)
    now = 1_000_000.0
    slow_n = 400
    slow_ts = np.arange(now - slow_n * 14400, now, 14400, dtype=float)
    slow_close = np.linspace(100.0, 200.0, slow_n)
    slow_df = pd.DataFrame({
        "timestamp": slow_ts,
        "open": slow_close, "high": slow_close + 1, "low": slow_close - 1,
        "close": slow_close, "volume": [10.0] * slow_n,
    })
    fast_n = 600
    fast_ts = np.arange(now - fast_n * 300, now, 300, dtype=float)
    fast_close = np.linspace(190.0, 200.0, fast_n)
    fast_df = pd.DataFrame({
        "timestamp": fast_ts,
        "open": fast_close, "high": fast_close + 0.1, "low": fast_close - 0.1,
        "close": fast_close, "volume": [10.0] * fast_n,
    })
    ctrl = _ema_stub(cfg, now=now)

    def _get(connector_name, trading_pair, interval, max_records):
        return fast_df if interval == "5m" else slow_df
    ctrl.market_data_provider.get_candles_df = MagicMock(side_effect=_get)
    with caplog.at_level(logging.INFO):
        await ctrl.update_processed_data()
    trace_lines = [r.message for r in caplog.records if r.message.startswith("EMA tick:")]
    assert len(trace_lines) == 1
    line = trace_lines[0]
    for token in ["signal=", "hold_mode=reentry", "trend_on=", "vol_ok=",
                  "eligible=", "bar_ts=", "bar_age_s=", "rows="]:
        assert token in line, f"Missing field {token!r} in EMA trace: {line}"
