"""
Tests for BacktestingEngineBase — deterministic, no network.

Tests cover:
- prepare_market_data: merge_asof direction="backward" (no look-ahead)
- prepare_market_data: default features when controller has no "features" key
- summarize_results: known-path metric validation (net_pnl, drawdown, sharpe, profit_factor, accuracy)
- summarize_results: empty executor list returns zero dict
"""
import pandas as pd
import numpy as np
import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase


class TestPrepareMarketData:
    """Tests for BacktestingEngineBase.prepare_market_data()"""

    def test_no_lookahead_with_merge_asof(self, sample_candles_df, sample_features_df):
        """
        After merge_asof with direction='backward', each row must only see
        feature timestamps <= the candle timestamp. No future leakage.
        """
        engine = _make_engine_with_candles_and_features(sample_candles_df, sample_features_df)

        # Capture the features df before prepare_market_data overwrites the timestamp column
        # We verify by checking that all feature values in merged rows correspond to
        # feature timestamps that are <= the candle timestamp
        features_ts = sorted(sample_features_df["timestamp"].tolist())

        result = engine.prepare_market_data()

        # For each row, the reference_price should match one of the feature rows
        # whose timestamp <= the candle timestamp (timestamp_bt)
        for _, row in result.iterrows():
            if pd.notna(row.get("reference_price")):
                ref_price = row["reference_price"]
                candle_ts = row["timestamp_bt"]
                # Find which feature this came from
                for feat_idx, feat_ts in enumerate(features_ts):
                    if sample_features_df.iloc[feat_idx]["reference_price"] == ref_price:
                        assert feat_ts <= candle_ts, (
                            f"Look-ahead detected: feature timestamp {feat_ts} > "
                            f"candle timestamp {candle_ts}"
                        )
                        break

    def test_defaults_without_features(self, sample_candles_df):
        """
        When controller.processed_data has no 'features' key,
        defaults should be: reference_price=close_bt, spread_multiplier=1, signal=0
        """
        engine = _make_engine_with_candles_and_features(sample_candles_df, features_df=None)
        result = engine.prepare_market_data()

        assert "reference_price" in result.columns
        assert "spread_multiplier" in result.columns
        assert "signal" in result.columns

        # reference_price should equal close_bt
        np.testing.assert_array_almost_equal(
            result["reference_price"].values,
            result["close_bt"].values
        )
        # spread_multiplier should be 1 everywhere
        assert (result["spread_multiplier"] == 1).all()
        # signal should be 0 everywhere
        assert (result["signal"] == 0).all()


class TestSummarizeResults:
    """Tests for BacktestingEngineBase.summarize_results()"""

    def test_known_metrics_path(self, mock_executor_info_list):
        """
        With known PnL values [10, 5, 15, -8, -3], validate:
        - net_pnl_quote = 19.0
        - total_executors = 5
        - win_signals = 3, loss_signals = 2
        - profit_factor = 30/11
        - accuracy = 3/5 = 0.6
        """
        result = BacktestingEngineBase.summarize_results(
            mock_executor_info_list, total_amount_quote=1000
        )

        assert result["net_pnl_quote"] == pytest.approx(19.0, abs=1e-6)
        assert result["total_executors"] == 5
        assert result["total_executors_with_position"] == 5
        assert result["win_signals"] == 3
        assert result["loss_signals"] == 2
        assert result["accuracy"] == pytest.approx(0.6, abs=1e-6)
        assert result["profit_factor"] == pytest.approx(30.0 / 11.0, abs=1e-6)
        assert result["net_pnl"] == pytest.approx(19.0 / 1000.0, abs=1e-6)

        # Drawdown should be non-positive
        assert result["max_drawdown_usd"] <= 0
        # Sharpe should be a finite number
        assert np.isfinite(result["sharpe_ratio"])

    def test_empty_executors_returns_zero_dict(self):
        """
        Empty executor list should return the zero-value fallback dict.
        """
        result = BacktestingEngineBase.summarize_results([], total_amount_quote=1000)

        assert result["net_pnl"] == 0
        assert result["net_pnl_quote"] == 0
        assert result["total_executors"] == 0
        assert result["sharpe_ratio"] == 0
        assert result["profit_factor"] == 0

    def test_all_winning_trades_profit_factor(self):
        """
        When all trades are profitable, profit_factor should be handled
        gracefully (no division by zero). The code returns 1 when total_loss == 0.
        """
        infos = []
        for i in range(3):
            info = MagicMock()
            info.net_pnl_quote = Decimal("10.0")
            info.filled_amount_quote = Decimal("100.0")
            info.timestamp = 1000000 + i * 60
            info.close_type = MagicMock()
            info.close_type.name = "TAKE_PROFIT"
            info.custom_info = {"side": MagicMock()}
            info.to_dict.return_value = {
                "net_pnl_quote": 10.0,
                "filled_amount_quote": 100.0,
                "timestamp": 1000000 + i * 60,
                "close_type": info.close_type,
                "side": info.custom_info["side"],
            }
            infos.append(info)

        result = BacktestingEngineBase.summarize_results(infos, total_amount_quote=1000)
        assert result["profit_factor"] == 1  # code returns 1 when total_loss == 0


# ── Helper to construct a testable engine ──────────────────────────────────

def _make_engine_with_candles_and_features(candles_df, features_df=None):
    """
    Build a minimal BacktestingEngineBase with mocked controller/data provider
    so prepare_market_data() can run without a real exchange connection.
    """
    engine = BacktestingEngineBase.__new__(BacktestingEngineBase)

    # Mock controller
    engine.controller = MagicMock()
    engine.controller.config.connector_name = "binance"
    engine.controller.config.trading_pair = "BTC-USDT"
    engine.backtesting_resolution = "1m"

    # Mock market_data_provider.get_candles_df to return our fixture
    candles_copy = candles_df.copy()
    engine.controller.market_data_provider.get_candles_df.return_value = candles_copy

    # Set processed_data
    if features_df is not None:
        engine.controller.processed_data = {"features": features_df.copy()}
    else:
        engine.controller.processed_data = {}

    return engine
