"""v15 behavior tests for range_inventory_ladder.

Two production incidents from the 2026-06-30 KRAKEN_LADDER_V1 instance drove these fixes:

1. EXCHANGE-MINIMUM GATE. The ladder placed 9 XMR-USD orders of 0.0046-0.0114 XMR against
   Kraken's 0.015 XMR minimum order size. Every order failed connector-side with
   "Order amount ... is lower than minimum order size 0.015" and burned 10 executor retries.
   Root cause: quantize_order_amount only snaps to the size quantum (it does NOT zero
   below-minimum amounts), and the controller's min-notional compression checked only the
   config min_order_quote (1 USD), never the exchange TradingRule. The fix routes compression,
   the create path and the planner through one shared feasibility gate
   (_level_quantization_failure) that also enforces the exchange min_order_size /
   min_notional_size / min_order_value.

2. FILL-SETTLE GRACE. One second after a 0.06874104 XMR buy fill booked, the controller
   warned "ledger over-claim detected" (overclaim_quote=20.41) and clamped the sell budget to
   near zero. Root cause: the ledger books a fill instantly while Kraken's wallet snapshot
   lags up to one LONG_POLL (120s) — and Kraken has no is_balance_settling flag, so the v14
   settling deferral never engaged. The fix defers over-claim reconciliation within
   fill_settle_grace_seconds (default 90) of the last BOOKED fill.
"""
import asyncio
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
    """balances: {asset: (total, available)}."""
    mdp = MagicMock()
    mdp.time.return_value = now
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.00000001"), rounding=ROUND_DOWN)
    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


def _kraken_xmr_usd_rule(min_order_size="0.015", min_notional="0", min_order_value="0"):
    """The XMR-USD trading rule shape from the incident (ordermin=0.015)."""
    return SimpleNamespace(
        min_order_size=D(min_order_size),
        min_notional_size=D(min_notional),
        min_order_value=D(min_order_value),
    )


def _install_trading_rule(mdp, pair, rule):
    mdp.get_connector.return_value.trading_rules = {pair: rule}


def _filling(level_id, side, price, eid, *, filled_base, filled_quote, fees="0"):
    """A TERMINATED executor reporting a cumulative fill via custom_info."""
    ex = MagicMock()
    ex.id = eid
    ex.status = RunnableStatus.TERMINATED
    ex.is_active = False
    ex.timestamp = 0.0
    ex.close_timestamp = 1000.0
    ex.connector_name = "kraken"
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

    def _build(self, mdp, *, cid="ctrl-v15", pair="XMR-USD", **config_overrides):
        defaults = dict(
            id=cid,
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="kraken",
            trading_pair=pair,
            total_amount_quote=Decimal("200"),
            max_fund_value_quote=Decimal("5000"),
            buy_prices=[Decimal("306.26"), Decimal("300.00"), Decimal("293.43")],
            buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("334.41"), Decimal("340.00"), Decimal("345.60"), Decimal("351.37")],
            sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1")],
            min_order_quote=Decimal("1"),
            ledger_overclaim_reanchor_seconds=999_999,  # re-anchor out of scope here
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
                    reserve_quote="0", reserve_base="0", pair="XMR-USD"):
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
            "initial_reference_price": "311.205",
            "reserve_quote_balance": str(reserve_quote),
            "reserve_base_balance": str(reserve_base),
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]


# =================================================== exchange-minimum feasibility gate

