"""
Phase 3 correctness tests for ema_regime_hold_v1.
Pure logic tests — no network, no bot runtime. Mocks market_data_provider and executors_info.
"""
import pathlib
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.core.data_type.common import TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402

from controllers.directional_trading.ema_regime_hold_v1 import (  # noqa: E402
    EMARegimeHoldV1,
    EMARegimeHoldV1Config,
)


def _make_config(**overrides):
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
        regime_ema_fast=50,
        regime_ema_slow=200,
        regime_adx_length=14,
        regime_adx_threshold=20.0,
        volume_filter_window=288,
        min_volume_quantile=0.30,
        hold_mode="reentry",
    )
    base.update(overrides)
    return EMARegimeHoldV1Config(**base)


def _stub_controller(config, now=1_000_000.0, executors_info=None):
    ctrl = EMARegimeHoldV1.__new__(EMARegimeHoldV1)
    ctrl.config = config
    ctrl.executors_info = executors_info or []
    ctrl.processed_data = {}
    mdp = MagicMock()
    mdp.time.return_value = now
    ctrl.market_data_provider = mdp
    return ctrl


def _exec(ts, side=TradeType.BUY, is_active=True, close_ts=None,
          status=RunnableStatus.RUNNING, exec_id="e1"):
    return SimpleNamespace(
        id=exec_id, timestamp=ts, side=side, is_active=is_active,
        close_timestamp=close_ts, status=status,
    )


# ------------- Task 1: hold_mode field -------------

def test_hold_mode_default_is_reentry():
    cfg = _make_config()
    assert cfg.hold_mode == "reentry"


def test_hold_mode_accepts_hold_value():
    cfg = _make_config(hold_mode="hold")
    assert cfg.hold_mode == "hold"


def test_hold_mode_normalizes_case():
    cfg = _make_config(hold_mode="HOLD")
    assert cfg.hold_mode == "hold"


def test_hold_mode_rejects_unknown_value():
    with pytest.raises(Exception):
        _make_config(hold_mode="pause")


# ------------- Task 2: stateful eligibility signal -------------

@pytest.mark.asyncio
async def test_signal_is_stateful_not_edge():
    """Signal must be 1 on every bar where eligible is true, not only the edge."""
    cfg = _make_config(volume_filter_window=0, min_volume_quantile=0.0)  # vol filter permissive
    now = 1_000_000.0
    ctrl = _stub_controller(cfg, now=now)

    slow_n = 400
    slow_ts = np.arange(now - slow_n * 14400, now, 14400, dtype=float)
    slow_close = np.linspace(100.0, 200.0, slow_n)  # strictly rising -> EMA_fast > EMA_slow
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

    def _get_df(connector_name, trading_pair, interval, max_records):
        return fast_df if interval == "5m" else slow_df
    ctrl.market_data_provider.get_candles_df = MagicMock(side_effect=_get_df)

    await ctrl.update_processed_data()
    feats = ctrl.processed_data["features"]
    assert "signal" in feats.columns
    tail_signal_ones = int(feats["signal"].tail(50).sum())
    assert tail_signal_ones > 1, (
        f"Expected stateful signal (>1 bar of signal=1 near end), got {tail_signal_ones}"
    )


@pytest.mark.asyncio
async def test_last_bar_kept_when_already_closed_sparse_feed():
    cfg = _make_config()
    now = 1_000_000.0
    fast_ts = np.array([now - 1800, now - 1500, now - 1200, now - 420], dtype=float)
    fast_df = pd.DataFrame({
        "timestamp": fast_ts, "open": 100, "high": 101, "low": 99,
        "close": 100, "volume": 10,
    })
    ctrl = _stub_controller(cfg, now=now)
    kept = ctrl._drop_incomplete_last_bar(fast_df, "5m")
    assert kept["timestamp"].max() == now - 420


@pytest.mark.asyncio
async def test_last_bar_dropped_when_still_forming():
    cfg = _make_config()
    now = 1_000_000.0
    fast_ts = np.array([now - 900, now - 600, now - 300, now - 60], dtype=float)
    fast_df = pd.DataFrame({
        "timestamp": fast_ts, "open": 100, "high": 101, "low": 99,
        "close": 100, "volume": 10,
    })
    ctrl = _stub_controller(cfg, now=now)
    dropped = ctrl._drop_incomplete_last_bar(fast_df, "5m")
    assert dropped["timestamp"].max() == now - 300


# ------------- Task 3: cooldown fix ported -------------

def test_cooldown_blocks_within_window_after_close():
    cfg = _make_config(cooldown_time=3600)
    now = 1_000_000.0
    closed_recently = _exec(ts=now - 7200, close_ts=now - 600, is_active=False,
                            status=RunnableStatus.TERMINATED)
    ctrl = _stub_controller(cfg, now=now, executors_info=[closed_recently])
    assert ctrl.can_create_executor(signal=1) is False


def test_cooldown_allows_after_window_elapsed():
    cfg = _make_config(cooldown_time=3600)
    now = 1_000_000.0
    closed_long_ago = _exec(ts=now - 10_000, close_ts=now - 7200, is_active=False,
                            status=RunnableStatus.TERMINATED)
    ctrl = _stub_controller(cfg, now=now, executors_info=[closed_long_ago])
    assert ctrl.can_create_executor(signal=1) is True


# ------------- Task 4: hold-mode force-close -------------

def test_hold_mode_stop_actions_empty_when_regime_on():
    cfg = _make_config(hold_mode="hold")
    ctrl = _stub_controller(cfg, executors_info=[_exec(ts=1_000_000 - 600)])
    ctrl.processed_data = {"features": pd.DataFrame({"trend_on_bool": [True, True, True]})}
    assert ctrl.stop_actions_proposal() == []


def test_hold_mode_stop_actions_emit_when_regime_off():
    cfg = _make_config(hold_mode="hold")
    exe = _exec(ts=1_000_000 - 600, exec_id="abc")
    ctrl = _stub_controller(cfg, executors_info=[exe])
    ctrl.processed_data = {"features": pd.DataFrame({"trend_on_bool": [True, True, False]})}
    actions = ctrl.stop_actions_proposal()
    assert len(actions) == 1
    assert actions[0].executor_id == "abc"
    assert actions[0].keep_position is False


def test_hold_mode_skips_already_shutting_down_executors():
    cfg = _make_config(hold_mode="hold")
    exe = _exec(ts=1_000_000 - 600, exec_id="abc", status=RunnableStatus.SHUTTING_DOWN)
    ctrl = _stub_controller(cfg, executors_info=[exe])
    ctrl.processed_data = {"features": pd.DataFrame({"trend_on_bool": [True, False]})}
    assert ctrl.stop_actions_proposal() == []


def test_reentry_mode_never_emits_stop_actions():
    cfg = _make_config(hold_mode="reentry")
    exe = _exec(ts=1_000_000 - 600, exec_id="abc")
    ctrl = _stub_controller(cfg, executors_info=[exe])
    ctrl.processed_data = {"features": pd.DataFrame({"trend_on_bool": [True, False]})}
    assert ctrl.stop_actions_proposal() == []
