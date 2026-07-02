"""
Behavior-contract tests for the range_inventory_ladder self-balance hardening.

These drive the REAL controller (not inline arithmetic):

  - Seed claim is a SUBSET of total_amount_quote (base sleeve carved out, not added).
  - Deployment sizes from the LIVE available wallet each cycle, bounded by a growth
    ceiling that starts at the seed value and compounds with the fills-only managed
    fund value, hard-capped at max_fund_value_quote.
  - No fixed base/quote ratio after seed; each side deploys what it holds.
  - Compression concentrates into the nearest fundable levels (never zero when one
    level is fundable).
  - A one-sided wallet runs a one-sided ladder (no conversion/rebalance orders).
  - The fills-only PnL ledger (owned_quote/owned_base) stays invariant to deposits.
  - Unfunded-side startup diagnostics fire.

Every assertion targets a behavior that fails if the corresponding change is reverted.
"""
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
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _bind_exchange_min_gate(controller, cls=RangeInventoryLadderController):
    """Bind the real v15 exchange-minimum feasibility chain onto a MagicMock controller.
    The MagicMock provider exposes no dict trading_rules, so exchange minimums resolve to 0
    and the gate reduces to the original amount>0 + min_order_quote checks."""
    controller._d = cls._d
    controller._rule_decimal = cls._rule_decimal
    for name in ("_exchange_trading_rule", "_exchange_min_order_size",
                 "_exchange_min_notional", "_level_quantization_failure"):
        setattr(controller, name, getattr(cls, name).__get__(controller, cls))


def _make_mdp(*, balances, mid, bid, ask, now=1_000_000.0):
    """balances: {asset: (total, available)}."""
    mdp = MagicMock()
    mdp.time.return_value = now

    def price_by_type(conn, pair, pt):
        return {PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]

    mdp.get_price_by_type.side_effect = price_by_type
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)

    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


def _make_config(**overrides):
    defaults = dict(
        id="ctrl-sb",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("170"),
        max_fund_value_quote=Decimal("1000"),
        buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
        buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
        sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        min_order_quote=Decimal("5"),
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


class _ControllerHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        config = _make_config(**config_overrides)
        controller = RangeInventoryLadderController(
            config, market_data_provider=mdp, actions_queue=MagicMock()
        )
        controller._emit_structured = MagicMock()
        patcher = patch.object(
            type(controller), "state_path", new_callable=PropertyMock,
            return_value=self._state_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return controller

    @staticmethod
    def _emit_events(controller, event_type):
        return [
            call for call in controller._emit_structured.call_args_list
            if call.args and call.args[0] == event_type
        ]


# ===================================================================== seed

class TestSeedSubset(_ControllerHarness):

    def test_seed_split_is_subset_not_additive(self):
        """total=170, claimed_base_value_quote=40 -> owned_base~40-worth, owned_quote~130,
        total seed ~170 (NOT 210)."""
        mdp = _make_mdp(balances={"XMR": (D(10), D(10)), "USDT": (D(1000), D(1000))},
                        mid=320, bid=319, ask=321)
        ctrl = self._build(mdp, use_wallet_balance=True, claimed_base_value_quote=Decimal("40"))
        self.assertTrue(ctrl._ensure_initialized(D(320)))

        owned_quote = D(ctrl._state["owned_quote"])
        owned_base = D(ctrl._state["owned_base"])
        seed_value = D(ctrl._state["seed_value_quote"])

        self.assertEqual(owned_base * D(320), D(40))      # 40-worth of base
        self.assertEqual(owned_quote, D(130))             # 170 - 40
        self.assertEqual(seed_value, D(170))              # subset total, not 210
        self.assertLessEqual(seed_value, D(170))

    def test_seed_only_base_wallet(self):
        """All-XMR wallet -> claims <=40-worth base, ~0 quote, total seed <=170."""
        mdp = _make_mdp(balances={"XMR": (D(10), D(10)), "USDT": (D(0), D(0))},
                        mid=320, bid=319, ask=321)
        ctrl = self._build(mdp, use_wallet_balance=True, claimed_base_value_quote=Decimal("40"))
        self.assertTrue(ctrl._ensure_initialized(D(320)))

        self.assertEqual(D(ctrl._state["owned_quote"]), D(0))
        self.assertEqual(D(ctrl._state["owned_base"]) * D(320), D(40))
        self.assertLessEqual(D(ctrl._state["seed_value_quote"]), D(170))

    def test_seed_only_quote_wallet(self):
        """All-USDT wallet -> ~0 base, quote up to 170."""
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))},
                        mid=320, bid=319, ask=321)
        ctrl = self._build(mdp, use_wallet_balance=True, claimed_base_value_quote=Decimal("40"))
        self.assertTrue(ctrl._ensure_initialized(D(320)))

        self.assertEqual(D(ctrl._state["owned_base"]), D(0))
        self.assertEqual(D(ctrl._state["owned_quote"]), D(170))
        self.assertEqual(D(ctrl._state["seed_value_quote"]), D(170))

    def test_unfunded_side_event_fires_for_buy_on_all_base_wallet(self):
        """All-XMR wallet -> buy side unfunded-at-init event fires (and not for sell)."""
        mdp = _make_mdp(balances={"XMR": (D(10), D(10)), "USDT": (D(0), D(0))},
                        mid=320, bid=319, ask=321)
        ctrl = self._build(mdp, use_wallet_balance=True, claimed_base_value_quote=Decimal("40"))
        ctrl._ensure_initialized(D(320))

        events = self._emit_events(ctrl, "range_ladder_side_unfunded_at_init")
        sides = {c.kwargs.get("side") for c in events}
        self.assertIn("buy", sides)
        self.assertNotIn("sell", sides)

    def test_unfunded_side_event_fires_for_sell_on_all_quote_wallet(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))},
                        mid=320, bid=319, ask=321)
        ctrl = self._build(mdp, use_wallet_balance=True, claimed_base_value_quote=Decimal("40"))
        ctrl._ensure_initialized(D(320))

        events = self._emit_events(ctrl, "range_ladder_side_unfunded_at_init")
        sides = {c.kwargs.get("side") for c in events}
        self.assertIn("sell", sides)
        self.assertNotIn("buy", sides)


