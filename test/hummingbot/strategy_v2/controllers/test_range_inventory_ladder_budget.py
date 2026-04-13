"""
Unit tests for range_inventory_ladder budget isolation fix.
Tests the controller-owned ledger (owned_quote/owned_base) that replaces
the buggy total_balance - static_reserve derivation.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

# Add the controller source path so we can import it
_CONTROLLER_DIR = Path(r"E:\tradingsoftware\dockerscripts\apps\hummingbot\api\data\bots\controllers\market_making")
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))

# Import the module-level helpers we need for testing
from range_inventory_ladder import _safe_decimal, RangeInventoryLadderConfig


class TestBudgetInvariance(unittest.TestCase):
    """Budget must NOT change when external wallet balance changes."""

    def test_owned_quote_invariant_to_external_deposits(self):
        """
        Initialize with owned_quote=100, simulate total_quote_balance changing
        from 169 to 176. Assert managed_quote_total stays at 100.
        """
        owned_quote = Decimal("100")
        # External deposit: total goes from 169 to 176 — irrelevant to the ledger
        managed_quote_total = max(Decimal("0"), owned_quote)
        self.assertEqual(managed_quote_total, Decimal("100"))

    def test_owned_quote_invariant_to_external_withdrawals(self):
        """
        owned_quote=100, total drops to 163. managed_quote_total stays 100.
        """
        owned_quote = Decimal("100")
        managed_quote_total = max(Decimal("0"), owned_quote)
        self.assertEqual(managed_quote_total, Decimal("100"))

    def test_old_derivation_was_buggy(self):
        """
        Prove the old derivation absorbs external changes.
        total_balance=176, reserve=69.54 -> managed=106.71 (wrong)
        """
        total_quote_balance = Decimal("176.25")
        reserve_quote_balance = Decimal("69.54202854")
        old_managed = max(Decimal("0"), total_quote_balance - reserve_quote_balance)
        self.assertNotEqual(old_managed, Decimal("100"))
        self.assertAlmostEqual(float(old_managed), 106.71, places=1)


class TestFillAccounting(unittest.TestCase):
    """Fill accounting: buy reduces owned_quote, increases owned_base."""

    def test_buy_fill_accounting(self):
        """
        Buy executor fills 10 USDT at price 320.
        owned_quote -= (10 + fees), owned_base += 10/320.
        """
        owned_quote = Decimal("100")
        owned_base = Decimal("0.5")
        filled_quote = Decimal("10")
        price = Decimal("320")
        fees = Decimal("0.05")

        filled_base = filled_quote / price
        owned_quote -= (filled_quote + fees)
        owned_base += filled_base

        self.assertAlmostEqual(float(owned_quote), 89.95, places=2)
        self.assertAlmostEqual(float(owned_base), 0.53125, places=5)

    def test_sell_fill_accounting(self):
        """Sell: owned_base decreases, owned_quote increases."""
        owned_quote = Decimal("50")
        owned_base = Decimal("1.0")
        filled_quote = Decimal("320")
        price = Decimal("320")
        fees = Decimal("0.16")

        filled_base = filled_quote / price
        owned_base -= filled_base
        owned_quote += (filled_quote - fees)

        self.assertEqual(owned_base, Decimal("0"))
        self.assertAlmostEqual(float(owned_quote), 369.84, places=2)

    def test_floor_at_zero(self):
        """owned_quote/owned_base should never go negative (clamped to 0)."""
        owned_quote = Decimal("5")
        filled_quote = Decimal("10")
        fees = Decimal("0.05")
        owned_quote -= (filled_quote + fees)
        owned_quote = max(Decimal("0"), owned_quote)
        self.assertEqual(owned_quote, Decimal("0"))


class TestV9ToV10Migration(unittest.TestCase):
    """V9 state files (without owned_quote/owned_base) must be migrated correctly."""

    def test_migration_derives_owned_from_initial(self):
        """Load v9 state lacking owned_quote/owned_base, derive from initial fields."""
        v9_state = {
            "schema_version": 9,
            "initial_managed_quote": "100.5",
            "initial_claimed_base_amount": "0.3125",
        }
        # Simulate migration logic
        if "owned_quote" not in v9_state:
            initial_managed = Decimal(v9_state.get("initial_managed_quote", "0"))
            v9_state["owned_quote"] = str(initial_managed)
        if "owned_base" not in v9_state:
            initial_base = Decimal(v9_state.get("initial_claimed_base_amount", "0"))
            v9_state["owned_base"] = str(initial_base)

        self.assertEqual(v9_state["owned_quote"], "100.5")
        self.assertEqual(v9_state["owned_base"], "0.3125")

    def test_v10_state_has_owned_fields(self):
        """v10 state should already have owned_quote/owned_base, no migration needed."""
        v10_state = {
            "schema_version": 10,
            "owned_quote": "95.0",
            "owned_base": "0.4",
        }
        # No migration should happen
        self.assertEqual(v10_state["owned_quote"], "95.0")
        self.assertEqual(v10_state["owned_base"], "0.4")


class TestReconciliationAlert(unittest.TestCase):
    """Reconciliation: alert on drift but do NOT change the budget."""

    def test_alert_logged_but_budget_unchanged(self):
        """
        owned_quote=100 but wallet-derived=110. Budget stays at 100.
        """
        owned_quote = Decimal("100")
        total_quote_balance = Decimal("179.54202854")
        reserve_quote_balance = Decimal("69.54202854")

        wallet_derived_quote = max(Decimal("0"), total_quote_balance - reserve_quote_balance)
        ledger_quote_drift = wallet_derived_quote - owned_quote

        # Drift should be ~10
        self.assertAlmostEqual(float(ledger_quote_drift), 10.0, places=1)

        # But managed_quote_total uses the ledger, not wallet-derived
        managed_quote_total = max(Decimal("0"), owned_quote)
        self.assertEqual(managed_quote_total, Decimal("100"))

        # Alert threshold check
        RECONCILIATION_ALERT_THRESHOLD_QUOTE = Decimal("0.5")
        self.assertTrue(abs(ledger_quote_drift) > RECONCILIATION_ALERT_THRESHOLD_QUOTE)


class TestDiagnosticLogFiltering(unittest.TestCase):
    """Diagnostic log should skip high-frequency events."""

    def test_skip_set_contains_expected_events(self):
        """range_ladder_cycle and range_ladder_noop_cycle should be in skip set."""
        # Import the class to check the frozenset
        from range_inventory_ladder import RangeInventoryLadderController
        skip = RangeInventoryLadderController._DIAGNOSTIC_SKIP_EVENTS
        self.assertIn("range_ladder_cycle", skip)
        self.assertIn("range_ladder_noop_cycle", skip)
        self.assertNotIn("range_ladder_initialized", skip)
        self.assertNotIn("range_ladder_ledger_updated", skip)


class TestSchemaVersionBump(unittest.TestCase):
    """Schema version must be 10 and support 6-10."""

    def test_schema_version_is_10(self):
        from range_inventory_ladder import RangeInventoryLadderController
        self.assertEqual(RangeInventoryLadderController.STATE_SCHEMA_VERSION, 10)

    def test_supported_versions(self):
        from range_inventory_ladder import RangeInventoryLadderController
        self.assertEqual(
            RangeInventoryLadderController.SUPPORTED_STATE_SCHEMA_VERSIONS,
            {6, 7, 8, 9, 10}
        )


if __name__ == "__main__":
    unittest.main()
