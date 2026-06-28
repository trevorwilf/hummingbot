"""Behavior-contract tests for ledger-funded budgets (ledger_funded_budgets, default True).

New buy/sell budgets are funded from THIS controller's own managed-fund ledger
(owned_quote/owned_base minus its own resting reservations), bounded by the live wallet as a
safety floor, instead of from the raw shared wallet balance. This isolates multiple controllers
sharing one exchange account without `shared_account_quote_quota`, lets a controller redeploy its
own proceeds with no quota change, and preserves deposit-exclusion (a deposit raises only the
floor, never owned). With ledger_funded_budgets=False the legacy raw-wallet path is byte-for-byte.

Every test drives the REAL controller; balances are mocked so wallet and ledger can diverge.
Assertions target real budget/order outcomes, not tautologies.
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

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
    """balances: {asset: (total, available)}. Mutate between cycles to simulate fills/deposits."""
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


def _filling(level_id, side, price, eid, *, filled_base, filled_quote, fees="0"):
    """A TERMINATED executor reporting a cumulative fill via custom_info (a v13 booking source)."""
    ex = MagicMock()
    ex.id = eid
    ex.status = RunnableStatus.TERMINATED
    ex.is_active = False
    ex.timestamp = 0.0
    ex.close_timestamp = 1000.0
    ex.connector_name = "nonkyc"
    ex.custom_info = {
        "filled_amount_base": D(filled_base),
        "filled_amount_quote": D(filled_quote),
        "cum_fees_quote": D(fees),
    }
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = D(price)
    cfg.amount = D("1")
    ex.config = cfg
    return ex


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def _build(self, mdp, *, cid="ctrl-lfb", pair="XMR-USDT", **config_overrides):
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
            ledger_overclaim_reanchor_seconds=999_999,  # re-anchor is out of scope here
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

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value,
                    reserve_quote="0", reserve_base="0", pair="XMR-USDT"):
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
            "reserve_quote_balance": str(reserve_quote),
            "reserve_base_balance": str(reserve_base),
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

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]


# =================================================== 1: dedicated-account parity

class TestDedicatedParity(_Harness):

    def test_owned_equals_wallet_matches_legacy(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        ledger = self._build(mdp, ledger_funded_budgets=True)
        legacy = self._build(mdp, ledger_funded_budgets=False)
        for ceiling in ("100000", "400"):  # unthrottled AND throttled
            ledger_out = self._budgets(ledger, ref="300", avail_quote="200", avail_base="1",
                                       ceiling=ceiling, owned_quote="200", owned_base="1")
            legacy_out = self._budgets(legacy, ref="300", avail_quote="200", avail_base="1",
                                       ceiling=ceiling)
            self.assertEqual(ledger_out, legacy_out, f"parity must hold (ceiling={ceiling})")


# =================================================== 2: self-funded redeploy (headline)

class TestSelfFundedRedeploy(_Harness):

    def test_sell_fill_raises_owned_quote_and_buy_budget_no_quota_change(self):
        # owned_quote low; a SELL fill raises owned_quote; next cycle the buy budget rises by
        # ~the proceeds WITHOUT any shared_account_quote_quota change.
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(20), D(20))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self.assertIsNone(ctrl.config.shared_account_quote_quota)  # no quota in play
        self._init_state(ctrl, owned_quote=20, owned_base="0.5", seed_value=200)

        asyncio.run(ctrl.update_processed_data())
        buy_before = ctrl.processed_data["free_buy_budget_quote"]
        self.assertEqual(buy_before, D(20))  # funded from owned_quote 20 (== wallet)

        # A sell fills: 0.3 base -> 105 quote (fee 0.2). Wallet quote rises; base falls.
        ctrl.executors_info = [_filling("sell_350", TradeType.SELL, "350", "s0",
                                        filled_base="0.3", filled_quote="105", fees="0.2")]
        balances["USDT"] = (D(125), D(125))
        balances["XMR"] = (D("0.2"), D("0.2"))
        asyncio.run(ctrl.update_processed_data())

        self.assertTrue(ctrl._booked_sell_fill_this_cycle)
        owned_quote_after = D(ctrl._state["owned_quote"])
        self.assertGreater(owned_quote_after, D(120))     # owned_quote grew by ~proceeds
        buy_after = ctrl.processed_data["free_buy_budget_quote"]
        self.assertGreater(buy_after, D(120))             # buy budget redeployed the proceeds
        self.assertAlmostEqual(float(buy_after - buy_before), 104.8, delta=1.0)
        self.assertIsNone(ctrl.config.shared_account_quote_quota)  # unchanged


# =================================================== 3-5,7,8: direct budget-source semantics

class TestBudgetSource(_Harness):

    def _ctrl(self, **overrides):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        return self._build(mdp, **overrides)

    def test_shared_isolation_quote_zero_owned_places_nothing(self):
        # Wallet shows $500 (another controller's cash); this controller owns $0 -> buy budget 0.
        ctrl = self._ctrl()
        buy, sell, _, _ = self._budgets(ctrl, avail_quote="500", owned_quote="0", owned_base="0")
        self.assertEqual(buy, D(0))

    def test_shared_isolation_quote_funded(self):
        ctrl = self._ctrl()
        buy, sell, _, _ = self._budgets(ctrl, avail_quote="500", owned_quote="500", owned_base="0")
        self.assertEqual(buy, D(500))

    def test_reservation_subtraction_reduces_buy_budget(self):
        ctrl = self._ctrl()
        no_resv = self._budgets(ctrl, avail_quote="500", owned_quote="300", owned_base="0",
                                reserved_quote="0")[0]
        with_resv = self._budgets(ctrl, avail_quote="500", owned_quote="300", owned_base="0",
                                  reserved_quote="120")[0]
        self.assertEqual(no_resv, D(300))
        self.assertEqual(with_resv, D(180))  # owned_quote_free = 300 - 120
        self.assertLess(with_resv, no_resv)

    def test_sell_side_symmetric_from_owned_base_free(self):
        ctrl = self._ctrl()
        # owned_base 2, reserved 0.5 -> free 1.5; wallet base 1.0 floors it to 1.0.
        _, sell, _, _ = self._budgets(ctrl, ref="300", avail_base="1.0", owned_quote="0",
                                      owned_base="2", reserved_base="0.5")
        self.assertEqual(sell, D("1.0"))   # min(1.5 owned_free, 1.0 wallet)
        # raise the wallet so owned_base_free binds instead
        _, sell2, _, _ = self._budgets(ctrl, ref="300", avail_base="5", owned_quote="0",
                                       owned_base="2", reserved_base="0.5")
        self.assertEqual(sell2, D("1.5"))  # min(1.5 owned_free, 5 wallet)

    def test_quota_still_caps_in_ledger_mode(self):
        ctrl = self._ctrl(shared_account_quote_quota=Decimal("30"))
        # owned_free 300, wallet 500, quota 30 -> budget == quota.
        buy, _, _, _ = self._budgets(ctrl, avail_quote="500", owned_quote="300", owned_base="0")
        self.assertEqual(buy, D(30))


# =================================================== 6: wallet floor binds + single warning

class TestWalletFloor(_Harness):

    def test_floor_binds_clamps_and_warns_once(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        # owned_quote_free 300 > available 50 -> clamp to 50 and warn.
        buy, _, _, _ = self._budgets(ctrl, avail_quote="50", owned_quote="300", owned_base="0")
        self.assertEqual(buy, D(50))
        warns = self._events(ctrl, "range_ladder_wallet_floor_binding")
        self.assertEqual(1, len(warns))
        self.assertEqual(warns[0].kwargs["side"], "buy")
        self.assertEqual(warns[0].kwargs["owned_free"], "300")
        self.assertEqual(warns[0].kwargs["available"], "50")
        self.assertEqual(warns[0].kwargs["clamped_budget"], "50")

    def test_floor_warning_is_not_spammed(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        for _ in range(5):  # floor binds every call, but warn only on the transition into binding
            self._budgets(ctrl, avail_quote="50", owned_quote="300", owned_base="0")
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_wallet_floor_binding")))
        # clears, then re-binds -> warns again (a new episode).
        self._budgets(ctrl, avail_quote="500", owned_quote="300", owned_base="0")  # not binding
        self._budgets(ctrl, avail_quote="50", owned_quote="300", owned_base="0")   # binds again
        self.assertEqual(2, len(self._events(ctrl, "range_ladder_wallet_floor_binding")))

    def test_no_warning_when_floor_not_binding(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._budgets(ctrl, avail_quote="500", owned_quote="300", owned_base="0")  # owned < wallet
        self.assertEqual([], self._events(ctrl, "range_ladder_wallet_floor_binding"))


# =================================================== 9: legacy toggle parity

class TestLegacyToggle(_Harness):

    def test_legacy_reproduces_wallet_funded_behavior(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        legacy = self._build(mdp, ledger_funded_budgets=False)
        # owned is IGNORED in legacy mode: budget tracks the raw wallet (quota-capped, ceiling-throttled).
        buy, sell, _, _ = self._budgets(legacy, ref="300", avail_quote="500", avail_base="2",
                                        owned_quote="10", owned_base="0", ceiling="100000")
        self.assertEqual(buy, D(500))    # raw wallet, NOT owned 10
        self.assertEqual(sell, D(2))

    def test_legacy_quota_cap_unchanged(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=300, bid=299, ask=301)
        legacy = self._build(mdp, ledger_funded_budgets=False, shared_account_quote_quota=Decimal("30"))
        buy, _, _, _ = self._budgets(legacy, avail_quote="100", owned_quote="10", owned_base="0")
        self.assertEqual(buy, D(30))


# =================================================== 10: deposit exclusion (point 4)

class TestDepositExclusion(_Harness):

    def test_deposit_raises_floor_not_owned_or_budget(self):
        # owned < wallet (floor NOT binding) so budget == owned. A deposit raises available only;
        # owned, managed-fund value, and the budget are all unchanged.
        balances = {"XMR": (D("0.5"), D("0.5")), "USDT": (D(500), D(500))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=100, owned_base="0.5", seed_value=300)

        asyncio.run(ctrl.update_processed_data())
        buy0 = ctrl.processed_data["free_buy_budget_quote"]
        fund0 = ctrl.processed_data["managed_fund_value_quote"]
        owned0 = ctrl._state["owned_quote"]
        self.assertEqual(buy0, D(100))  # funded from owned 100, NOT wallet 500

        balances["USDT"] = (D(5500), D(5500))  # +5000 deposit, no fill
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual(ctrl._state["owned_quote"], owned0)                          # owned unchanged
        self.assertEqual(ctrl.processed_data["managed_fund_value_quote"], fund0)      # fund unchanged
        self.assertEqual(ctrl.processed_data["free_buy_budget_quote"], buy0)          # budget unchanged


# =================================================== 11: two-controller integration

class TestTwoControllerIntegration(_Harness):

    def test_shared_wallet_two_controllers_isolated(self):
        # One shared account: USDT 500 total/available is visible to BOTH controllers. The XMR
        # controller owns $0 quote; the DASH controller owns $500. Each funds only from its own
        # ledger; the sum of new buy budgets never exceeds the physical wallet.
        shared_usdt = [D(500), D(500)]  # mutable [total, available]
        balances = {
            "USDT": shared_usdt,
            "XMR": [D(0), D(0)],
            "DASH": [D(0), D(0)],
        }
        mdp = MagicMock()
        mdp.time.return_value = 1000.0
        mdp.get_price_by_type.side_effect = lambda c, p, pt: {
            PriceType.MidPrice: D(50), PriceType.BestBid: D(49), PriceType.BestAsk: D(51)}[pt]
        mdp.get_balance.side_effect = lambda c, a: balances[a][0]
        mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
        mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
        mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)
        conn = MagicMock(); conn.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
        conn.in_flight_orders = {}
        mdp.get_connector.return_value = conn

        xmr = self._build(mdp, cid="ctrl-xmr", pair="XMR-USDT",
                          buy_prices=[Decimal("48"), Decimal("45")], buy_amounts_pct=[Decimal("1"), Decimal("1")],
                          sell_prices=[Decimal("55"), Decimal("60")], sell_amounts_pct=[Decimal("1"), Decimal("1")])
        dash = self._build(mdp, cid="ctrl-dash", pair="DASH-USDT",
                           buy_prices=[Decimal("48"), Decimal("45")], buy_amounts_pct=[Decimal("1"), Decimal("1")],
                           sell_prices=[Decimal("55"), Decimal("60")], sell_amounts_pct=[Decimal("1"), Decimal("1")])
        self._init_state(xmr, owned_quote=0, owned_base=0, seed_value=500, pair="XMR-USDT")
        self._init_state(dash, owned_quote=500, owned_base=0, seed_value=500, pair="DASH-USDT")

        asyncio.run(xmr.update_processed_data())
        asyncio.run(dash.update_processed_data())
        xmr_buy = xmr.processed_data["free_buy_budget_quote"]
        dash_buy = dash.processed_data["free_buy_budget_quote"]

        self.assertEqual(xmr_buy, D(0))      # owns no quote -> deploys nothing despite the $500 wallet
        self.assertEqual(dash_buy, D(500))   # deploys its own $500
        self.assertLessEqual(xmr_buy + dash_buy, shared_usdt[1])  # sum within the physical wallet


# =================================================== 12 & 13: regression / config

class TestLedgerFundedRegression(_Harness):

    def test_state_schema_version_unchanged(self):
        self.assertEqual(RangeInventoryLadderController.STATE_SCHEMA_VERSION, 10)
        self.assertEqual(RangeInventoryLadderController.SUPPORTED_STATE_SCHEMA_VERSIONS, {6, 7, 8, 9, 10})

    def test_config_field_default_true_and_updatable(self):
        cfg = RangeInventoryLadderConfig(
            id="t", controller_name="range_inventory_ladder", controller_type="market_making",
            connector_name="nonkyc", trading_pair="XMR-USDT", total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")])
        self.assertIs(cfg.ledger_funded_budgets, True)
        extra = RangeInventoryLadderConfig.model_fields["ledger_funded_budgets"].json_schema_extra or {}
        self.assertTrue(extra.get("is_updatable", False))

    def test_config_field_accepts_bool_false(self):
        cfg = RangeInventoryLadderConfig(
            id="t", controller_name="range_inventory_ladder", controller_type="market_making",
            connector_name="nonkyc", trading_pair="XMR-USDT", total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")],
            ledger_funded_budgets=False)
        self.assertIs(cfg.ledger_funded_budgets, False)


if __name__ == "__main__":
    unittest.main()