class TestExchangeMinimumGate(_Harness):

    def _mdp(self):
        return _make_mdp(balances={"XMR": (D(0), D(0)), "USD": (D(0), D(0))}, mid=306, bid=305, ask=307)

    def test_no_trading_rule_reduces_to_legacy_checks(self):
        # MagicMock connector: trading_rules is not a dict -> exchange minimums resolve to 0.
        ctrl = self._build(self._mdp())
        self.assertIsNone(ctrl._exchange_trading_rule())
        self.assertEqual(D(0), ctrl._exchange_min_order_size())
        self.assertEqual(D(0), ctrl._exchange_min_notional())
        # amount>0 and notional>=min_order_quote (1) -> feasible
        self.assertIsNone(ctrl._level_quantization_failure(D("0.01"), D("306.26")))

    def test_zero_amount_rejected(self):
        ctrl = self._build(self._mdp())
        self.assertEqual("quantized_amount_zero", ctrl._level_quantization_failure(D("0"), D("306.26")))

    def test_below_config_min_notional_rejected(self):
        ctrl = self._build(self._mdp())
        # 0.001 * 306.26 = 0.30626 < min_order_quote 1
        self.assertEqual("notional_below_min", ctrl._level_quantization_failure(D("0.001"), D("306.26")))

    def test_below_exchange_min_order_size_rejected(self):
        mdp = self._mdp()
        ctrl = self._build(mdp)
        _install_trading_rule(mdp, "XMR-USD", _kraken_xmr_usd_rule())
        # The EXACT first failing production order: 0.00457721 XMR @ 306.26 (notional 1.40 >= 1,
        # so the old gate passed it and the connector rejected it).
        self.assertEqual(
            "below_exchange_min_order_size",
            ctrl._level_quantization_failure(D("0.00457721"), D("306.26")),
        )

    def test_at_exchange_min_order_size_accepted(self):
        mdp = self._mdp()
        ctrl = self._build(mdp)
        _install_trading_rule(mdp, "XMR-USD", _kraken_xmr_usd_rule())
        self.assertIsNone(ctrl._level_quantization_failure(D("0.015"), D("306.26")))

    def test_below_exchange_min_notional_rejected(self):
        mdp = self._mdp()
        ctrl = self._build(mdp)
        _install_trading_rule(mdp, "XMR-USD", _kraken_xmr_usd_rule(min_order_size="0", min_notional="10"))
        # 0.02 * 306.26 = 6.13 >= config 1 but < exchange 10
        self.assertEqual(
            "below_exchange_min_notional",
            ctrl._level_quantization_failure(D("0.02"), D("306.26")),
        )

    def test_min_notional_takes_stricter_of_pair(self):
        mdp = self._mdp()
        ctrl = self._build(mdp)
        _install_trading_rule(
            mdp, "XMR-USD",
            _kraken_xmr_usd_rule(min_order_size="0", min_notional="5", min_order_value="12"))
        self.assertEqual(D("12"), ctrl._exchange_min_notional())

    def test_non_finite_rule_values_ignored(self):
        mdp = self._mdp()
        ctrl = self._build(mdp)
        _install_trading_rule(
            mdp, "XMR-USD",
            SimpleNamespace(min_order_size=D("NaN"), min_notional_size=D("Infinity"), min_order_value=None))
        self.assertEqual(D(0), ctrl._exchange_min_order_size())
        self.assertEqual(D(0), ctrl._exchange_min_notional())


# =================================================== Kraken incident reproduction

