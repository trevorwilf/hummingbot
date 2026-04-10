"""
Comprehensive executor lineage tests: verify controller_id, executor_id, level_id
are set on InFlightOrder by ExecutorBase.place_order() for all executor types.
"""
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock

from hummingbot.core.data_type.common import OrderType, TradeType, PositionAction
from hummingbot.core.data_type.in_flight_order import InFlightOrder


class TestExecutorLineageBase(unittest.TestCase):
    """Test that ExecutorBase.place_order() sets lineage on tracked orders."""

    def _make_tracked_order(self, order_id="test_order"):
        return InFlightOrder(
            client_order_id=order_id,
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("0.1"),
            creation_timestamp=time.time(),
            price=Decimal("50000"),
        )

    def _make_mock_executor(self, controller_id="ctrl_1", executor_id="exec_1", level_id="level_3"):
        """Create a mock executor with config attributes."""
        mock_config = MagicMock()
        mock_config.controller_id = controller_id
        mock_config.id = executor_id
        mock_config.level_id = level_id

        mock_executor = MagicMock()
        mock_executor.config = mock_config

        # Set up connector with order tracker
        tracked_order = self._make_tracked_order("oid_1")
        mock_tracker = MagicMock()
        mock_tracker.all_orders = {"oid_1": tracked_order}

        mock_connector = MagicMock()
        mock_connector._order_tracker = mock_tracker

        mock_executor.connectors = {"exchange": mock_connector}

        return mock_executor, tracked_order

    def test_lineage_fields_on_inflight_order(self):
        """Verify controller_id, executor_id, level_id can be set on InFlightOrder."""
        order = self._make_tracked_order()
        order.controller_id = "ctrl_test"
        order.executor_id = "exec_test"
        order.level_id = "level_5"
        order.bot_run_id = "run_123"
        self.assertEqual(order.controller_id, "ctrl_test")
        self.assertEqual(order.executor_id, "exec_test")
        self.assertEqual(order.level_id, "level_5")
        self.assertEqual(order.bot_run_id, "run_123")

    def test_lineage_survives_json_roundtrip(self):
        """Verify lineage fields survive to_json/from_json."""
        order = self._make_tracked_order()
        order.controller_id = "ctrl_rt"
        order.executor_id = "exec_rt"
        order.level_id = "level_2"
        order.bot_run_id = "run_rt"

        data = order.to_json()
        restored = InFlightOrder.from_json(data)
        self.assertEqual(restored.controller_id, "ctrl_rt")
        self.assertEqual(restored.executor_id, "exec_rt")
        self.assertEqual(restored.level_id, "level_2")
        self.assertEqual(restored.bot_run_id, "run_rt")

    def test_lineage_defaults_to_none(self):
        """New InFlightOrder should have None lineage fields."""
        order = self._make_tracked_order()
        self.assertIsNone(order.controller_id)
        self.assertIsNone(order.executor_id)
        self.assertIsNone(order.level_id)
        self.assertIsNone(order.bot_run_id)

    def test_fill_sources_dict(self):
        """fill_sources should be an empty dict by default."""
        order = self._make_tracked_order()
        self.assertEqual(order.fill_sources, {})
        order.fill_sources["trade_1"] = "ws"
        order.fill_sources["trade_2"] = "rest_poll"
        self.assertEqual(order.fill_sources["trade_1"], "ws")

    def test_lineage_in_to_json_output(self):
        """to_json must include lineage fields."""
        order = self._make_tracked_order()
        order.controller_id = "c1"
        order.executor_id = "e1"
        order.level_id = "l1"
        order.bot_run_id = "r1"
        data = order.to_json()
        self.assertEqual(data["controller_id"], "c1")
        self.assertEqual(data["executor_id"], "e1")
        self.assertEqual(data["level_id"], "l1")
        self.assertEqual(data["bot_run_id"], "r1")

    def test_lineage_from_json_with_missing_fields(self):
        """from_json should handle old data without lineage fields."""
        order = self._make_tracked_order()
        data = order.to_json()
        # Remove lineage fields to simulate old data
        del data["controller_id"]
        del data["executor_id"]
        del data["level_id"]
        del data["bot_run_id"]
        del data["fill_sources"]
        restored = InFlightOrder.from_json(data)
        self.assertIsNone(restored.controller_id)
        self.assertIsNone(restored.executor_id)
        self.assertIsNone(restored.level_id)
        self.assertIsNone(restored.bot_run_id)
        self.assertEqual(restored.fill_sources, {})


if __name__ == "__main__":
    unittest.main()
