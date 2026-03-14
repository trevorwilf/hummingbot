"""
Tests for BacktestingDataProvider — deterministic, no network.

Tests cover:
- ensure_epoch_index: output index is epoch-based and deterministic
- time window behavior
"""
import pytest
import pandas as pd

from hummingbot.strategy_v2.backtesting.backtesting_data_provider import BacktestingDataProvider


class TestEnsureEpochIndex:
    """Tests for BacktestingDataProvider.ensure_epoch_index()"""

    def test_epoch_index_is_deterministic(self):
        """
        Given a DataFrame with a timestamp column, ensure_epoch_index should
        produce a deterministic integer index based on the timestamps.
        """
        df = pd.DataFrame({
            "timestamp": [1000000, 1000060, 1000120, 1000180],
            "close": [100.0, 101.0, 102.0, 103.0],
        })
        result = BacktestingDataProvider.ensure_epoch_index(df)

        # Index should be numeric
        assert pd.api.types.is_numeric_dtype(result.index)
        # Index should be monotonically increasing
        assert result.index.is_monotonic_increasing
        # Running twice should produce identical results
        df2 = pd.DataFrame({
            "timestamp": [1000000, 1000060, 1000120, 1000180],
            "close": [100.0, 101.0, 102.0, 103.0],
        })
        result2 = BacktestingDataProvider.ensure_epoch_index(df2)
        pd.testing.assert_index_equal(result.index, result2.index)

    def test_epoch_index_preserves_data(self):
        """ensure_epoch_index should not drop or alter data rows."""
        df = pd.DataFrame({
            "timestamp": [1000000, 1000060, 1000120],
            "close": [100.0, 101.0, 102.0],
            "volume": [500, 600, 700],
        })
        result = BacktestingDataProvider.ensure_epoch_index(df)

        assert len(result) == 3
        assert list(result["close"]) == [100.0, 101.0, 102.0]
        assert list(result["volume"]) == [500, 600, 700]

    def test_epoch_index_name(self):
        """Index should be named 'epoch_seconds' by default."""
        df = pd.DataFrame({
            "timestamp": [1000000, 1000060],
            "close": [100.0, 101.0],
        })
        result = BacktestingDataProvider.ensure_epoch_index(df)
        assert result.index.name == "epoch_seconds"

    def test_already_indexed_returns_as_is(self):
        """If index is already named 'epoch_seconds', return unchanged."""
        df = pd.DataFrame({
            "close": [100.0, 101.0],
        }, index=pd.Index([1000000, 1000060], name="epoch_seconds"))

        result = BacktestingDataProvider.ensure_epoch_index(df)
        assert len(result) == 2
        assert result.index.name == "epoch_seconds"

    def test_empty_dataframe_returns_as_is(self):
        """Empty DataFrame should be returned unchanged."""
        df = pd.DataFrame(columns=["timestamp", "close"])
        result = BacktestingDataProvider.ensure_epoch_index(df)
        assert result.empty

    def test_missing_timestamp_column_raises(self):
        """Should raise ValueError when no timestamp column exists."""
        df = pd.DataFrame({"close": [100.0, 101.0]})
        with pytest.raises(ValueError, match="Cannot create timestamp index"):
            BacktestingDataProvider.ensure_epoch_index(df)