class TestKrakenMinSizeIncident(_Harness):
    """Reproduces the 2026-07-01 15:50:42 incident: budget 6.6586 USD across 3 buy levels
    produced 0.0046-0.0107 XMR orders, all below Kraken's 0.015 XMR minimum."""

    def _mdp_with_rule(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USD": (D("6.66"), D("6.66"))},
                        mid=306, bid=305, ask=307)
        _install_trading_rule(mdp, "XMR-USD", _kraken_xmr_usd_rule())
        return mdp

    def test_buy_compression_concentrates_above_exchange_minimum(self):
        ctrl = self._build(self._mdp_with_rule())
        # Equal thirds of 6.6586 are ~2.22 USD -> ~0.0072 XMR each: below 0.015. The old gate
        # kept all three (notional 2.22 >= 1) and every order failed on the exchange.
        kept = ctrl._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=[0, 1, 2],
            total_quote_budget=D("6.6586324285"),
        )
        self.assertEqual([0], kept)  # concentrated into the nearest level: 6.6586/306.26 = 0.0217 XMR
        # And the surviving level really is placeable on Kraken.
        w_total = sum(ctrl.config.normalized_buy_weights[i] for i in kept)
        for idx in kept:
            level_quote = D("6.6586324285") * ctrl.config.normalized_buy_weights[idx] / w_total
            amount = (level_quote / D("306.26")).quantize(D("0.00000001"), rounding=ROUND_DOWN)
            self.assertGreaterEqual(amount, D("0.015"))

    def test_sell_compression_concentrates_above_exchange_minimum(self):
        ctrl = self._build(self._mdp_with_rule())
        # 0.07085676 XMR over 4 levels = 0.0177 each (feasible); over 5+ would be below min.
        kept = ctrl._compress_sell_level_indexes_for_min_notional(
            candidate_indexes=[0, 1, 2, 3],
            total_base_budget=D("0.0708567600"),
        )
        self.assertEqual([0, 1, 2, 3], kept)
        # Shrink the budget so 4 ways is below the exchange minimum: must drop levels, and
        # every kept level must clear 0.015.
        kept_small = ctrl._compress_sell_level_indexes_for_min_notional(
            candidate_indexes=[0, 1, 2, 3],
            total_base_budget=D("0.045"),
        )
        self.assertTrue(kept_small)  # 0.045 funds up to 3 levels of 0.015
        self.assertLess(len(kept_small), 4)
        w_total = sum(ctrl.config.normalized_sell_weights[i] for i in kept_small)
        for idx in kept_small:
            level_base = D("0.045") * ctrl.config.normalized_sell_weights[idx] / w_total
            self.assertGreaterEqual(
                level_base.quantize(D("0.00000001"), rounding=ROUND_DOWN), D("0.015"))

    def test_build_buy_action_refuses_sub_minimum_order(self):
        """REGRESSION: the exact failing production order (0.00457721 XMR @ 306.26) must never
        reach the executor. Before the fix this returned a CreateExecutorAction."""
        ctrl = self._build(self._mdp_with_rule())
        action, notional = ctrl._build_buy_executor_action(
            idx=0, level_id="buy_0", price=D("306.26"),
            order_quote=D("1.4017"),  # -> 0.00457... XMR, below ordermin 0.015
            remaining_quote_before=D("6.6586"),
            kept_buy_indexes=[0],
            passive_execution_strategy=ExecutionStrategy.LIMIT_MAKER,
        )
        self.assertIsNone(action)
        self.assertEqual(D(0), notional)
        skips = self._events(ctrl, "range_ladder_buy_level_skipped_post_quantization")
        self.assertEqual(1, len(skips))
        self.assertEqual("below_exchange_min_order_size", skips[0].kwargs["reason"])

    def test_build_sell_action_refuses_sub_minimum_order(self):
        ctrl = self._build(self._mdp_with_rule())
        action, amount = ctrl._build_sell_executor_action(
            idx=0, level_id="sell_0", price=D("334.41"),
            order_base=D("0.00517254"),  # the failing production sell amount
            remaining_base_before=D("0.07085676"),
            kept_sell_indexes=[0],
            passive_execution_strategy=ExecutionStrategy.LIMIT_MAKER,
        )
        self.assertIsNone(action)
        self.assertEqual(D(0), amount)
        skips = self._events(ctrl, "range_ladder_sell_level_skipped_post_quantization")
        self.assertEqual(1, len(skips))
        self.assertEqual("below_exchange_min_order_size", skips[0].kwargs["reason"])

    def test_build_buy_action_places_feasible_order(self):
        ctrl = self._build(self._mdp_with_rule())
        action, notional = ctrl._build_buy_executor_action(
            idx=0, level_id="buy_0", price=D("306.26"),
            order_quote=D("6.6586"),  # -> 0.0217 XMR, above ordermin
            remaining_quote_before=D("6.6586"),
            kept_buy_indexes=[0],
            passive_execution_strategy=ExecutionStrategy.LIMIT_MAKER,
        )
        self.assertIsNotNone(action)
        self.assertGreaterEqual(action.executor_config.amount, D("0.015"))

    def test_planner_mirrors_create_gate(self):
        """The planner quantizers must apply the same exchange-minimum gate as the create path."""
        ctrl = self._build(self._mdp_with_rule())
        self.assertIsNone(ctrl._quantize_buy_level(D("306.26"), D("1.4017")))
        self.assertIsNotNone(ctrl._quantize_buy_level(D("306.26"), D("6.6586")))
        self.assertIsNone(ctrl._quantize_sell_level(D("334.41"), D("0.00517254")))
        self.assertIsNotNone(ctrl._quantize_sell_level(D("334.41"), D("0.0177")))


# =================================================== fill-settle grace (over-claim deferral)

