"""Tests for range_inventory_ladder shutdown-aware reservation and level-blocking."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.models.base import RunnableStatus


def _make_executor_info(
    *,
    status,
    side=TradeType.BUY,
    price=Decimal("100"),
    amount=Decimal("1"),
    executed_base=Decimal("0"),
    level_id="buy_100",
    close_timestamp=None,
    executor_id="e1",
):
    executor = MagicMock()
    executor.id = executor_id
    executor.status = status
    executor.close_timestamp = close_timestamp
    executor.is_active = status in (RunnableStatus.NOT_STARTED, RunnableStatus.RUNNING)
    executor.connector_name = "nonkyc"
    executor.custom_info = {}

    config = MagicMock()
    config.type = "order_executor"
    config.side = side
    config.price = price
    config.amount = amount
    config.level_id = level_id
    executor.config = config
    # For _remaining_open_order_amounts: no in_flight_order, config_fallback path
    return executor


def _make_controller(executors, cooldown_time=30):
    from range_inventory_ladder import RangeInventoryLadderController
    controller = MagicMock(spec=RangeInventoryLadderController)
    controller.executors_info = executors
    controller.config = MagicMock()
    controller.config.cooldown_time = cooldown_time
    # These tests validate the LEGACY per-level cooldown in _recently_closed_level_ids; pin the
    # legacy mode (event mode neutralizes the per-level cooldown in favor of the per-side model).
    controller.config.event_refresh_enabled = False
    controller._buy_reservation_sources = {}
    controller._sell_reservation_sources = {}

    mdp = MagicMock()
    mdp.time.return_value = 1_000_000.0
    mdp.get_connector = MagicMock(side_effect=Exception("no connector in test"))
    controller.market_data_provider = mdp

    controller._cleanup_cooldown_bypass = MagicMock()
    controller._should_bypass_level_cooldown = MagicMock(return_value=False)
    # v12 Part B: _recently_closed_level_ids reads these recycle-window deadlines.
    # 0.0 (their real __init__ default) means "no window open" -> cooldown logic unchanged.
    controller._recycle_bypass_buy_until = 0.0
    controller._recycle_bypass_sell_until = 0.0

    # Bind the real methods under test
    controller._order_executors_active_or_shutting_down = (
        RangeInventoryLadderController._order_executors_active_or_shutting_down.__get__(
            controller, RangeInventoryLadderController
        )
    )
    controller._active_reserved_quote_for_buys = (
        RangeInventoryLadderController._active_reserved_quote_for_buys.__get__(
            controller, RangeInventoryLadderController
        )
    )
    controller._active_reserved_base_for_sells = (
        RangeInventoryLadderController._active_reserved_base_for_sells.__get__(
            controller, RangeInventoryLadderController
        )
    )
    controller._recently_closed_level_ids = (
        RangeInventoryLadderController._recently_closed_level_ids.__get__(
            controller, RangeInventoryLadderController
        )
    )
    controller._executor_side = RangeInventoryLadderController._executor_side
    controller._remaining_open_order_amounts = (
        RangeInventoryLadderController._remaining_open_order_amounts.__get__(
            controller, RangeInventoryLadderController
        )
    )
    controller._get_executor_in_flight_order = MagicMock(return_value=None)
    controller._d = RangeInventoryLadderController._d
    return controller


class TestShutdownAwareReservation(unittest.TestCase):

    def test_shutting_down_executor_counts_toward_buy_reservation(self):
        ex = _make_executor_info(
            status=RunnableStatus.SHUTTING_DOWN,
            side=TradeType.BUY,
            price=Decimal("100"),
            amount=Decimal("1"),
            executed_base=Decimal("0"),
            level_id="buy_100",
        )
        controller = _make_controller([ex])
        reserved = controller._active_reserved_quote_for_buys()
        self.assertEqual(reserved, Decimal("100"))

    def test_shutting_down_executor_counts_toward_sell_reservation(self):
        ex = _make_executor_info(
            status=RunnableStatus.SHUTTING_DOWN,
            side=TradeType.SELL,
            price=Decimal("100"),
            amount=Decimal("0.5"),
            level_id="sell_100",
        )
        controller = _make_controller([ex])
        reserved = controller._active_reserved_base_for_sells()
        self.assertEqual(reserved, Decimal("0.5"))

    def test_shutting_down_executor_blocks_level_id(self):
        ex = _make_executor_info(
            status=RunnableStatus.SHUTTING_DOWN,
            level_id="buy_333",
            close_timestamp=None,
        )
        controller = _make_controller([ex])
        blocked = controller._recently_closed_level_ids()
        self.assertIn("buy_333", blocked)

    def test_terminated_executor_with_old_close_timestamp_is_not_blocked(self):
        now_ts = 1_000_000.0
        ex = _make_executor_info(
            status=RunnableStatus.TERMINATED,
            level_id="buy_100",
            close_timestamp=now_ts - 31 - 5,  # cooldown_time=30
        )
        controller = _make_controller([ex], cooldown_time=30)
        blocked = controller._recently_closed_level_ids()
        self.assertNotIn("buy_100", blocked)

    def test_terminated_executor_within_cooldown_still_blocked(self):
        now_ts = 1_000_000.0
        ex = _make_executor_info(
            status=RunnableStatus.TERMINATED,
            level_id="buy_100",
            close_timestamp=now_ts - 1,  # within 30s cooldown
        )
        controller = _make_controller([ex], cooldown_time=30)
        blocked = controller._recently_closed_level_ids()
        self.assertIn("buy_100", blocked)

    def test_running_executor_still_blocks_and_reserves_as_before(self):
        ex = _make_executor_info(
            status=RunnableStatus.RUNNING,
            side=TradeType.BUY,
            price=Decimal("100"),
            amount=Decimal("1"),
            level_id="buy_100",
        )
        controller = _make_controller([ex])
        self.assertEqual(controller._active_reserved_quote_for_buys(), Decimal("100"))
        self.assertIn("buy_100", controller._recently_closed_level_ids())

    def test_cross_status_transition_reservation_continuity(self):
        """Reservation stays > 0 through RUNNING and SHUTTING_DOWN, drops to 0 at TERMINATED."""
        # RUNNING
        ex_running = _make_executor_info(
            status=RunnableStatus.RUNNING, side=TradeType.BUY,
            price=Decimal("100"), amount=Decimal("1"),
        )
        c1 = _make_controller([ex_running])
        self.assertEqual(c1._active_reserved_quote_for_buys(), Decimal("100"))

        # SHUTTING_DOWN
        ex_shutting = _make_executor_info(
            status=RunnableStatus.SHUTTING_DOWN, side=TradeType.BUY,
            price=Decimal("100"), amount=Decimal("1"),
        )
        c2 = _make_controller([ex_shutting])
        self.assertEqual(c2._active_reserved_quote_for_buys(), Decimal("100"))

        # TERMINATED (with recent close_timestamp so still in cooldown but NOT reserving)
        ex_terminated = _make_executor_info(
            status=RunnableStatus.TERMINATED, side=TradeType.BUY,
            price=Decimal("100"), amount=Decimal("1"),
            close_timestamp=1_000_000.0 - 1,
        )
        c3 = _make_controller([ex_terminated], cooldown_time=30)
        self.assertEqual(c3._active_reserved_quote_for_buys(), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