# ============================================================= deploy budget

class TestDeployCeilingAndBudgets(_ControllerHarness):

    def _ctrl(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        return self._build(mdp)

    def test_ceiling_starts_at_seed_and_compounds_capped(self):
        ctrl = self._ctrl()
        # seed below managed -> ceiling rises with managed
        self.assertEqual(ctrl._compute_deploy_ceiling(D(170), D(170)), D(170))
        self.assertEqual(ctrl._compute_deploy_ceiling(D(170), D(500)), D(500))
        # never below the seed value even if managed dipped
        self.assertEqual(ctrl._compute_deploy_ceiling(D(170), D(100)), D(170))
        # hard cap at max_fund_value_quote (1000)
        self.assertEqual(ctrl._compute_deploy_ceiling(D(170), D(2000)), D(1000))

    def test_budget_grows_with_available_balance_no_throttle(self):
        ctrl = self._ctrl()
        small = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D(20), available_base=D("0.1"),
            active_buy_reserved_quote=D(0), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )
        bigger = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D(60), available_base=D("0.1"),
            active_buy_reserved_quote=D(0), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )
        # free_buy is element 0; deposit picked up next cycle with no throttle.
        self.assertEqual(small[0], D(20))
        self.assertEqual(bigger[0], D(60))
        self.assertEqual(small[2], D(1))   # throttle scale == 1 (no throttle)

    def test_combined_deployment_capped_at_ceiling(self):
        ctrl = self._ctrl()
        free_buy, free_sell, scale, headroom = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D(5000), available_base=D(10),
            active_buy_reserved_quote=D(0), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )
        deployed_value = free_buy + free_sell * D(300)
        self.assertLessEqual(deployed_value, D(1000))
        self.assertEqual(deployed_value, D(1000))  # fully uses, never exceeds the cap
        self.assertLess(scale, D(1))               # throttle applied

    def test_active_reservations_reduce_headroom(self):
        ctrl = self._ctrl()
        # 800 value already on the book; only 200 of ceiling headroom remains.
        free_buy, free_sell, scale, headroom = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D(5000), available_base=D(10),
            active_buy_reserved_quote=D(800), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )
        self.assertEqual(headroom, D(200))
        self.assertLessEqual(free_buy + free_sell * D(300), D(200))

    def test_quota_clamps_buy_budget(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, shared_account_quote_quota=Decimal("30"))
        free_buy, _, _, _ = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D(100), available_base=D(0),
            active_buy_reserved_quote=D(0), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )
        self.assertEqual(free_buy, D(30))


# ============================================================ ledger invariance

