"""Tests for transition-based (rate-limited) per-level diagnostic filter events.

The per-level events range_ladder_{buy,sell}_level_filtered_blocked and
range_ladder_{buy,sell}_level_filtered_not_passive must fire only when a level's
filter reason CHANGES, not every control cycle (per-cycle emission produced
~271 MB/day of diagnostic JSONL). A level returning to eligibility emits
range_ladder_{buy,sell}_level_eligible_again. The diagnostic heartbeat carries
the current not_passive level-id sets per side for steady-state visibility.
"""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CONTROLLER_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))


def _events_of_type(controller, event_type):
    return [
        call for call in controller._emit_structured.call_args_list
        if call.args and call.args[0] == event_type
    ]


def _make_buy_controller(buy_prices, blocked=None, best_bid="340"):
    from range_inventory_ladder import RangeInventoryLadderController
    controller = MagicMock(spec=RangeInventoryLadderController)
    controller.config = MagicMock()
    controller.config.buy_prices = [Decimal(str(p)) for p in buy_prices]
    controller.config.min_order_quote = Decimal("1")
    controller.config.passive_order_placement = True
    controller.processed_data = {
        "blocked_level_ids": set(blocked or set()),
        "free_buy_budget_quote": Decimal("0"),
        "best_bid": Decimal(str(best_bid)),
    }
    controller._emit_structured = MagicMock()
    controller._compress_buy_level_indexes_for_min_notional = MagicMock(return_value=[])
    controller._emit_compression_event_if_changed = MagicMock()
    controller._buy_level_filter_reasons = {}
    # v12 Issue 3: _create_buy_actions checks the side-specific defer flags; False = place.
    controller._defer_buy_creates_this_cycle = False
    controller._defer_sell_creates_this_cycle = False
    controller._price_level_token = RangeInventoryLadderController._price_level_token
    for name in ("_create_buy_actions", "_buy_level_id", "_can_place_buy_level"):
        setattr(
            controller, name,
            getattr(RangeInventoryLadderController, name).__get__(
                controller, RangeInventoryLadderController
            ),
        )
    return controller


def _make_sell_controller(sell_prices, blocked=None, best_ask="330"):
    from range_inventory_ladder import RangeInventoryLadderController
    controller = MagicMock(spec=RangeInventoryLadderController)
    controller.config = MagicMock()
    controller.config.sell_prices = [Decimal(str(p)) for p in sell_prices]
    controller.config.min_order_quote = Decimal("1")
    controller.config.passive_order_placement = True
    controller.processed_data = {
        "blocked_level_ids": set(blocked or set()),
        "free_sell_budget_base": Decimal("0"),
        "best_ask": Decimal(str(best_ask)),
    }
    controller._emit_structured = MagicMock()
    controller._compress_sell_level_indexes_for_min_notional = MagicMock(return_value=[])
    controller._emit_compression_event_if_changed = MagicMock()
    controller._sell_level_filter_reasons = {}
    # v12 Issue 3: _create_sell_actions checks the side-specific defer flags; False = place.
    controller._defer_buy_creates_this_cycle = False
    controller._defer_sell_creates_this_cycle = False
    controller._price_level_token = RangeInventoryLadderController._price_level_token
    for name in ("_create_sell_actions", "_sell_level_id", "_can_place_sell_level"):
        setattr(
            controller, name,
            getattr(RangeInventoryLadderController, name).__get__(
                controller, RangeInventoryLadderController
            ),
        )
    return controller


