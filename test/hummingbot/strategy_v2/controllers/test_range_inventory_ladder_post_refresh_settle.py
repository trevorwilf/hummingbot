"""Tests for the post-refresh settle window feature."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))


def _make_exec(executor_id="e1", level_id="buy_100", age_s=700):
    """Make a mock executor that is active, order_executor type, and is aged past refresh."""
    from hummingbot.strategy_v2.models.base import RunnableStatus
    ex = MagicMock()
    ex.id = executor_id
    ex.status = RunnableStatus.RUNNING
    ex.is_active = True
    ex.timestamp = 0.0  # anchored at 0; mdp.time() returns age_s so age = age_s
    ex.connector_name = "nonkyc"
    ex.custom_info = {}

    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    ex.config = cfg
    return ex


def _make_controller(post_refresh_settle_seconds=0, now_ts=1_000.0, executors=None):
    from range_inventory_ladder import RangeInventoryLadderController
    controller = MagicMock(spec=RangeInventoryLadderController)

    controller.config = MagicMock()
    controller.config.executor_refresh_time = 600
    # These tests validate the LEGACY per-executor-age refresh in stop_actions_proposal.
    controller.config.event_refresh_enabled = False
    controller.config.post_refresh_settle_seconds = post_refresh_settle_seconds
    controller.config.cancel_orders_on_market_data_hard_pause = False
    controller.config.cancel_orders_on_session_end = False
    controller.config.id = "ctrl_test"

    controller.executors_info = executors or []
    controller._market_data_hard_pause = False
    controller._session_expired = False
    controller._session_expired_reason = ""
    controller._config_rebuild_pending = False
    controller._config_rebuild_reason = ""
    controller._pending_runtime_config_signature = None
    controller._applied_runtime_config_signature = None
    controller._refresh_quiet_until = 0.0

    mdp = MagicMock()
    mdp.time.return_value = now_ts
    controller.market_data_provider = mdp

    controller._active_order_executors = MagicMock(return_value=executors or [])
    controller._mark_bypass_cooldown_for_level = MagicMock()
    controller._emit_structured = MagicMock()
    # Preflight-retry fix seams: no dust stops queued, no wave records in these tests.
    controller._reconcile_stop_ids = set()
    controller._refresh_wave = {"buy": None, "sell": None}

    # Bind real methods
    from range_inventory_ladder import RangeInventoryLadderController as _R
    controller.stop_actions_proposal = _R.stop_actions_proposal.__get__(
        controller, _R
    )
    return controller


class TestPostRefreshSettle(unittest.TestCase):

    def test_default_zero_preserves_current_behavior(self):
        """settle=0: refresh stops emitted normally, _refresh_quiet_until stays 0."""
        ex = _make_exec()
        controller = _make_controller(post_refresh_settle_seconds=0, now_ts=1_000.0, executors=[ex])

        actions = controller.stop_actions_proposal()
        self.assertEqual(len(actions), 1)
        self.assertEqual(controller._refresh_quiet_until, 0.0)

    def test_settle_window_blocks_next_refresh_cycle(self):
        """Before the quiet window expires, stop_actions_proposal returns early."""
        ex = _make_exec()
        controller = _make_controller(post_refresh_settle_seconds=5, now_ts=1_000.0, executors=[ex])

        # First call emits stop and opens quiet window
        actions1 = controller.stop_actions_proposal()
        self.assertEqual(len(actions1), 1)
        self.assertEqual(controller._refresh_quiet_until, 1_005.0)

        # Still within the quiet window -> no new actions
        controller.market_data_provider.time.return_value = 1_002.0
        # Make a second executor (also past refresh)
        ex2 = _make_exec(executor_id="e2", level_id="buy_200")
        controller.executors_info = [ex, ex2]
        controller._active_order_executors = MagicMock(return_value=[ex, ex2])
        actions2 = controller.stop_actions_proposal()
        self.assertEqual(actions2, [])

    def test_quiet_window_advances_on_refresh_stop(self):
        """After refresh stops are emitted, _refresh_quiet_until advances."""
        ex = _make_exec()
        controller = _make_controller(post_refresh_settle_seconds=5, now_ts=1_000.0, executors=[ex])

        controller.stop_actions_proposal()
        self.assertEqual(controller._refresh_quiet_until, 1_005.0)

    def test_quiet_window_not_opened_when_no_refresh_stops(self):
        """If no executor ages past refresh, quiet window does NOT open."""
        ex = _make_exec()
        ex.timestamp = 999.0  # age = 1s, not past refresh
        controller = _make_controller(post_refresh_settle_seconds=5, now_ts=1_000.0, executors=[ex])

        controller.stop_actions_proposal()
        self.assertEqual(controller._refresh_quiet_until, 0.0)

    def test_quiet_window_expires_and_refresh_resumes(self):
        """After time advances past quiet window, refresh can fire again."""
        ex = _make_exec()
        controller = _make_controller(post_refresh_settle_seconds=5, now_ts=1_000.0, executors=[ex])

        # First refresh opens window
        controller.stop_actions_proposal()
        self.assertEqual(controller._refresh_quiet_until, 1_005.0)

        # After window expires, next call processes normally
        controller.market_data_provider.time.return_value = 1_010.0
        ex2 = _make_exec(executor_id="e2", level_id="buy_200")
        controller.executors_info = [ex2]
        controller._active_order_executors = MagicMock(return_value=[ex2])
        actions = controller.stop_actions_proposal()
        self.assertEqual(len(actions), 1)

    def test_validator_rejects_negative(self):
        """Pydantic should reject post_refresh_settle_seconds < 0."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        with self.assertRaises(Exception):
            RangeInventoryLadderConfig(
                id="t1",
                controller_name="range_inventory_ladder",
                controller_type="market_making",
                connector_name="nonkyc",
                trading_pair="XMR-USDT",
                total_amount_quote=Decimal("100"),
                buy_prices=[Decimal("321")],
                buy_amounts_pct=[Decimal("1")],
                sell_prices=[Decimal("340")],
                sell_amounts_pct=[Decimal("1")],
                post_refresh_settle_seconds=-1,
            )

    def test_validator_accepts_zero(self):
        from range_inventory_ladder import RangeInventoryLadderConfig
        config = RangeInventoryLadderConfig(
            id="t2",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")],
            buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")],
            sell_amounts_pct=[Decimal("1")],
            post_refresh_settle_seconds=0,
        )
        self.assertEqual(config.post_refresh_settle_seconds, 0)

    def test_config_is_updatable(self):
        """The post_refresh_settle_seconds field must be flagged updatable."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        info = RangeInventoryLadderConfig.model_fields["post_refresh_settle_seconds"]
        extra = info.json_schema_extra or {}
        self.assertTrue(extra.get("is_updatable", False))

    def test_create_actions_proposal_blocked_during_settle(self):
        """create_actions_proposal returns [] and emits block event during settle window."""
        from range_inventory_ladder import RangeInventoryLadderController
        controller = MagicMock(spec=RangeInventoryLadderController)
        controller.processed_data = {
            "market_data_ready": True,
            "initialization_ready": True,
            "blocked_level_ids": set(),
            "free_buy_budget_quote": Decimal("0"),
            "free_sell_budget_base": Decimal("0"),
        }
        controller._market_data_hard_pause = False
        controller._session_expired = False
        controller._accounting_degraded = False  # hbpurse P2: healthy ledger (not IO-degraded)
        controller._purse_degraded = False       # hbpurse P4: healthy purse journal
        controller._purse_degraded_reason = ""
        controller._purse = MagicMock(loaded=True)  # hbpurse P4 (CDX-R01): journal adopted
        controller._state_io_failures = 0
        controller._config_rebuild_pending = False
        controller._refresh_quiet_until = 2_000.0  # future
        mdp = MagicMock()
        mdp.time.return_value = 1_000.0
        controller.market_data_provider = mdp
        controller._active_order_executors = MagicMock(return_value=[])
        controller._emit_structured = MagicMock()

        controller.create_actions_proposal = RangeInventoryLadderController.create_actions_proposal.__get__(
            controller, RangeInventoryLadderController
        )

        actions = controller.create_actions_proposal()
        self.assertEqual(actions, [])
        # Verify the block event was emitted
        emitted_types = [c.args[0] for c in controller._emit_structured.call_args_list]
        self.assertIn("range_ladder_create_blocked_post_refresh_settle", emitted_types)


if __name__ == "__main__":
    unittest.main()
