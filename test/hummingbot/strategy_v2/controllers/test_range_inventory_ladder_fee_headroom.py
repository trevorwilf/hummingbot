"""Phase 1 hardening: buy-side fee headroom.

NonKYC holds notional + fee as collateral for a resting buy order (the connector's
_place_order_and_process_update computes hold_amount = notional + fee). Deploying 100% of
free_buy_budget_quote therefore over-commits the wallet by the sum of fees and the last
rung(s) reject with insufficient funds. _compute_deploy_budgets now divides the buy budget
by (1 + fee_rate) AFTER the ledger/wallet clamp and quota cap and BEFORE the deploy-ceiling
throttle, in BOTH funding modes. The sell side is never haircut (sell fees are deducted from
proceeds, not held as extra collateral).

Every test drives the REAL controller with mocked balances, mirroring the harness style of
test_range_inventory_ladder_ledger_funded_budgets.py.
"""
import asyncio
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from hummingbot.core.data_type.common import OrderType, PriceType  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731

ONE_PLUS_FEE = D("1") + D("0.002")


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
    """balances: {asset: (total, available)}."""
    mdp = MagicMock()
    mdp.time.return_value = now
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)
    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def _build(self, mdp, *, cid="ctrl-fee", pair="XMR-USDT", **config_overrides):
        defaults = dict(
            id=cid,
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair=pair,
            total_amount_quote=Decimal("200"),
            max_fund_value_quote=Decimal("5000"),
            buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
            buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
            sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            min_order_quote=Decimal("5"),
            ledger_overclaim_reanchor_seconds=999_999,
        )
        defaults.update(config_overrides)
        config = RangeInventoryLadderConfig(**defaults)
        controller = RangeInventoryLadderController(config, market_data_provider=mdp, actions_queue=MagicMock())
        controller._emit_structured = MagicMock()
        self._n += 1
        state_path = Path(self._tmp.name) / f"state_{self._n}.json"
        patcher = patch.object(type(controller), "state_path", new_callable=PropertyMock, return_value=state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        controller.executors_info = []
        controller.positions_held = []
        return controller

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value, pair="XMR-USDT"):
        base_asset, quote_asset = pair.split("-")
        ctrl._state = {
            "initialized": True,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "owned_quote": str(owned_quote),
            "owned_base": str(owned_base),
            "seed_value_quote": str(seed_value),
            "initial_managed_quote": str(owned_quote),
            "initial_claimed_base_amount": str(owned_base),
            "initial_reference_price": "335",
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True

    def _budgets(self, ctrl, *, ref="300", avail_quote="0", avail_base="0",
                 reserved_quote="0", reserved_base="0", ceiling="100000",
                 owned_quote=None, owned_base=None):
        kwargs = dict(
            reference_price=D(ref),
            available_quote=D(avail_quote),
            available_base=D(avail_base),
            active_buy_reserved_quote=D(reserved_quote),
            active_sell_reserved_base=D(reserved_base),
            deploy_ceiling=D(ceiling),
        )
        if owned_quote is not None:
            kwargs["owned_quote"] = D(owned_quote)
        if owned_base is not None:
            kwargs["owned_base"] = D(owned_base)
        return ctrl._compute_deploy_budgets(**kwargs)


# =================================================== haircut math on the budget function

class TestFeeHaircutMath(_Harness):

    def _ctrl(self, **overrides):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        return self._build(mdp, **overrides)

    def test_ledger_mode_buy_budget_divided_by_one_plus_fee(self):
        ctrl = self._ctrl(fee_rate=Decimal("0.002"))
        buy, _, _, _ = self._budgets(ctrl, avail_quote="500", owned_quote="500", owned_base="0")
        self.assertEqual(buy, D(500) / ONE_PLUS_FEE)
        self.assertEqual(ctrl._last_buy_fee_headroom_quote, D(500) - D(500) / ONE_PLUS_FEE)

    def test_legacy_mode_also_haircuts(self):
        ctrl = self._ctrl(fee_rate=Decimal("0.002"), ledger_funded_budgets=False)
        buy, _, _, _ = self._budgets(ctrl, avail_quote="500")
        self.assertEqual(buy, D(500) / ONE_PLUS_FEE)

    def test_zero_fee_rate_is_a_noop(self):
        ctrl = self._ctrl(fee_rate=Decimal("0"))
        buy, _, _, _ = self._budgets(ctrl, avail_quote="500", owned_quote="500", owned_base="0")
        self.assertEqual(buy, D(500))
        self.assertEqual(ctrl._last_buy_fee_headroom_quote, D(0))

    def test_sell_side_is_never_haircut(self):
        ctrl = self._ctrl(fee_rate=Decimal("0.002"))
        _, sell, _, _ = self._budgets(ctrl, ref="300", avail_base="2",
                                      owned_quote="0", owned_base="2")
        self.assertEqual(sell, D(2))  # untouched by fee_rate

    def test_haircut_applied_after_quota_cap(self):
        # quota 30 caps first, THEN the haircut applies -> 30/(1.002), not min(500/1.002, 30).
        ctrl = self._ctrl(fee_rate=Decimal("0.002"), shared_account_quote_quota=Decimal("30"))
        buy, _, _, _ = self._budgets(ctrl, avail_quote="500", owned_quote="500", owned_base="0")
        self.assertEqual(buy, D(30) / ONE_PLUS_FEE)

    def test_haircut_applied_before_ceiling_throttle(self):
        # Ceiling 100: desired = 500/1.002 (post-haircut) -> throttle scales THAT down to 100.
        # If the haircut ran after the throttle, the result would be 100/1.002 instead.
        ctrl = self._ctrl(fee_rate=Decimal("0.002"))
        buy, _, scale, headroom = self._budgets(ctrl, avail_quote="500", owned_quote="500",
                                                owned_base="0", ceiling="100")
        self.assertEqual(headroom, D(100))
        self.assertEqual(buy, D(100))  # full headroom deployed, haircut already inside
        self.assertLess(scale, D(1))


# =================================================== full-cycle behavior

class TestFeeHeadroomEndToEnd(_Harness):

    def _cycled(self, *, fee_rate="0.002", wallet_quote="200", **overrides):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(wallet_quote), D(wallet_quote))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, fee_rate=Decimal(fee_rate), **overrides)
        self._init_state(ctrl, owned_quote=wallet_quote, owned_base="0", seed_value=wallet_quote)
        asyncio.run(ctrl.update_processed_data())
        return ctrl

    def test_total_buy_notional_within_haircut_budget(self):
        ctrl = self._cycled()
        ctrl._buy_side_dirty = True
        ctrl._defer_buy_creates_this_cycle = False
        actions = ctrl._create_buy_actions()
        self.assertGreater(len(actions), 0)
        total_notional = sum(a.executor_config.amount * a.executor_config.price for a in actions)
        budget_cap = D(200) / ONE_PLUS_FEE
        self.assertLessEqual(total_notional, budget_cap)
        # the haircut must not strand the budget: the book still deploys nearly all of it
        self.assertGreater(total_notional, budget_cap * D("0.95"))
        # total notional + fees fits inside the raw wallet balance (the actual exchange hold)
        self.assertLessEqual(total_notional * ONE_PLUS_FEE, D(200))

    def test_headroom_surfaces_in_processed_data_and_custom_info(self):
        ctrl = self._cycled()
        expected = D(200) - D(200) / ONE_PLUS_FEE
        self.assertEqual(ctrl.processed_data["buy_fee_headroom_quote"], expected)
        self.assertEqual(ctrl.processed_data["free_buy_budget_quote"], D(200) / ONE_PLUS_FEE)
        info = ctrl.get_custom_info()
        self.assertEqual(info["buy_fee_headroom_quote"], str(expected))

    def test_headroom_in_diagnostic_heartbeat_payload(self):
        ctrl = self._cycled(diagnostic_log_enabled=True)
        beats = [c for c in ctrl._emit_structured.call_args_list
                 if c.args and c.args[0] == "range_ladder_diagnostic_heartbeat"]
        self.assertGreaterEqual(len(beats), 1)
        expected = D(200) - D(200) / ONE_PLUS_FEE
        self.assertEqual(beats[-1].kwargs["buy_fee_headroom_quote"], expected)

    def test_planner_create_path_parity_with_haircut(self):
        ctrl = self._cycled()
        ctrl._buy_side_dirty = True
        ctrl._defer_buy_creates_this_cycle = False
        planned = ctrl._plan_buy_book()
        created = {a.executor_config.level_id: a.executor_config.amount
                   for a in ctrl._create_buy_actions()}
        self.assertTrue(planned)
        self.assertEqual(planned, created)

    def test_legacy_mode_full_cycle_haircuts(self):
        ctrl = self._cycled(ledger_funded_budgets=False)
        self.assertEqual(ctrl.processed_data["free_buy_budget_quote"], D(200) / ONE_PLUS_FEE)
        expected = D(200) - D(200) / ONE_PLUS_FEE
        self.assertEqual(ctrl.processed_data["buy_fee_headroom_quote"], expected)


if __name__ == "__main__":
    unittest.main()
