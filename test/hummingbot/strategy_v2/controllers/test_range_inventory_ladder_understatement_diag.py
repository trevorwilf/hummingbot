"""Phase 5 hardening: ledger-understatement drift diagnostic.

A missed final SELL fill understates owned_quote; the v12 re-anchor is deliberately
downward-only, so understated proceeds strand outside the fund silently. The controller now
computes the wallet-over-ledger surplus each cycle and, when it exceeds
ledger_reconcile_threshold_quote continuously for LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS
(not during balance settling), emits range_ladder_ledger_understatement_suspected --
rate-limited to once per _drift_warning_interval with its OWN latch (not shared with the
over-claim warning). Diagnostic only: the ledger is never auto-corrected upward.

Harness mirrors the over-claim settle-grace test pattern in
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

PERSIST_S = RangeInventoryLadderController.LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS  # 1800.0


def _make_mdp(*, balances, mid, bid, ask, now=1000.0):
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
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        defaults = dict(
            id="ctrl-und",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
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
        controller.executors_info = []
        controller.positions_held = []
        return controller

    def _set_state(self, ctrl, *, owned_quote, owned_base):
        ctrl._state = {
            "initialized": True,
            "owned_quote": str(owned_quote),
            "owned_base": str(owned_base),
            "seed_value_quote": str(owned_quote),
            "initial_managed_quote": str(owned_quote),
            "initial_claimed_base_amount": str(owned_base),
            "initial_reference_price": "335",
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]

    def _surplus_ctrl(self, *, owned_quote="100", wallet_quote="200"):
        """Controller whose wallet holds `wallet_quote` while the ledger owns `owned_quote`."""
        balances = {"XMR": (D(0), D(0)), "USDT": (D(wallet_quote), D(wallet_quote))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote=owned_quote, owned_base="0")
        return ctrl, mdp


class TestUnderstatementDiagnostic(_Harness):

    EVENT = "range_ladder_ledger_understatement_suspected"

    def test_surplus_below_threshold_no_event_and_timer_stays_unset(self):
        # surplus 0.3 < threshold 0.5 -> never even starts the persistence timer.
        ctrl, mdp = self._surplus_ctrl(owned_quote="100", wallet_quote="100.3")
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 100)
        self.assertEqual([], self._events(ctrl, self.EVENT))
        self.assertIsNone(ctrl._understatement_since)

    def test_surplus_above_threshold_but_short_lived_no_event(self):
        ctrl, mdp = self._surplus_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S - 1)   # persistence not yet met
        self.assertEqual([], self._events(ctrl, self.EVENT))
        self.assertEqual(1000.0, ctrl._understatement_since)

    def test_persistent_surplus_single_baseline_warning_then_reduced_diag_cadence(self):
        # Growth-gate fix (2026-07-12): a STABLE surplus warns exactly once (the baseline);
        # the previous behavior re-warned every _drift_warning_interval and produced 85%+ of
        # total warning volume in production. The jsonl event keeps flowing at a reduced
        # cadence (once per 30 minutes) for offline analysis.
        ctrl, mdp = self._surplus_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                     # timer starts
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # persistence met -> baseline warning
        events = self._events(ctrl, self.EVENT)
        self.assertEqual(1, len(events))
        self.assertEqual("100", events[0].kwargs["surplus_quote"])
        self.assertEqual("100", events[0].kwargs["owned_quote"])
        self.assertEqual("200", events[0].kwargs["wallet_derived_quote"])
        self.assertEqual("baseline", events[0].kwargs["warn_kind"])

        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 100)   # flat -> silent
        self.assertEqual(1, len(self._events(ctrl, self.EVENT)))

        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 301)   # still flat -> STILL silent
        self.assertEqual(1, len(self._events(ctrl, self.EVENT)))

        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 1801)  # reduced-cadence jsonl visibility
        events = self._events(ctrl, self.EVENT)
        self.assertEqual(2, len(events))
        self.assertEqual("flat_no_warning", events[1].kwargs["warn_kind"])

    def test_surplus_clearing_resets_the_persistence_timer(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote="100", owned_base="0")
        self._cycle(ctrl, mdp, 1000.0)                     # surplus -> timer starts
        self.assertEqual(1000.0, ctrl._understatement_since)

        balances["USDT"] = (D(100), D(100))                # surplus clears
        self._cycle(ctrl, mdp, 1100.0)
        self.assertIsNone(ctrl._understatement_since)

        balances["USDT"] = (D(200), D(200))                # surplus returns -> fresh timer
        self._cycle(ctrl, mdp, 1200.0)
        self._cycle(ctrl, mdp, 1200.0 + PERSIST_S - 1)     # not persistent from the NEW start
        self.assertEqual([], self._events(ctrl, self.EVENT))

    def test_settling_defers_timer_without_resetting_it(self):
        # Mirror of the over-claim settle-grace pattern: a settling cycle neither advances the
        # warning nor resets the timer, so persistence completes on the first post-settle cycle.
        ctrl, mdp = self._surplus_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                     # timer starts at 1000
        mdp.get_connector.return_value.is_balance_settling = True
        self._cycle(ctrl, mdp, 1500.0)                     # settling -> deferred, timer kept
        self.assertEqual(1000.0, ctrl._understatement_since)
        self.assertEqual([], self._events(ctrl, self.EVENT))

        mdp.get_connector.return_value.is_balance_settling = False
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # post-settle -> persistence met
        self.assertEqual(1, len(self._events(ctrl, self.EVENT)))

    def test_no_warning_while_settling_even_when_persistence_met(self):
        ctrl, mdp = self._surplus_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        mdp.get_connector.return_value.is_balance_settling = True
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 100)   # would warn, but settling
        self.assertEqual([], self._events(ctrl, self.EVENT))

    def test_surplus_surfaces_in_processed_data_and_custom_info(self):
        ctrl, mdp = self._surplus_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(100), ctrl.processed_data["ledger_surplus_quote"])
        self.assertEqual("100", ctrl.get_custom_info()["ledger_surplus_quote"])

    def test_base_surplus_valued_at_reference_price(self):
        # wallet base 1, owned base 0 -> surplus = 1 * mid(335).
        balances = {"XMR": (D(1), D(1)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote="100", owned_base="0")
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(335), ctrl.processed_data["ledger_surplus_quote"])

    def test_ledger_is_never_raised_by_the_diagnostic(self):
        ctrl, mdp = self._surplus_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # warning fired
        self.assertEqual("100", ctrl._state["owned_quote"])  # ledger untouched


if __name__ == "__main__":
    unittest.main()
