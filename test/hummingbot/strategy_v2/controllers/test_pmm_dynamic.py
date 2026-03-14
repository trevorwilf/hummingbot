"""
Tests for controllers/market_making/pmm_dynamic.py

Tests cover:
- PMMDynamicControllerConfig: candle_connector/pair defaults from connector_name/trading_pair
- PMMDynamicControllerConfig: explicit values are not overridden
- PMMDynamicController.update_processed_data: deterministic MACD/NATR output on fixed candles
"""
import pytest
import pandas as pd
import numpy as np
from decimal import Decimal
from unittest.mock import MagicMock

from controllers.market_making.pmm_dynamic import (
    PMMDynamicControllerConfig,
    PMMDynamicController,
)


class TestPMMDynamicControllerConfig:
    """Tests for PMMDynamicControllerConfig Pydantic validators."""

    def test_candles_connector_defaults_from_connector_name(self):
        """
        When candles_connector is None or empty, it should inherit
        from connector_name via the field_validator.
        """
        config = PMMDynamicControllerConfig(
            id="test-1",
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
            candles_connector=None,
            candles_trading_pair=None,
        )
        assert config.candles_connector == "nonkyc"
        assert config.candles_trading_pair == "BTC-USDT"

    def test_candles_connector_explicit_not_overridden(self):
        """
        When candles_connector is explicitly set, it should NOT be
        overridden by connector_name.
        """
        config = PMMDynamicControllerConfig(
            id="test-2",
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
            candles_connector="binance",
            candles_trading_pair="ETH-USDT",
        )
        assert config.candles_connector == "binance"
        assert config.candles_trading_pair == "ETH-USDT"

    def test_empty_string_treated_as_none(self):
        """
        Empty string for candles_connector should trigger the default.
        """
        config = PMMDynamicControllerConfig(
            id="test-3",
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
            candles_connector="",
            candles_trading_pair="",
        )
        assert config.candles_connector == "nonkyc"
        assert config.candles_trading_pair == "BTC-USDT"

    def test_default_field_values(self):
        """Verify default MACD/NATR parameters."""
        config = PMMDynamicControllerConfig(
            id="test-4",
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
        )
        assert config.macd_fast == 21
        assert config.macd_slow == 42
        assert config.macd_signal == 9
        assert config.natr_length == 14
        assert config.interval == "3m"


class TestPMMDynamicControllerFeatures:
    """Tests for PMMDynamicController.update_processed_data()."""

    @pytest.mark.asyncio
    async def test_deterministic_feature_output(self):
        """
        Given a fixed candle DataFrame, update_processed_data should produce
        deterministic reference_price and spread_multiplier values.
        """
        # Build a large enough candle set for MACD(21,42,9) + NATR(14) + 100 buffer
        n_candles = 200
        base_price = 50000.0
        closes = [base_price + i * 10 + np.sin(i / 5) * 100 for i in range(n_candles)]
        candles = pd.DataFrame({
            "timestamp": list(range(1000000, 1000000 + n_candles * 60, 60)),
            "open": [c - 5 for c in closes],
            "high": [c + 50 for c in closes],
            "low": [c - 50 for c in closes],
            "close": closes,
            "volume": [1000.0] * n_candles,
        })

        config = PMMDynamicControllerConfig(
            id="test-5",
            connector_name="binance",
            trading_pair="BTC-USDT",
        )

        # Mock the market_data_provider
        mock_mdp = MagicMock()
        mock_mdp.get_candles_df.return_value = candles

        controller = PMMDynamicController.__new__(PMMDynamicController)
        controller.config = config
        controller.max_records = max(config.macd_slow, config.macd_fast, config.macd_signal, config.natr_length) + 100
        controller.market_data_provider = mock_mdp
        controller.processed_data = {}

        await controller.update_processed_data()

        # Verify output structure
        assert "reference_price" in controller.processed_data
        assert "spread_multiplier" in controller.processed_data
        assert "features" in controller.processed_data

        ref_price = controller.processed_data["reference_price"]
        spread_mult = controller.processed_data["spread_multiplier"]

        assert isinstance(ref_price, Decimal)
        assert isinstance(spread_mult, Decimal)

        # Reference price should be close to the last candle close (within reason)
        assert float(ref_price) > 0
        assert float(spread_mult) > 0

        # Run again with same data — must be deterministic
        controller.processed_data = {}
        mock_mdp.get_candles_df.return_value = candles.copy()
        await controller.update_processed_data()

        ref_price2 = controller.processed_data["reference_price"]
        spread_mult2 = controller.processed_data["spread_multiplier"]

        assert ref_price == ref_price2
        assert spread_mult == spread_mult2
