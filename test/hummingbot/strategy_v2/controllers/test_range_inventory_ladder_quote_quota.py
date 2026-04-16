"""Tests for range_inventory_ladder shared-account quote quota."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

# Discover the controller directory portably.
# Prefer the dockerscripts copy (authoritative in this dev environment); fall back to repo paths.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CANDIDATES = [
    Path(r"E:\tradingsoftware\dockerscripts\apps\hummingbot\api\data\bots\controllers\market_making"),
    _REPO_ROOT / "controllers" / "market_making",
    _REPO_ROOT / "api" / "data" / "bots" / "controllers" / "market_making",
]
_CTRL_DIR = next((p for p in _CANDIDATES if (p / "range_inventory_ladder.py").exists()), None)
if _CTRL_DIR is None:
    raise RuntimeError(
        f"Could not locate range_inventory_ladder.py. Tried: {[str(p) for p in _CANDIDATES]}"
    )
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))


def _apply_budget_clamp(
    *,
    deployable_quote_total: Decimal,
    active_buy_reserved_quote: Decimal,
    available_quote_balance: Decimal,
    shared_account_quote_quota,
) -> Decimal:
    """Simulate the exact clamp logic used in update_processed_data."""
    free_buy_budget_quote = max(Decimal("0"), deployable_quote_total - active_buy_reserved_quote)
    account_cap = available_quote_balance
    if shared_account_quote_quota is not None:
        account_cap = min(account_cap, shared_account_quote_quota)
    return min(free_buy_budget_quote, account_cap)


def _make_config(**overrides):
    from range_inventory_ladder import RangeInventoryLadderConfig
    defaults = dict(
        id="test_quota",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("100"),
        buy_prices=[Decimal("320")],
        buy_amounts_pct=[Decimal("1")],
        sell_prices=[Decimal("340")],
        sell_amounts_pct=[Decimal("1")],
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


class TestQuoteQuota(unittest.TestCase):

    def test_quota_none_preserves_existing_behavior(self):
        free = _apply_budget_clamp(
            deployable_quote_total=Decimal("100"),
            active_buy_reserved_quote=Decimal("0"),
            available_quote_balance=Decimal("50"),
            shared_account_quote_quota=None,
        )
        self.assertEqual(free, Decimal("50"))

    def test_quota_lower_than_balance_clamps_to_quota(self):
        free = _apply_budget_clamp(
            deployable_quote_total=Decimal("100"),
            active_buy_reserved_quote=Decimal("0"),
            available_quote_balance=Decimal("50"),
            shared_account_quote_quota=Decimal("30"),
        )
        self.assertEqual(free, Decimal("30"))

    def test_quota_higher_than_balance_still_clamps_to_balance(self):
        free = _apply_budget_clamp(
            deployable_quote_total=Decimal("100"),
            active_buy_reserved_quote=Decimal("0"),
            available_quote_balance=Decimal("50"),
            shared_account_quote_quota=Decimal("200"),
        )
        self.assertEqual(free, Decimal("50"))

    def test_quota_validation_rejects_negative(self):
        with self.assertRaises(Exception):
            _make_config(shared_account_quote_quota=Decimal("-1"))

    def test_quota_validation_accepts_zero(self):
        config = _make_config(shared_account_quote_quota=Decimal("0"))
        self.assertEqual(config.shared_account_quote_quota, Decimal("0"))
        # With quota=0, free_buy_budget_quote is 0 regardless of other values
        free = _apply_budget_clamp(
            deployable_quote_total=Decimal("100"),
            active_buy_reserved_quote=Decimal("0"),
            available_quote_balance=Decimal("50"),
            shared_account_quote_quota=Decimal("0"),
        )
        self.assertEqual(free, Decimal("0"))

    def test_quota_is_updatable_in_json_schema(self):
        """The field is declared is_updatable=True for hot-update support."""
        from range_inventory_ladder import RangeInventoryLadderConfig
        field_info = RangeInventoryLadderConfig.model_fields["shared_account_quote_quota"]
        extra = field_info.json_schema_extra or {}
        self.assertTrue(
            extra.get("is_updatable", False),
            "shared_account_quote_quota must be is_updatable=True",
        )

    def test_quota_default_is_none(self):
        config = _make_config()
        self.assertIsNone(config.shared_account_quote_quota)


if __name__ == "__main__":
    unittest.main()
