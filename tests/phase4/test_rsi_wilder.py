"""
Phase 4 rsi_wilder edge-case tests.
"""
import pathlib
import sys

import numpy as np
import pandas as pd

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.strategy_v2.utils.ta_utils import rsi_wilder  # noqa: E402


def test_all_gain_returns_100():
    s = pd.Series(np.arange(1, 51, dtype=float))
    r = rsi_wilder(s, 14)
    tail = r.tail(5).tolist()
    assert all(abs(v - 100.0) < 1e-9 for v in tail), f"Expected all 100.0, got {tail}"


def test_all_loss_returns_0():
    s = pd.Series(np.arange(50, 0, -1, dtype=float))
    r = rsi_wilder(s, 14)
    tail = r.tail(5).tolist()
    assert all(abs(v - 0.0) < 1e-9 for v in tail), f"Expected all 0.0, got {tail}"


def test_flat_returns_50():
    s = pd.Series([100.0] * 50)
    r = rsi_wilder(s, 14)
    tail = r.tail(5).tolist()
    assert all(abs(v - 50.0) < 1e-9 for v in tail), f"Expected all 50.0, got {tail}"


def test_warmup_rows_remain_nan():
    s = pd.Series(np.arange(1, 51, dtype=float))
    r = rsi_wilder(s, 14)
    assert r.iloc[:14].isna().all(), "Expected warmup rows to be NaN"


def test_mixed_series_produces_finite_values_in_middle():
    s = pd.Series([100.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(60)])
    r = rsi_wilder(s, 14)
    finite_tail = r.dropna()
    assert len(finite_tail) > 0
    assert (finite_tail >= 0.0).all() and (finite_tail <= 100.0).all()


def test_transition_from_all_gain_to_flat_reaches_bounds():
    rising = list(np.arange(1, 31, dtype=float))
    flat = [30.0] * 30
    s = pd.Series(rising + flat)
    r = rsi_wilder(s, 14)
    assert abs(r.iloc[29] - 100.0) < 1e-9
    assert r.iloc[-1] <= r.iloc[29]
    assert 0.0 <= r.iloc[-1] <= 100.0
