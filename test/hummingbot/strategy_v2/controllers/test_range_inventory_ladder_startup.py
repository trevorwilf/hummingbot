"""Tests for range_inventory_ladder startup base-claim and feasibility warning."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

_CONTROLLER_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))


class TestStartupBaseClaim(unittest.TestCase):
    """Verify startup base-claim config options work correctly."""

    def test_config_accepts_claimed_base_amount(self):
        """The config model should accept claimed_base_amount as an optional Decimal."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        config = RangeInventoryLadderConfig(
            id="test_1",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=100,
            buy_prices=[321, 318, 315],
            buy_amounts_pct=[1, 1, 1],
            sell_prices=[350, 355, 360],
            sell_amounts_pct=[1, 1, 0.5],
            claimed_base_amount=Decimal("0.003"),
        )
        self.assertEqual(config.claimed_base_amount, Decimal("0.003"))

    def test_config_defaults_claimed_base_amount_to_none(self):
        """When not specified, claimed_base_amount should be None."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        config = RangeInventoryLadderConfig(
            id="test_2",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=100,
            buy_prices=[321],
            buy_amounts_pct=[1],
            sell_prices=[350],
            sell_amounts_pct=[1],
        )
        self.assertIsNone(config.claimed_base_amount)

    def test_config_rejects_negative_claimed_base_amount(self):
        """Negative claimed_base_amount should be rejected."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        with self.assertRaises(Exception):
            RangeInventoryLadderConfig(
                id="test_3",
                controller_name="range_inventory_ladder",
                controller_type="market_making",
                connector_name="nonkyc",
                trading_pair="XMR-USDT",
                total_amount_quote=100,
                buy_prices=[321],
                buy_amounts_pct=[1],
                sell_prices=[350],
                sell_amounts_pct=[1],
                claimed_base_amount=Decimal("-1"),
            )

    def test_claimed_base_amount_zero_is_valid(self):
        """Zero is a valid value for claimed_base_amount."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        config = RangeInventoryLadderConfig(
            id="test_4",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=100,
            buy_prices=[321],
            buy_amounts_pct=[1],
            sell_prices=[350],
            sell_amounts_pct=[1],
            claimed_base_amount=Decimal("0"),
        )
        self.assertEqual(config.claimed_base_amount, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