class TestBuyFilterEventRateLimit(unittest.TestCase):
    """Buy-side per-level filter events fire on transitions only."""

    def test_same_blocked_condition_twice_emits_one_event(self):
        controller = _make_buy_controller([339], blocked={"buy_339"})
        for _ in range(5):
            controller._create_buy_actions()
        events = _events_of_type(controller, "range_ladder_buy_level_filtered_blocked")
        self.assertEqual(1, len(events))
        self.assertEqual("buy_339", events[0].kwargs["level_id"])

    def test_same_not_passive_condition_twice_emits_one_event(self):
        # price 339 >= best_bid 330 -> not passive, every cycle
        controller = _make_buy_controller([339], best_bid="330")
        for _ in range(5):
            controller._create_buy_actions()
        events = _events_of_type(controller, "range_ladder_buy_level_filtered_not_passive")
        self.assertEqual(1, len(events))
        self.assertEqual("buy_339", events[0].kwargs["level_id"])
        self.assertEqual("330", events[0].kwargs["best_bid"])

    def test_condition_change_emits_new_event(self):
        controller = _make_buy_controller([339], blocked={"buy_339"})
        controller._create_buy_actions()
        # transition: blocked -> not_passive
        controller.processed_data["blocked_level_ids"] = set()
        controller.processed_data["best_bid"] = Decimal("330")
        controller._create_buy_actions()
        controller._create_buy_actions()
        blocked_events = _events_of_type(controller, "range_ladder_buy_level_filtered_blocked")
        not_passive_events = _events_of_type(controller, "range_ladder_buy_level_filtered_not_passive")
        self.assertEqual(1, len(blocked_events))
        self.assertEqual(1, len(not_passive_events))
        # a filtered->filtered reason change is NOT a return to eligibility
        self.assertEqual([], _events_of_type(controller, "range_ladder_buy_level_eligible_again"))

    def test_eligible_again_event_fires(self):
        controller = _make_buy_controller([339], blocked={"buy_339"})
        controller._create_buy_actions()
        # transition: blocked -> eligible (339 < best_bid 340, not blocked)
        controller.processed_data["blocked_level_ids"] = set()
        controller._create_buy_actions()
        # the level must re-enter eligibility ON the transition cycle itself
        candidate = controller._compress_buy_level_indexes_for_min_notional.call_args.kwargs["candidate_indexes"]
        self.assertEqual([0], candidate)
        controller._create_buy_actions()
        events = _events_of_type(controller, "range_ladder_buy_level_eligible_again")
        self.assertEqual(1, len(events))
        self.assertEqual("buy_339", events[0].kwargs["level_id"])
        self.assertEqual("blocked", events[0].kwargs["previous_reason"])

    def test_eligible_from_start_emits_no_event(self):
        controller = _make_buy_controller([339], best_bid="340")
        controller._create_buy_actions()
        controller._create_buy_actions()
        self.assertEqual([], _events_of_type(controller, "range_ladder_buy_level_eligible_again"))
        self.assertEqual([], _events_of_type(controller, "range_ladder_buy_level_filtered_blocked"))
        self.assertEqual([], _events_of_type(controller, "range_ladder_buy_level_filtered_not_passive"))

    def test_reblocked_after_eligible_emits_again(self):
        controller = _make_buy_controller([339], blocked={"buy_339"})
        controller._create_buy_actions()
        controller.processed_data["blocked_level_ids"] = set()
        controller._create_buy_actions()
        controller.processed_data["blocked_level_ids"] = {"buy_339"}
        controller._create_buy_actions()
        blocked_events = _events_of_type(controller, "range_ladder_buy_level_filtered_blocked")
        eligible_events = _events_of_type(controller, "range_ladder_buy_level_eligible_again")
        self.assertEqual(2, len(blocked_events))
        self.assertEqual(1, len(eligible_events))

    def test_filter_semantics_unchanged(self):
        """Blocked and not-passive levels are still excluded from eligibility."""
        controller = _make_buy_controller([339, 335, 325], blocked={"buy_335"}, best_bid="338")
        controller._create_buy_actions()
        # 339 not passive (>= 338), 335 blocked, 325 eligible
        candidate = controller._compress_buy_level_indexes_for_min_notional.call_args.kwargs["candidate_indexes"]
        self.assertEqual([2], candidate)


