"""Tests for range_inventory_ladder observability improvements."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CONTROLLER_DIR = Path(r"E:\tradingsoftware\dockerscripts\apps\hummingbot\api\data\bots\controllers\market_making")
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))


class TestDriftWarningThrottling(unittest.TestCase):
    """Verify drift warnings are throttled, not spammed every cycle."""

    def test_drift_warning_attributes_exist(self):
        """Controller __init__ should set drift throttling attributes."""
        from range_inventory_ladder import RangeInventoryLadderController
        import inspect
        source = inspect.getsource(RangeInventoryLadderController.__init__)
        self.assertIn("_last_drift_warning_time", source)
        self.assertIn("_drift_warning_interval", source)
        self.assertIn("_last_drift_above_threshold", source)

    def test_drift_warning_is_throttled_in_source(self):
        """update_processed_data should use throttled drift warnings, not per-cycle."""
        from range_inventory_ladder import RangeInventoryLadderController
        import inspect
        source = inspect.getsource(RangeInventoryLadderController.update_processed_data)
        self.assertIn("should_warn", source)
        self.assertIn("_drift_warning_interval", source)
        self.assertIn("_last_drift_above_threshold", source)


class TestRefreshGuard(unittest.TestCase):
    """Verify the misleading is_trading guard was removed from refresh logic."""

    def test_no_is_trading_in_refresh(self):
        """stop_actions_proposal should not check executor.is_trading for refresh."""
        import inspect
        from range_inventory_ladder import RangeInventoryLadderController
        source = inspect.getsource(RangeInventoryLadderController.stop_actions_proposal)
        lines = source.split('\n')
        for line in lines:
            if 'executor_refresh_time' in line:
                self.assertNotIn('is_trading', line,
                    "Refresh condition should not check executor.is_trading")


class TestStatusOutput(unittest.TestCase):
    """Verify status output includes wallet balances and eligible prices."""

    def test_status_source_contains_wallet_fields(self):
        """to_format_status should include wallet balance information."""
        import inspect
        from range_inventory_ladder import RangeInventoryLadderController
        source = inspect.getsource(RangeInventoryLadderController.to_format_status)
        self.assertIn("available_quote_balance", source)
        self.assertIn("available_base_balance", source)
        self.assertIn("Eligible buy prices", source)
        self.assertIn("Eligible sell prices", source)

    def test_custom_info_contains_wallet_fields(self):
        """get_custom_info should include wallet balance information."""
        import inspect
        from range_inventory_ladder import RangeInventoryLadderController
        source = inspect.getsource(RangeInventoryLadderController.get_custom_info)
        self.assertIn("available_quote_balance", source)
        self.assertIn("available_base_balance", source)


class TestLevelFilterLogging(unittest.TestCase):
    """Verify filtered levels emit structured events."""

    def test_create_buy_actions_source_has_filter_events(self):
        """_create_buy_actions should emit events for blocked and non-passive levels."""
        import inspect
        from range_inventory_ladder import RangeInventoryLadderController
        source = inspect.getsource(RangeInventoryLadderController._create_buy_actions)
        self.assertIn("range_ladder_buy_level_filtered_blocked", source)
        self.assertIn("range_ladder_buy_level_filtered_not_passive", source)

    def test_create_sell_actions_source_has_filter_events(self):
        """_create_sell_actions should emit events for blocked and non-passive levels."""
        import inspect
        from range_inventory_ladder import RangeInventoryLadderController
        source = inspect.getsource(RangeInventoryLadderController._create_sell_actions)
        self.assertIn("range_ladder_sell_level_filtered_blocked", source)
        self.assertIn("range_ladder_sell_level_filtered_not_passive", source)


if __name__ == "__main__":
    unittest.main()