class TestLedgerInvariance(_ControllerHarness):

    def test_deposit_does_not_change_owned_ledger(self):
        """A deposit (available balance up) must NOT move the fills-only ledger."""
        mdp = _make_mdp(balances={"XMR": (D(5), D(5)), "USDT": (D(500), D(500))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        ctrl._state = {
            "initialized": True,
            "owned_quote": "100",
            "owned_base": "0.5",
            "seed_value_quote": "170",
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True
        ctrl.executors_info = []

        ctrl._update_ledger_from_completed_executors()  # no fills -> no change

        self.assertEqual(D(ctrl._state["owned_quote"]), D(100))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.5"))

    def test_fill_proceeds_grow_opposite_side_budget(self):
        """Fill-driven rebalance: a sell fill credits quote -> buy budget grows next cycle."""
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=300, bid=299, ask=301)
        ctrl = self._build(mdp)
        before = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D(10), available_base=D(0),
            active_buy_reserved_quote=D(0), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )[0]
        # sell fill delivered ~28 USDT into the wallet
        after = ctrl._compute_deploy_budgets(
            reference_price=D(300), available_quote=D("37.7"), available_base=D(0),
            active_buy_reserved_quote=D(0), active_sell_reserved_base=D(0),
            deploy_ceiling=D(1000),
        )[0]
        self.assertGreater(after, before)


# ============================================================ concentrate compression

class _CompressHarness(unittest.TestCase):
    @staticmethod
    def _buy(prices, weights, min_order_quote="5"):
        controller = MagicMock(spec=RangeInventoryLadderController)
        controller.config = MagicMock()
        controller.config.buy_prices = [D(p) for p in prices]
        controller.config.min_order_quote = D(min_order_quote)
        controller.config.connector_name = "nonkyc"
        controller.config.trading_pair = "XMR-USDT"
        total_w = sum(D(w) for w in weights)
        controller.config.normalized_buy_weights = [D(w) / total_w for w in weights]
        mdp = MagicMock()
        mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
        mdp.quantize_order_amount.side_effect = lambda c, p, a: D(a).quantize(D("0.001"), rounding=ROUND_DOWN)
        controller.market_data_provider = mdp
        controller._compress_buy_level_indexes_for_min_notional = (
            RangeInventoryLadderController._compress_buy_level_indexes_for_min_notional.__get__(
                controller, RangeInventoryLadderController)
        )
        _bind_exchange_min_gate(controller)
        return controller

    @staticmethod
    def _sell(prices, weights, min_order_quote="5"):
        controller = MagicMock(spec=RangeInventoryLadderController)
        controller.config = MagicMock()
        controller.config.sell_prices = [D(p) for p in prices]
        controller.config.min_order_quote = D(min_order_quote)
        controller.config.connector_name = "nonkyc"
        controller.config.trading_pair = "XMR-USDT"
        total_w = sum(D(w) for w in weights)
        controller.config.normalized_sell_weights = [D(w) / total_w for w in weights]
        mdp = MagicMock()
        mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
        mdp.quantize_order_amount.side_effect = lambda c, p, a: D(a).quantize(D("0.001"), rounding=ROUND_DOWN)
        controller.market_data_provider = mdp
        controller._compress_sell_level_indexes_for_min_notional = (
            RangeInventoryLadderController._compress_sell_level_indexes_for_min_notional.__get__(
                controller, RangeInventoryLadderController)
        )
        _bind_exchange_min_gate(controller)
        return controller


class TestConcentrateCompression(_CompressHarness):

    def test_buy_budget_for_one_level_keeps_exactly_one(self):
        ctrl = self._buy([320, 315, 310], [1, 1, 1])
        kept = ctrl._compress_buy_level_indexes_for_min_notional([0, 1, 2], D(7))
        self.assertEqual(kept, [0])  # 7/2=3.5<5 each for two; one level gets full 7

    def test_buy_budget_for_two_keeps_two_nearest(self):
        ctrl = self._buy([320, 315, 310], [1, 1, 1])
        kept = ctrl._compress_buy_level_indexes_for_min_notional([0, 1, 2], D(11))
        self.assertEqual(kept, [0, 1])  # 11/2=5.5>=5 each; 11/3=3.67<5

    def test_buy_budget_below_one_level_keeps_none(self):
        ctrl = self._buy([320, 315, 310], [1, 1, 1])
        kept = ctrl._compress_buy_level_indexes_for_min_notional([0, 1, 2], D(4))
        self.assertEqual(kept, [])

    def test_buy_concentrates_single_level_when_only_one_fundable(self):
        """A budget that funds exactly one level must KEEP one (never strand to [])."""
        ctrl = self._buy([320, 315, 310], [1, 1, 1])
        kept = ctrl._compress_buy_level_indexes_for_min_notional([0, 1, 2], D("5.5"))
        self.assertEqual(kept, [0])

    def test_sell_budget_for_one_level_keeps_exactly_one(self):
        ctrl = self._sell([350, 355, 360], [1, 1, 1])
        kept = ctrl._compress_sell_level_indexes_for_min_notional([0, 1, 2], D("0.02"))
        self.assertEqual(kept, [0])  # 0.02*350=7>=5; 0.01*350=3.5<5

    def test_sell_budget_for_two_keeps_two_nearest(self):
        ctrl = self._sell([350, 355, 360], [1, 1, 1])
        kept = ctrl._compress_sell_level_indexes_for_min_notional([0, 1, 2], D("0.04"))
        self.assertEqual(kept, [0, 1])  # 0.02*350=7 each; 0.0133*350=4.67<5

    def test_sell_budget_below_one_level_keeps_none(self):
        ctrl = self._sell([350, 355, 360], [1, 1, 1])
        kept = ctrl._compress_sell_level_indexes_for_min_notional([0, 1, 2], D("0.01"))
        self.assertEqual(kept, [])


# ============================================================ one-sided / no-conversion

class TestOneSidedNoConversion(_ControllerHarness):

    async def _run_cycle(self, ctrl):
        await ctrl.update_processed_data()
        return ctrl.create_actions_proposal()

    def _init_state(self, ctrl, owned_quote, owned_base, seed_value):
        ctrl._state = {
            "initialized": True,
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
        ctrl.executors_info = []
        ctrl.positions_held = []

    def test_all_base_wallet_runs_sell_only_ladder(self):
        import asyncio
        # Price between bands: sells > ask, buys < bid. Wallet is all XMR.
        mdp = _make_mdp(balances={"XMR": (D(1), D(1)), "USDT": (D("0.04"), D("0.04"))},
                        mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=0, owned_base=1, seed_value=170)

        actions = asyncio.run(self._run_cycle(ctrl))

        self.assertTrue(actions, "expected sell ladder actions on the funded side")
        for a in actions:
            self.assertIsInstance(a, CreateExecutorAction)
            cfg = a.executor_config
            # No conversion/rebalance order: only normal SELL ladder levels at config prices.
            self.assertEqual(cfg.side, TradeType.SELL)
            self.assertIn(cfg.price, ctrl.config.sell_prices)
            self.assertNotEqual(cfg.execution_strategy, ExecutionStrategy.MARKET)
        buy_actions = [a for a in actions if a.executor_config.side == TradeType.BUY]
        self.assertEqual(buy_actions, [])

    def test_all_quote_wallet_runs_buy_only_ladder(self):
        import asyncio
        mdp = _make_mdp(balances={"XMR": (D("0.0001"), D("0.0001")), "USDT": (D(500), D(500))},
                        mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=170, owned_base=0, seed_value=170)

        actions = asyncio.run(self._run_cycle(ctrl))

        self.assertTrue(actions, "expected buy ladder actions on the funded side")
        for a in actions:
            self.assertEqual(a.executor_config.side, TradeType.BUY)
            self.assertIn(a.executor_config.price, ctrl.config.buy_prices)
            self.assertNotEqual(a.executor_config.execution_strategy, ExecutionStrategy.MARKET)
        sell_actions = [a for a in actions if a.executor_config.side == TradeType.SELL]
        self.assertEqual(sell_actions, [])

    def test_deposit_picked_up_next_cycle_without_state_reset(self):
        """Increasing available quote between cycles grows the buy budget; owned ledger unchanged."""
        import asyncio
        balances = {"XMR": (D("0.0001"), D("0.0001")), "USDT": (D(20), D(20))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote=170, owned_base=0, seed_value=170)

        asyncio.run(ctrl.update_processed_data())
        free_buy_before = ctrl.processed_data["free_buy_budget_quote"]
        owned_quote_before = ctrl._state["owned_quote"]

        # Simulate a deposit: available USDT jumps 20 -> 120 (no state file reset).
        balances["USDT"] = (D(120), D(120))
        asyncio.run(ctrl.update_processed_data())
        free_buy_after = ctrl.processed_data["free_buy_budget_quote"]

        self.assertGreater(free_buy_after, free_buy_before)
        self.assertEqual(ctrl._state["owned_quote"], owned_quote_before)  # ledger untouched


if __name__ == "__main__":
    unittest.main()