class TestSellFilterEventRateLimit(unittest.TestCase):
    """Sell-side per-level filter events fire on transitions only."""

    def test_same_blocked_condition_twice_emits_one_event(self):
        controller = _make_sell_controller([350], blocked={"sell_350"})
        for _ in range(5):
            controller._create_sell_actions()
        events = _events_of_type(controller, "range_ladder_sell_level_filtered_blocked")
        self.assertEqual(1, len(events))
        self.assertEqual("sell_350", events[0].kwargs["level_id"])

    def test_same_not_passive_condition_twice_emits_one_event(self):
        # price 350 <= best_ask 360 -> not passive, every cycle
        controller = _make_sell_controller([350], best_ask="360")
        for _ in range(5):
            controller._create_sell_actions()
        events = _events_of_type(controller, "range_ladder_sell_level_filtered_not_passive")
        self.assertEqual(1, len(events))
        self.assertEqual("sell_350", events[0].kwargs["level_id"])
        self.assertEqual("360", events[0].kwargs["best_ask"])

    def test_condition_change_emits_new_event(self):
        controller = _make_sell_controller([350], blocked={"sell_350"})
        controller._create_sell_actions()
        # transition: blocked -> not_passive
        controller.processed_data["blocked_level_ids"] = set()
        controller.processed_data["best_ask"] = Decimal("360")
        controller._create_sell_actions()
        controller._create_sell_actions()
        blocked_events = _events_of_type(controller, "range_ladder_sell_level_filtered_blocked")
        not_passive_events = _events_of_type(controller, "range_ladder_sell_level_filtered_not_passive")
        self.assertEqual(1, len(blocked_events))
        self.assertEqual(1, len(not_passive_events))
        # a filtered->filtered reason change is NOT a return to eligibility
        self.assertEqual([], _events_of_type(controller, "range_ladder_sell_level_eligible_again"))

    def test_eligible_again_event_fires(self):
        # not_passive first (350 <= 360), then eligible (350 > 330)
        controller = _make_sell_controller([350], best_ask="360")
        controller._create_sell_actions()
        controller.processed_data["best_ask"] = Decimal("330")
        controller._create_sell_actions()
        # the level must re-enter eligibility ON the transition cycle itself
        candidate = controller._compress_sell_level_indexes_for_min_notional.call_args.kwargs["candidate_indexes"]
        self.assertEqual([0], candidate)
        controller._create_sell_actions()
        events = _events_of_type(controller, "range_ladder_sell_level_eligible_again")
        self.assertEqual(1, len(events))
        self.assertEqual("sell_350", events[0].kwargs["level_id"])
        self.assertEqual("not_passive", events[0].kwargs["previous_reason"])

    def test_eligible_from_start_emits_no_event(self):
        controller = _make_sell_controller([350], best_ask="330")
        controller._create_sell_actions()
        controller._create_sell_actions()
        self.assertEqual([], _events_of_type(controller, "range_ladder_sell_level_eligible_again"))
        self.assertEqual([], _events_of_type(controller, "range_ladder_sell_level_filtered_blocked"))
        self.assertEqual([], _events_of_type(controller, "range_ladder_sell_level_filtered_not_passive"))

    def test_reblocked_after_eligible_emits_again(self):
        controller = _make_sell_controller([350], blocked={"sell_350"}, best_ask="330")
        controller._create_sell_actions()
        controller.processed_data["blocked_level_ids"] = set()
        controller._create_sell_actions()
        controller.processed_data["blocked_level_ids"] = {"sell_350"}
        controller._create_sell_actions()
        blocked_events = _events_of_type(controller, "range_ladder_sell_level_filtered_blocked")
        eligible_events = _events_of_type(controller, "range_ladder_sell_level_eligible_again")
        self.assertEqual(2, len(blocked_events))
        self.assertEqual(1, len(eligible_events))

    def test_filter_semantics_unchanged(self):
        controller = _make_sell_controller([350, 355, 360], blocked={"sell_355"}, best_ask="352")
        controller._create_sell_actions()
        # 350 not passive (<= 352), 355 blocked, 360 eligible
        candidate = controller._compress_sell_level_indexes_for_min_notional.call_args.kwargs["candidate_indexes"]
        self.assertEqual([2], candidate)