class TestFillSettleGrace(_Harness):

    def _incident_mdp(self, now=1000.0):
        """Wallet snapshot from the 2026-07-01 06:47:20 incident: the 0.06874104 XMR buy fill
        is booked in the ledger but NOT yet in the wallet totals."""
        return _make_mdp(
            balances={"XMR": (D("1.0753632200"), D("0.0021157200")),
                      "USD": (D("106.7112"), D("6.66"))},
            mid=306.26, bid=306.20, ask=306.32, now=now)

    def _incident_controller(self, mdp):
        ctrl = self._build(mdp)
        self._init_state(ctrl, owned_quote="78.8947399359500000", owned_base="1.14198857",
                         seed_value="434")
        return ctrl

    def test_config_default_and_validation(self):
        ctrl = self._build(self._incident_mdp())
        self.assertEqual(90, ctrl.config.fill_settle_grace_seconds)
        extra = RangeInventoryLadderConfig.model_fields["fill_settle_grace_seconds"].json_schema_extra or {}
        self.assertTrue(extra.get("is_updatable", False))
        with self.assertRaises(Exception):
            RangeInventoryLadderConfig(
                id="t", controller_name="range_inventory_ladder", controller_type="market_making",
                connector_name="kraken", trading_pair="XMR-USD", total_amount_quote=Decimal("100"),
                buy_prices=[Decimal("300")], buy_amounts_pct=[Decimal("1")],
                sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")],
                fill_settle_grace_seconds=-1)

    def test_within_grace_window_logic(self):
        ctrl = self._build(self._incident_mdp())
        self.assertFalse(ctrl._within_fill_settle_grace(1000.0))  # no fill yet
        ctrl._last_fill_booked_ts = 999.0
        self.assertTrue(ctrl._within_fill_settle_grace(1000.0))   # 1s after fill
        self.assertTrue(ctrl._within_fill_settle_grace(1088.9))   # 89.9s after fill
        self.assertFalse(ctrl._within_fill_settle_grace(1089.0))  # grace (90s) expired

    def test_booking_a_fill_stamps_grace_timestamp(self):
        mdp = self._incident_mdp()
        ctrl = self._incident_controller(mdp)
        self.assertIsNone(ctrl._last_fill_booked_ts)
        ctrl.executors_info = [_filling(
            "buy_306.26", TradeType.BUY, "306.26", "exec-1",
            filled_base="0.06874104", filled_quote="21.0526309104", fees="0.05263158")]
        ctrl._book_fills_from_orders()
        self.assertEqual(1000.0, ctrl._last_fill_booked_ts)

    def test_no_fill_leaves_grace_unarmed(self):
        mdp = self._incident_mdp()
        ctrl = self._incident_controller(mdp)
        ctrl._book_fills_from_orders()  # no executors -> nothing booked
        self.assertIsNone(ctrl._last_fill_booked_ts)

    def test_overclaim_warning_deferred_within_grace(self):
        """REGRESSION for the 06:47:20 false positive: ledger is one fill ahead of the wallet,
        the connector has no is_balance_settling flag (Kraken), and the fill booked 1s ago.
        Neither the over-claim warning nor the wallet-floor warning may fire."""
        mdp = self._incident_mdp()
        ctrl = self._incident_controller(mdp)
        ctrl._last_fill_booked_ts = 999.0  # fill booked 1s ago
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual([], self._events(ctrl, "range_ladder_reconciliation_overclaim"))
        self.assertEqual([], self._events(ctrl, "range_ladder_wallet_floor_binding"))

    def test_persistent_overclaim_still_warns_after_grace(self):
        """A GENUINE over-claim (wallet never catches up) must still warn once the grace expires."""
        mdp = self._incident_mdp()
        ctrl = self._incident_controller(mdp)
        ctrl._last_fill_booked_ts = 905.0  # 95s ago -> grace (90s) expired
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_reconciliation_overclaim")))
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_wallet_floor_binding")))

    def test_connector_settling_flag_still_defers(self):
        """The v14 connector-flag path (NonKYC) must keep working alongside the v15 grace."""
        mdp = self._incident_mdp()
        ctrl = self._incident_controller(mdp)
        mdp.get_connector.return_value.is_balance_settling = True
        asyncio.run(ctrl.update_processed_data())
        self.assertEqual([], self._events(ctrl, "range_ladder_reconciliation_overclaim"))
        self.assertEqual([], self._events(ctrl, "range_ladder_wallet_floor_binding"))


if __name__ == "__main__":
    unittest.main()
