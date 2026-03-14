"""Tests for hummingbot.strategy_v2.models.executor_actions — action model round-trips."""
import pytest
from hummingbot.strategy_v2.models.executor_actions import (
    ExecutorAction,
    CreateExecutorAction,
    StopExecutorAction,
    StoreExecutorAction,
)


class TestExecutorAction:
    def test_default_controller_id(self):
        action = ExecutorAction()
        assert action.controller_id == "main"

    def test_custom_controller_id(self):
        action = ExecutorAction(controller_id="secondary")
        assert action.controller_id == "secondary"


class TestStopExecutorAction:
    def test_fields(self):
        action = StopExecutorAction(executor_id="exec-123")
        assert action.executor_id == "exec-123"
        assert action.keep_position is False  # default

    def test_keep_position_true(self):
        action = StopExecutorAction(executor_id="exec-123", keep_position=True)
        assert action.keep_position is True

    def test_round_trip(self):
        action = StopExecutorAction(executor_id="exec-456", controller_id="ctrl-1")
        dumped = action.model_dump()
        restored = StopExecutorAction(**dumped)
        assert restored.executor_id == action.executor_id
        assert restored.controller_id == action.controller_id
        assert restored.keep_position == action.keep_position


class TestStoreExecutorAction:
    def test_fields(self):
        action = StoreExecutorAction(executor_id="exec-789")
        assert action.executor_id == "exec-789"
        assert action.controller_id == "main"