class TestHeartbeatCarriesFilterSets(unittest.TestCase):
    """The diagnostic heartbeat reports blocked and not_passive level ids."""

    def _make_heartbeat_controller(self):
        from range_inventory_ladder import RangeInventoryLadderController
        controller = MagicMock(spec=RangeInventoryLadderController)
        controller.config = MagicMock()
        controller.config.diagnostic_log_enabled = True
        controller.config.diagnostic_heartbeat_interval_seconds = 300
        controller.processed_data = {
            "blocked_level_ids": {"buy_305", "sell_355"},
        }
        controller.market_data_provider = MagicMock()
        controller.market_data_provider.time = MagicMock(return_value=1000.0)
        controller._last_diagnostic_heartbeat_ts = 0.0
        controller._session_started_ts = None
        controller._session_expired = False
        controller._session_expired_reason = ""
        controller._market_data_hard_pause = False
        controller._active_order_executors = MagicMock(return_value=[])
        controller.positions_held = []
        controller._buy_reservation_sources = {}
        controller._sell_reservation_sources = {}
        controller._buy_level_filter_reasons = {
            "buy_339": "not_passive",
            "buy_335": "not_passive",
            "buy_305": "blocked",
            "buy_324": None,
        }
        controller._sell_level_filter_reasons = {
            "sell_350": "not_passive",
            "sell_355": "blocked",
            "sell_360": None,
        }
        controller._emit_structured = MagicMock()
        controller._emit_diagnostic_heartbeat_if_due = (
            RangeInventoryLadderController._emit_diagnostic_heartbeat_if_due.__get__(
                controller, RangeInventoryLadderController
            )
        )
        return controller

    def test_heartbeat_carries_not_passive_sets_per_side(self):
        controller = self._make_heartbeat_controller()
        controller._emit_diagnostic_heartbeat_if_due()
        calls = _events_of_type(controller, "range_ladder_diagnostic_heartbeat")
        self.assertEqual(1, len(calls))
        kwargs = calls[0].kwargs
        self.assertEqual(["buy_335", "buy_339"], kwargs["not_passive_buy_level_ids"])
        self.assertEqual(["sell_350"], kwargs["not_passive_sell_level_ids"])

    def test_heartbeat_still_carries_blocked_set(self):
        controller = self._make_heartbeat_controller()
        controller._emit_diagnostic_heartbeat_if_due()
        kwargs = _events_of_type(controller, "range_ladder_diagnostic_heartbeat")[0].kwargs
        self.assertEqual(["buy_305", "sell_355"], kwargs["blocked_level_ids"])

    def test_heartbeat_respects_interval(self):
        controller = self._make_heartbeat_controller()
        controller._emit_diagnostic_heartbeat_if_due()
        # 100s later: not due yet (interval 300s)
        controller.market_data_provider.time = MagicMock(return_value=1100.0)
        controller._emit_diagnostic_heartbeat_if_due()
        calls = _events_of_type(controller, "range_ladder_diagnostic_heartbeat")
        self.assertEqual(1, len(calls))


class TestActionStateFlowsIntoHeartbeat(unittest.TestCase):
    """End-to-end: the reason map written by _create_buy_actions is what the
    heartbeat reports, and levels removed from the config are pruned from it."""

    def _make_flow_controller(self, buy_prices, best_bid):
        from range_inventory_ladder import RangeInventoryLadderController
        controller = _make_buy_controller(buy_prices, best_bid=best_bid)
        controller.config.diagnostic_log_enabled = True
        controller.config.diagnostic_heartbeat_interval_seconds = 300
        controller.market_data_provider = MagicMock()
        controller.market_data_provider.time = MagicMock(return_value=1000.0)
        controller._last_diagnostic_heartbeat_ts = 0.0
        controller._session_started_ts = None
        controller._session_expired = False
        controller._session_expired_reason = ""
        controller._market_data_hard_pause = False
        controller._active_order_executors = MagicMock(return_value=[])
        controller.positions_held = []
        controller._buy_reservation_sources = {}
        controller._sell_reservation_sources = {}
        controller._sell_level_filter_reasons = {}
        controller._emit_diagnostic_heartbeat_if_due = (
            RangeInventoryLadderController._emit_diagnostic_heartbeat_if_due.__get__(
                controller, RangeInventoryLadderController
            )
        )
        return controller

    def test_heartbeat_reports_state_written_by_create_actions(self):
        # 339 not passive (>= 337), 335 eligible (< 337)
        controller = self._make_flow_controller([339, 335], best_bid="337")
        controller._create_buy_actions()
        controller._emit_diagnostic_heartbeat_if_due()
        kwargs = _events_of_type(controller, "range_ladder_diagnostic_heartbeat")[0].kwargs
        self.assertEqual(["buy_339"], kwargs["not_passive_buy_level_ids"])

    def test_levels_removed_from_config_are_pruned(self):
        controller = self._make_flow_controller([339, 335], best_bid="337")
        controller._create_buy_actions()
        # runtime config rebuild drops the 339 level entirely
        controller.config.buy_prices = [Decimal("335")]
        controller._create_buy_actions()
        controller._emit_diagnostic_heartbeat_if_due()
        kwargs = _events_of_type(controller, "range_ladder_diagnostic_heartbeat")[0].kwargs
        self.assertEqual([], kwargs["not_passive_buy_level_ids"])


if __name__ == "__main__":
    unittest.main()
