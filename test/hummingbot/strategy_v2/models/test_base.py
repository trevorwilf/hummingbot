"""Tests for hummingbot.strategy_v2.models.base"""
import pytest
from hummingbot.strategy_v2.models.base import RunnableStatus


class TestRunnableStatus:
    def test_enum_values(self):
        assert RunnableStatus.NOT_STARTED.value == 1
        assert RunnableStatus.RUNNING.value == 2
        assert RunnableStatus.SHUTTING_DOWN.value == 3
        assert RunnableStatus.TERMINATED.value == 4

    def test_enum_identity(self):
        assert RunnableStatus(1) is RunnableStatus.NOT_STARTED
        assert RunnableStatus(4) is RunnableStatus.TERMINATED

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            RunnableStatus(99)
