"""Tests for hummingbot.strategy_v2.models.position_config"""
import pytest
from decimal import Decimal
from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.models.position_config import InitialPositionConfig


class TestInitialPositionConfig:
    def test_basic_creation(self):
        config = InitialPositionConfig(
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
            amount=Decimal("0.5"),
            side=TradeType.BUY,
        )
        assert config.connector_name == "nonkyc"
        assert config.trading_pair == "BTC-USDT"
        assert config.amount == Decimal("0.5")
        assert config.side == TradeType.BUY

    def test_side_from_string(self):
        """The field_validator should parse string 'BUY' to TradeType.BUY."""
        config = InitialPositionConfig(
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
            amount=Decimal("1.0"),
            side="BUY",
        )
        assert config.side == TradeType.BUY

    def test_round_trip(self):
        config = InitialPositionConfig(
            connector_name="nonkyc",
            trading_pair="ETH-USDT",
            amount=Decimal("10.0"),
            side=TradeType.SELL,
        )
        dumped = config.model_dump()
        restored = InitialPositionConfig(**dumped)
        assert restored.connector_name == config.connector_name
        assert restored.amount == config.amount
