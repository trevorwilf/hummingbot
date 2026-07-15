"""CSF-V1 Phase 5: range ladder noise & churn fixes (LOG-6, LOG-8, LOG-2').

- LOG-6: regime hysteresis. Production (2026-07-13/14 Kraken XMR) logged 133 regime
  transitions in 38h, median 12s apart, with the price parked exactly on the first sell
  level. A new `regime_dwell_seconds` config (default 30) requires the regime condition to
  hold for the dwell before the switch is confirmed (logged / emitted / reported). Fill
  processing is regime-independent: a fill during the dwell books normally.

- LOG-8: over-claim warning settle-grace. A WS balance delta landing ~1s before the fill
  event reached the ledger read as a momentary over-claim and fired the warning even though
  the self-heal cleared it in 1s. The warning now requires the over-claim to persist >10s
  (OVERCLAIM_WARNING_GRACE_SECONDS) or across two consecutive non-settling evaluations.

- LOG-2': understatement vs manual orders. The operator's manual ("orphan") open orders
  hold wallet balance that is legitimately outside the fills-only ledger; the chronic
  "possible ledger understatement" warnings were exactly those holds. The understatement
  check now subtracts the connector's `external_order_holds(trading_pair)` (NonKYC-only;
  Kraken degrades gracefully to zeros == current behavior) before comparing wallet-held vs
  ledger-owned. The warning stays intact for residual drift above the existing threshold.

DEFERRED (recorded per the phase instruction): LOG-8's "immediate re-propose on ledger
update" watchdog refinement — watchdog timing is deliberately untouched in this phase.
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

PERSIST_S = RangeInventoryLadderController.LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS  # 1800.0
GRACE_S = RangeInventoryLadderController.OVERCLAIM_WARNING_GRACE_SECONDS  # 10.0

REGIME_EVENT = "range_ladder_price_regime_changed"
OVERCLAIM_EVENT = "range_ladder_reconciliation_overclaim"
UNDERSTATEMENT_EVENT = "range_ladder_ledger_understatement_suspected"


def _make_mdp(*, balances, prices, now=1000.0):
    """balances: {asset: (total, available)} (mutable), prices: {"mid","bid","ask"} (mutable)."""
    mdp = MagicMock()
    mdp.time.return_value = now
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(prices["mid"]),
        PriceType.BestBid: D(prices["bid"]),
        PriceType.BestAsk: D(prices["ask"])}[pt]
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)
    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


def _install_external_holds(mdp, holds):
    """A real callable returning a real dict, so the strict sanitization accepts it."""
    mdp.get_connector.return_value.external_order_holds = lambda pair: holds


def _filling(level_id, side, price, eid, *, filled_base, filled_quote, fees="0"):
    """A TERMINATED executor reporting a cumulative fill via custom_info."""
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
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        defaults = dict(
            id="ctrl-p5",
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
            ledger_overclaim_reanchor_seconds=999_999,  # the v12 re-anchor is out of scope here
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

    def _regime_ctrl(self, **overrides):
        """Controller with a calm wallet, mid parked between the ladders (335)."""
        balances = {"XMR": (D(1), D(1)), "USDT": (D(100), D(100))}
        prices = {"mid": 335, "bid": 334.9, "ask": 335.1}
        mdp = _make_mdp(balances=balances, prices=prices)
        ctrl = self._build(mdp, **overrides)
        self._set_state(ctrl, owned_quote="100", owned_base="1")
        return ctrl, mdp, prices


# =================================================== LOG-6: regime hysteresis

class TestRegimeHysteresis(_Harness):

    def test_config_default_and_validation(self):
        ctrl, _, _ = self._regime_ctrl()
        self.assertEqual(30, ctrl.config.regime_dwell_seconds)
        extra = RangeInventoryLadderConfig.model_fields["regime_dwell_seconds"].json_schema_extra or {}
        self.assertTrue(extra.get("is_updatable", False))
        with self.assertRaises(Exception):
            RangeInventoryLadderConfig(
                id="t", controller_name="range_inventory_ladder", controller_type="market_making",
                connector_name="nonkyc", trading_pair="XMR-USDT", total_amount_quote=Decimal("100"),
                buy_prices=[Decimal("321")], buy_amounts_pct=[Decimal("1")],
                sell_prices=[Decimal("350")], sell_amounts_pct=[Decimal("1")],
                regime_dwell_seconds=-1)

    def test_first_cycle_confirms_immediately(self):
        ctrl, mdp, _ = self._regime_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        events = self._events(ctrl, REGIME_EVENT)
        self.assertEqual(1, len(events))
        self.assertEqual("", events[0].kwargs["previous_regime"])
        self.assertEqual("between_ladders", events[0].kwargs["new_regime"])
        self.assertEqual("between_ladders", ctrl.processed_data["price_regime"])

    def test_flap_within_dwell_suppressed(self):
        """The exact production pattern: price oscillating across the first sell level every
        few seconds. Pre-fix, EVERY crossing emitted a regime-changed event."""
        ctrl, mdp, prices = self._regime_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # confirm between_ladders
        for i, mid in enumerate([352, 335, 352, 335, 352]):  # 12s flaps, all shorter than 30s dwell
            prices["mid"] = mid
            self._cycle(ctrl, mdp, 1012.0 + i * 12)
        self.assertEqual(1, len(self._events(ctrl, REGIME_EVENT)))
        self.assertEqual("between_ladders", ctrl.processed_data["price_regime"])
        self.assertEqual("between_ladders", ctrl._last_price_regime)

    def test_change_confirmed_after_dwell(self):
        ctrl, mdp, prices = self._regime_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # confirm between_ladders
        prices["mid"] = 352
        self._cycle(ctrl, mdp, 1010.0)                      # change observed, dwell starts
        self.assertEqual(1, len(self._events(ctrl, REGIME_EVENT)))
        self.assertEqual("between_ladders", ctrl.processed_data["price_regime"])  # still the old one
        self._cycle(ctrl, mdp, 1041.0)                      # 31s held > 30s dwell -> confirmed
        events = self._events(ctrl, REGIME_EVENT)
        self.assertEqual(2, len(events))
        self.assertEqual("between_ladders", events[1].kwargs["previous_regime"])
        self.assertEqual("inside_sell_band", events[1].kwargs["new_regime"])
        self.assertEqual("inside_sell_band", ctrl.processed_data["price_regime"])

    def test_return_to_confirmed_regime_resets_the_dwell(self):
        ctrl, mdp, prices = self._regime_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        prices["mid"] = 352
        self._cycle(ctrl, mdp, 1010.0)                      # dwell starts at 1010
        prices["mid"] = 335
        self._cycle(ctrl, mdp, 1020.0)                      # back to confirmed -> pending cleared
        prices["mid"] = 352
        self._cycle(ctrl, mdp, 1045.0)                      # NEW dwell starts at 1045
        self.assertEqual(1, len(self._events(ctrl, REGIME_EVENT)))
        self._cycle(ctrl, mdp, 1076.0)                      # 31s from the NEW start -> confirmed
        self.assertEqual(2, len(self._events(ctrl, REGIME_EVENT)))

    def test_dwell_zero_confirms_on_first_observation(self):
        ctrl, mdp, prices = self._regime_ctrl(regime_dwell_seconds=0)
        self._cycle(ctrl, mdp, 1000.0)
        prices["mid"] = 352
        self._cycle(ctrl, mdp, 1001.0)
        events = self._events(ctrl, REGIME_EVENT)
        self.assertEqual(2, len(events))
        self.assertEqual("inside_sell_band", events[1].kwargs["new_regime"])

    def test_fill_during_dwell_books_normally(self):
        """Fill processing is regime-independent: a fill landing mid-dwell must book into the
        ledger even though the regime switch is still unconfirmed."""
        ctrl, mdp, prices = self._regime_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # confirm between_ladders
        prices["mid"] = 352
        ctrl.executors_info = [_filling(
            "buy_321", TradeType.BUY, "321", "exec-fill-1",
            filled_base="0.1", filled_quote="32.1", fees="0.0642")]
        self._cycle(ctrl, mdp, 1010.0)                      # mid-dwell cycle carrying the fill
        # the fill booked: base ledger grew, the booked-fill timestamp was stamped
        self.assertEqual(1010.0, ctrl._last_fill_booked_ts)
        self.assertEqual(D("1.1"), D(ctrl._state["owned_base"]))
        self.assertLess(D(ctrl._state["owned_quote"]), D("100"))
        # ...while the regime switch is still dwelling (no second event)
        self.assertEqual(1, len(self._events(ctrl, REGIME_EVENT)))
        self.assertEqual("between_ladders", ctrl.processed_data["price_regime"])


# =================================================== LOG-8: over-claim warning settle-grace

class TestOverclaimWarningGrace(_Harness):

    def _overclaim_ctrl(self):
        """Ledger owns 100 quote; the wallet only holds 50 -> a 50-quote over-claim."""
        balances = {"XMR": (D(0), D(0)), "USDT": (D(50), D(50))}
        prices = {"mid": 335, "bid": 334.9, "ask": 335.1}
        mdp = _make_mdp(balances=balances, prices=prices)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote="100", owned_base="0")
        return ctrl, mdp, balances

    def test_transient_overclaim_single_evaluation_no_warning(self):
        """The 2026-07-14 13:35 production incident: the WS balance delta lands one evaluation
        before the fill reaches the ledger; the over-claim self-heals by the next cycle.
        Pre-fix, this single evaluation fired the warning."""
        ctrl, mdp, balances = self._overclaim_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # one over-claim evaluation
        balances["USDT"] = (D(100), D(100))                  # wallet catches up (self-heal)
        self._cycle(ctrl, mdp, 1001.0)
        self.assertEqual([], self._events(ctrl, OVERCLAIM_EVENT))
        self.assertIsNone(ctrl._overclaim_warn_since)
        self.assertEqual(0, ctrl._overclaim_warn_streak)

    def test_persistent_overclaim_two_consecutive_evaluations_warns(self):
        ctrl, mdp, _ = self._overclaim_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # observed, grace pending
        self.assertEqual([], self._events(ctrl, OVERCLAIM_EVENT))
        self._cycle(ctrl, mdp, 1001.0)                      # second consecutive evaluation
        self.assertEqual(1, len(self._events(ctrl, OVERCLAIM_EVENT)))

    def test_persistent_overclaim_past_grace_seconds_warns(self):
        ctrl, mdp, _ = self._overclaim_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + GRACE_S + 1)        # >10s persisted
        self.assertEqual(1, len(self._events(ctrl, OVERCLAIM_EVENT)))

    def test_clear_cycle_resets_the_persistence_state(self):
        ctrl, mdp, balances = self._overclaim_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # over-claim observed
        balances["USDT"] = (D(100), D(100))
        self._cycle(ctrl, mdp, 1001.0)                      # clears -> state reset
        balances["USDT"] = (D(50), D(50))
        self._cycle(ctrl, mdp, 1002.0)                      # re-observed: streak restarts at 1
        self.assertEqual([], self._events(ctrl, OVERCLAIM_EVENT))
        self._cycle(ctrl, mdp, 1003.0)                      # streak 2 -> warns
        self.assertEqual(1, len(self._events(ctrl, OVERCLAIM_EVENT)))

    def test_settling_freezes_the_persistence_state(self):
        ctrl, mdp, _ = self._overclaim_ctrl()
        self._cycle(ctrl, mdp, 1000.0)                      # streak 1
        mdp.get_connector.return_value.is_balance_settling = True
        self._cycle(ctrl, mdp, 1001.0)                      # settling -> frozen, no warn
        self.assertEqual([], self._events(ctrl, OVERCLAIM_EVENT))
        self.assertEqual(1, ctrl._overclaim_warn_streak)
        mdp.get_connector.return_value.is_balance_settling = False
        self._cycle(ctrl, mdp, 1002.0)                      # resumes: streak 2 -> warns
        self.assertEqual(1, len(self._events(ctrl, OVERCLAIM_EVENT)))


# =================================================== LOG-2': external holds in the understatement check

class TestUnderstatementExternalHolds(_Harness):

    def _surplus_ctrl(self, *, owned_quote="100", wallet_quote="200",
                      wallet_base="0", owned_base="0"):
        balances = {"XMR": (D(wallet_base), D(wallet_base)),
                    "USDT": (D(wallet_quote), D(wallet_quote))}
        prices = {"mid": 335, "bid": 334.9, "ask": 335.1}
        mdp = _make_mdp(balances=balances, prices=prices)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote=owned_quote, owned_base=owned_base)
        return ctrl, mdp

    def test_silent_with_matching_external_holds(self):
        """The production LOG-2 signature: ~two manual orphan orders holding exactly the
        'understated' 100 quote. With the holds subtracted, the surplus is zero -- the
        persistence timer never even starts."""
        ctrl, mdp = self._surplus_ctrl()
        _install_external_holds(mdp, {"quote": D(100), "base": D(0)})
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 100)
        self.assertEqual([], self._events(ctrl, UNDERSTATEMENT_EVENT))
        self.assertIsNone(ctrl._understatement_since)
        self.assertEqual(D(0), ctrl.processed_data["ledger_surplus_quote"])

    def test_still_fires_when_residual_exceeds_holds(self):
        """A genuine residual drift above the holds must keep warning."""
        ctrl, mdp = self._surplus_ctrl(owned_quote="80")     # wallet 200, ledger 80, holds 100
        _install_external_holds(mdp, {"quote": D(100), "base": D(0)})
        self._cycle(ctrl, mdp, 1000.0)                       # residual 20 -> timer starts
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)           # persistence met -> warns
        events = self._events(ctrl, UNDERSTATEMENT_EVENT)
        self.assertEqual(1, len(events))
        self.assertEqual("20", events[0].kwargs["surplus_quote"])
        self.assertEqual("100", events[0].kwargs["external_holds_quote"])

    def test_base_side_holds_subtracted(self):
        # wallet base 1 all held by a manual sell -> no surplus.
        ctrl, mdp = self._surplus_ctrl(wallet_quote="100", wallet_base="1")
        _install_external_holds(mdp, {"quote": D(0), "base": D(1)})
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(0), ctrl.processed_data["ledger_surplus_quote"])

    def test_degrades_to_zeros_without_the_connector_api(self):
        """Kraken (no external_order_holds) and MagicMock connectors both resolve to zeros ==
        pre-fix behavior."""
        ctrl, mdp = self._surplus_ctrl()
        # default MagicMock connector: auto-generated return value is not a dict -> zeros
        self.assertEqual((D(0), D(0)), ctrl._external_order_holds())
        # attribute present but not callable -> zeros
        mdp.get_connector.return_value.external_order_holds = None
        self.assertEqual((D(0), D(0)), ctrl._external_order_holds())
        # connector lookup raising -> zeros
        mdp.get_connector.side_effect = RuntimeError("down")
        self.assertEqual((D(0), D(0)), ctrl._external_order_holds())
        mdp.get_connector.side_effect = None
        # and the surplus math is therefore unchanged: full 100 surplus visible
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(100), ctrl.processed_data["ledger_surplus_quote"])

    def test_sanitizes_non_decimal_and_non_finite_values(self):
        ctrl, mdp = self._surplus_ctrl()
        _install_external_holds(mdp, {"quote": "100", "base": D("NaN")})
        self.assertEqual((D(0), D(0)), ctrl._external_order_holds())
        _install_external_holds(mdp, {"quote": D("-5"), "base": D("Infinity")})
        self.assertEqual((D(0), D(0)), ctrl._external_order_holds())
        _install_external_holds(mdp, {"quote": D("12.5")})   # missing key -> 0 for base
        self.assertEqual((D("12.5"), D(0)), ctrl._external_order_holds())


if __name__ == "__main__":
    unittest.main()
