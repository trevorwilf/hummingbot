"""Tests for hummingbot.strategy_v2.models.executors — CloseType enum."""
import pytest
from hummingbot.strategy_v2.models.executors import CloseType


class TestCloseType:
    def test_all_close_types_exist(self):
        expected = [
            "TIME_LIMIT", "STOP_LOSS", "TAKE_PROFIT", "EXPIRED",
            "EARLY_STOP", "TRAILING_STOP", "INSUFFICIENT_BALANCE",
            "FAILED", "COMPLETED", "POSITION_HOLD",
        ]
        for name in expected:
            assert hasattr(CloseType, name), f"Missing CloseType.{name}"

    def test_values_are_unique(self):
        values = [ct.value for ct in CloseType]
        assert len(values) == len(set(values)), "Duplicate CloseType values"

    def test_round_trip_by_value(self):
        for ct in CloseType:
            assert CloseType(ct.value) is ct

    def test_round_trip_by_name(self):
        for ct in CloseType:
            assert CloseType[ct.name] is ct
