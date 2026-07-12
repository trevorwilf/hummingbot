"""Ledger-understatement growth-gate tests (2026-07-12 fix).

Production emitted 272+ "possible ledger understatement" warnings per pair -- one every
~5 minutes -- on a surplus that was STABLE the whole time (11.18 USDT flat for 20+ hours).
The message itself says "investigate only if this grew after fills", so the fix baselines
the surplus at first detection (one WARNING), then re-warns only when the surplus grows
past the baseline by more than understatement_growth_threshold_quote (default =
ledger_reconcile_threshold_quote) AND a fill has been booked since the baseline was set.
Flat/shrinking surpluses stay silent at WARNING level; the jsonl diagnostic event keeps
flowing at a reduced cadence (once per 30 minutes) for offline analysis.
"""
import logging
import unittest

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_understatement_diag import (
    D,
    PERSIST_S,
    _Harness,
    _make_mdp,
)

EVENT = "range_ladder_ledger_understatement_suspected"
WARN_TEXT = "possible ledger understatement"


class _WarnCatcher(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        if WARN_TEXT in record.getMessage():
            self.records.append(record)


class _GateHarness(_Harness):

    def _gate_ctrl(self, *, owned_quote="100", wallet_quote="111.18"):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(wallet_quote), D(wallet_quote))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote=owned_quote, owned_base="0")
        catcher = _WarnCatcher()
        ctrl.logger().setLevel(1)
        ctrl.logger().addHandler(catcher)
        self.addCleanup(ctrl.logger().removeHandler, catcher)
        return ctrl, mdp, balances, catcher


class TestUnderstatementGrowthGate(_GateHarness):

    def test_required_regression_stable_then_growth_then_flat(self):
        """The mandated regression: stable 11.18 surplus for 100 cycles -> exactly 1
        WARNING; a fill followed by +2.0 growth -> exactly 1 more WARNING and the baseline
        moves; flat again for 100 cycles -> 0 further WARNINGs."""
        ctrl, mdp, balances, catcher = self._gate_ctrl()

        # Phase 1: 100 evaluation cycles, 60s apart, surplus flat at 11.18.
        for i in range(100):
            self._cycle(ctrl, mdp, 1000.0 + i * 60.0)
        self.assertEqual(1, len(catcher.records))
        self.assertEqual(D("11.18"), ctrl._understatement_baseline)

        # Phase 2: a fill books, then the surplus grows by +2.0 (a missed-fill pattern).
        # The evaluation cycle runs past the 90s fill-settle grace, which defers all
        # ledger/wallet checks right after a booked fill.
        t_fill = 1000.0 + 100 * 60.0
        ctrl._last_fill_booked_ts = t_fill
        balances["USDT"] = (D("113.18"), D("113.18"))
        self._cycle(ctrl, mdp, t_fill + 120.0)
        self.assertEqual(2, len(catcher.records))
        self.assertEqual(D("13.18"), ctrl._understatement_baseline)

        # Phase 3: flat at the new level for 100 more cycles -> silence.
        for i in range(100):
            self._cycle(ctrl, mdp, t_fill + 180.0 + i * 60.0)
        self.assertEqual(2, len(catcher.records))

        # The warnings map 1:1 onto warn_kind-carrying events.
        kinds = [e.kwargs["warn_kind"] for e in self._events(ctrl, EVENT)]
        self.assertEqual(1, kinds.count("baseline"))
        self.assertEqual(1, kinds.count("growth_after_fill"))

    def test_growth_without_a_fill_stays_silent(self):
        ctrl, mdp, balances, catcher = self._gate_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # baseline warning
        self.assertEqual(1, len(catcher.records))

        balances["USDT"] = (D("120"), D("120"))            # +8.82 growth, NO fill booked
        for i in range(10):
            self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 60.0 * (i + 1))
        self.assertEqual(1, len(catcher.records))

    def test_fill_without_growth_stays_silent(self):
        ctrl, mdp, balances, catcher = self._gate_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # baseline warning
        ctrl._last_fill_booked_ts = 1000.0 + PERSIST_S + 30.0
        for i in range(10):
            self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 60.0 * (i + 1))
        self.assertEqual(1, len(catcher.records))

    def test_growth_below_threshold_stays_silent(self):
        # Default growth threshold = ledger_reconcile_threshold_quote = 0.5.
        ctrl, mdp, balances, catcher = self._gate_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)
        ctrl._last_fill_booked_ts = 1000.0 + PERSIST_S + 30.0
        balances["USDT"] = (D("111.5"), D("111.5"))        # +0.32 <= 0.5
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 60.0)
        self.assertEqual(1, len(catcher.records))

    def test_configurable_growth_threshold(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D("111.18"), D("111.18"))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, understatement_growth_threshold_quote=D("5"))
        self._set_state(ctrl, owned_quote="100", owned_base="0")
        catcher = _WarnCatcher()
        ctrl.logger().setLevel(1)
        ctrl.logger().addHandler(catcher)
        self.addCleanup(ctrl.logger().removeHandler, catcher)

        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # baseline
        ctrl._last_fill_booked_ts = 1000.0 + PERSIST_S + 30.0
        balances["USDT"] = (D("113.18"), D("113.18"))      # +2.0 <= custom threshold 5
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 60.0)
        self.assertEqual(1, len(catcher.records))
        balances["USDT"] = (D("117.18"), D("117.18"))      # +6.0 > 5
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 120.0)
        self.assertEqual(2, len(catcher.records))

    def test_flat_surplus_keeps_reduced_cadence_jsonl_visibility(self):
        ctrl, mdp, balances, catcher = self._gate_ctrl()
        # ~6000s of flat surplus after the baseline warning.
        for i in range(100):
            self._cycle(ctrl, mdp, 1000.0 + i * 60.0)
        flat_events = [e for e in self._events(ctrl, EVENT)
                       if e.kwargs["warn_kind"] == "flat_no_warning"]
        # Baseline fires at ~PERSIST_S; flat events at most once per 1800s afterwards.
        span = 100 * 60.0 - PERSIST_S
        self.assertGreaterEqual(len(flat_events), 1)
        self.assertLessEqual(len(flat_events), int(span / 1800.0) + 1)

    def test_surplus_clearing_resets_the_baseline_for_a_new_episode(self):
        ctrl, mdp, balances, catcher = self._gate_ctrl()
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S)         # baseline warning #1
        self.assertEqual(1, len(catcher.records))

        balances["USDT"] = (D("100.3"), D("100.3"))        # below threshold -> episode ends
        self._cycle(ctrl, mdp, 1000.0 + PERSIST_S + 60.0)
        self.assertIsNone(ctrl._understatement_baseline)
        self.assertIsNone(ctrl._understatement_since)

        balances["USDT"] = (D("111.18"), D("111.18"))      # surplus re-emerges
        t0 = 1000.0 + PERSIST_S + 120.0
        self._cycle(ctrl, mdp, t0)
        self._cycle(ctrl, mdp, t0 + PERSIST_S)             # fresh persistence -> warning #2
        self.assertEqual(2, len(catcher.records))
        kinds = [e.kwargs["warn_kind"] for e in self._events(ctrl, EVENT)
                 if e.kwargs["warn_kind"] != "flat_no_warning"]
        self.assertEqual(["baseline", "baseline"], kinds)


if __name__ == "__main__":
    unittest.main()
