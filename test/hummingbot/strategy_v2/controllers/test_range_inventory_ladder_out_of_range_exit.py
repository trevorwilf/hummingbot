"""Out-of-range market exit tests (out_of_range_action='market_exit').

When the market moves entirely outside the ladder band, passive LIMIT_MAKER placement
filters every rung on the out-of-band side ("not_passive") and the side goes inert --
the controller holds inventory it would gladly exit into the move but places nothing.
The opt-in 'market_exit' action crosses the spread with ONE LIMIT order for the full
available side budget, priced at the nearest edge rung (worst-case slippage bound).

NOTE on the edge rung: buy_prices is validated highest-to-lowest, so the nearest edge
for below_buy_range (the LOWEST buy rung) is buy_prices[-1]; sell_prices is validated
lowest-to-highest, so the nearest edge for above_sell_range is sell_prices[-1].

Covered (Section 4 of the design):
 1/2. zero side budget -> no action
 3.   in-band regimes -> no action
 4.   dormant (default) -> no action in any regime
 5.   passive_order_placement=False -> exit suppressed (no double-spend)
 6.   live exit order (blocked level id) -> no duplicate
 7.   closed prior exit, still out of band, residual budget -> tops up next cycle
 8.   dust budget (below min notional) -> no action
 9.   raw regime out of band but unconfirmed (regime dwell) -> no action until confirmed
 10.  the in-band side keeps placing normally while the other side exits
 11.  exit issued amount never exceeds the side budget; invariant does not trip
 plus: validator accept/reject, hot-update without a config rebuild, observability.
"""
import unittest
from decimal import Decimal

from pydantic import ValidationError

from hummingbot.core.data_type.common import PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (
    D,
    _Harness,
    _make_mdp,
    _resting,
)

from range_inventory_ladder import RangeInventoryLadderConfig  # noqa: E402

BASE_OWNED = "1.06036199"
QUOTE_OWNED = "200"


def _set_prices(mdp, mid, bid, ask):
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]


class _OobHarness(_Harness):
    """Harness defaults (from _Harness._build): buy_prices 321/318/315 (highest->lowest),
    sell_prices 350/355/360 (lowest->highest), min_order_quote=5, event refresh ON."""

    def _base_funded(self, *, mid, bid, ask, base=BASE_OWNED, **overrides):
        balances = {"XMR": [D(base), D(base)], "USDT": [D(0), D(0)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=0, owned_base=base, seed_value=400)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    def _quote_funded(self, *, mid, bid, ask, quote=QUOTE_OWNED, **overrides):
        balances = {"XMR": [D(0), D(0)], "USDT": [D(quote), D(quote)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=quote, owned_base=0, seed_value=quote)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    def _dual_funded(self, *, mid, bid, ask, base=BASE_OWNED, quote=QUOTE_OWNED, **overrides):
        balances = {"XMR": [D(base), D(base)], "USDT": [D(quote), D(quote)]}
        mdp = _make_mdp(balances=balances, mid=mid, bid=bid, ask=ask)
        ctrl = self._build(mdp, **overrides)
        self._init_state(ctrl, owned_quote=quote, owned_base=base, seed_value=800)
        self._prime(ctrl, mdp, 1000.0)
        return ctrl, mdp, balances

    @staticmethod
    def _oob_creates(actions):
        return [a for a in actions
                if hasattr(a, "executor_config")
                and getattr(a.executor_config, "level_id", "") in ("buy_oob", "sell_oob")]


# ============================================================ firing cases

class TestSellExitAboveRange(_OobHarness):

    def test_one_crossing_limit_sell_at_highest_rung_for_full_base_budget(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        self.assertEqual("above_sell_range", ctrl.processed_data["price_regime"])
        actions = self._full(ctrl, mdp, 1001.0)
        oob = self._oob_creates(actions)
        self.assertEqual(1, len(oob))
        cfg = oob[0].executor_config
        self.assertEqual("sell_oob", cfg.level_id)
        self.assertEqual(TradeType.SELL, cfg.side)
        self.assertEqual(D("360"), cfg.price)  # sell_prices[-1] = highest rung
        self.assertEqual(ExecutionStrategy.LIMIT, cfg.execution_strategy)
        budget = ctrl.processed_data["free_sell_budget_base"]
        self.assertGreater(budget, D(0))
        self.assertEqual(ctrl._quantize_amount_down(budget), cfg.amount)
        # The rung price is a FLOOR below the current book: the order crosses.
        self.assertLess(cfg.price, ctrl.processed_data["best_bid"])
        # No further creates: every sell rung is not_passive, buy side has no funds.
        self.assertEqual(1, len(self._creates(actions)))

    def test_structured_event_emitted_with_full_context(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        self._full(ctrl, mdp, 1001.0)
        events = self._events(ctrl, "range_ladder_out_of_range_exit")
        self.assertEqual(1, len(events))
        kw = events[0].kwargs
        self.assertEqual("sell", kw["side"])
        self.assertEqual("above_sell_range", kw["regime"])
        self.assertEqual("360", kw["limit_price"])
        self.assertEqual("370", kw["reference_price"])
        self.assertEqual("369.9", kw["best_bid"])
        self.assertEqual("370.1", kw["best_ask"])
        self.assertEqual(str(ctrl.processed_data["free_sell_budget_base"]), kw["budget"])
        expected_amount = ctrl._quantize_amount_down(ctrl.processed_data["free_sell_budget_base"])
        self.assertEqual(str(expected_amount), kw["amount"])

    def test_exit_amount_never_exceeds_side_budget_and_invariant_silent(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        actions = self._full(ctrl, mdp, 1001.0)
        oob = self._oob_creates(actions)
        self.assertEqual(1, len(oob))
        self.assertLessEqual(oob[0].executor_config.amount,
                             ctrl.processed_data["free_sell_budget_base"])
        self.assertEqual([], self._events(ctrl, "range_ladder_plan_budget_invariant_violation"))


class TestBuyExitBelowRange(_OobHarness):

    def test_one_crossing_limit_buy_at_lowest_rung_within_quote_budget(self):
        ctrl, mdp, _ = self._quote_funded(mid=300, bid=299.9, ask=300.1,
                                          out_of_range_action="market_exit")
        self.assertEqual("below_buy_range", ctrl.processed_data["price_regime"])
        actions = self._full(ctrl, mdp, 1001.0)
        oob = self._oob_creates(actions)
        self.assertEqual(1, len(oob))
        cfg = oob[0].executor_config
        self.assertEqual("buy_oob", cfg.level_id)
        self.assertEqual(TradeType.BUY, cfg.side)
        self.assertEqual(D("315"), cfg.price)  # buy_prices[-1] = LOWEST rung (nearest edge)
        self.assertEqual(ExecutionStrategy.LIMIT, cfg.execution_strategy)
        budget = ctrl.processed_data["free_buy_budget_quote"]
        self.assertGreater(budget, D(0))
        self.assertLessEqual(cfg.amount * cfg.price, budget)
        # The rung price is a CEILING above the current book: the order crosses.
        self.assertGreater(cfg.price, ctrl.processed_data["best_ask"])
        events = self._events(ctrl, "range_ladder_out_of_range_exit")
        self.assertEqual(1, len(events))
        self.assertEqual("buy", events[0].kwargs["side"])
        self.assertEqual("below_buy_range", events[0].kwargs["regime"])
        self.assertEqual([], self._events(ctrl, "range_ladder_plan_budget_invariant_violation"))


class TestInBandSideKeepsWorking(_OobHarness):

    def test_buy_ladder_still_places_while_sell_side_exits(self):
        ctrl, mdp, _ = self._dual_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        ctrl._buy_side_dirty = True
        ctrl._buy_dirty_reason = "startup"
        actions = self._full(ctrl, mdp, 1001.0)
        creates = self._creates(actions)
        buy_levels = [a.executor_config.level_id for a in creates
                      if a.executor_config.side == TradeType.BUY]
        self.assertIn("buy_321", buy_levels)  # normal rungs rest below the market
        self.assertNotIn("buy_oob", buy_levels)
        sell_levels = [a.executor_config.level_id for a in creates
                       if a.executor_config.side == TradeType.SELL]
        self.assertEqual(["sell_oob"], sell_levels)  # only the exit on the OOB side


# ============================================================ non-firing cases

class TestDormantDefault(_OobHarness):

    def test_default_is_dormant(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1)
        self.assertEqual("dormant", ctrl.config.out_of_range_action)

    def test_dormant_never_exits_in_any_regime(self):
        for mid, bid, ask, regime in (
            (370, 369.9, 370.1, "above_sell_range"),
            (300, 299.9, 300.1, "below_buy_range"),
            (335, 334.9, 335.1, "between_ladders"),
            (318, 317.9, 318.1, "inside_buy_band"),
            (355, 354.9, 355.1, "inside_sell_band"),
        ):
            with self.subTest(regime=regime):
                ctrl, mdp, _ = self._dual_funded(mid=mid, bid=bid, ask=ask,
                                                 out_of_range_action="dormant")
                self.assertEqual(regime, ctrl.processed_data["price_regime"])
                actions = self._full(ctrl, mdp, 1001.0)
                self.assertEqual([], self._oob_creates(actions))
                self.assertEqual([], self._events(ctrl, "range_ladder_out_of_range_exit"))

    def test_dormant_above_range_sell_side_stays_inert(self):
        """Byte-for-byte default behavior: above the band, a base-only fund places NOTHING."""
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1)
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "startup"
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._creates(actions))


class TestInBandRegimesNoAction(_OobHarness):

    def test_market_exit_in_band_no_action(self):
        for mid, bid, ask, regime in (
            (335, 334.9, 335.1, "between_ladders"),
            (318, 317.9, 318.1, "inside_buy_band"),
            (355, 354.9, 355.1, "inside_sell_band"),
        ):
            with self.subTest(regime=regime):
                ctrl, mdp, _ = self._dual_funded(mid=mid, bid=bid, ask=ask,
                                                 out_of_range_action="market_exit")
                self.assertEqual(regime, ctrl.processed_data["price_regime"])
                actions = self._full(ctrl, mdp, 1001.0)
                self.assertEqual([], self._oob_creates(actions))


class TestPassivePlacementOffSuppressesExit(_OobHarness):

    def test_no_exit_when_passive_placement_disabled(self):
        """passive_order_placement=False: the normal ladder already places crossing LIMIT
        orders itself -- the exit must stay silent or the same budget is spent twice."""
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit",
                                         passive_order_placement=False)
        ctrl._sell_side_dirty = True
        ctrl._sell_dirty_reason = "startup"
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        self.assertEqual([], self._events(ctrl, "range_ladder_out_of_range_exit"))
        # The normal ladder DID place the crossing sells (that is the no-double-spend point).
        sells = [a for a in self._creates(actions) if a.executor_config.side == TradeType.SELL]
        self.assertGreater(len(sells), 0)
        issued_base = sum((a.executor_config.amount for a in sells), Decimal("0"))
        self.assertLessEqual(issued_base, ctrl.processed_data["free_sell_budget_base"])


class TestDuplicateSuppression(_OobHarness):

    def test_live_exit_order_blocks_a_duplicate(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        live = _resting("sell_oob", TradeType.SELL, "360", BASE_OWNED, "x0",
                        status=RunnableStatus.RUNNING)
        ctrl.executors_info = [live]
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))

    def test_shutting_down_exit_order_still_blocks(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        live = _resting("sell_oob", TradeType.SELL, "360", BASE_OWNED, "x0",
                        status=RunnableStatus.SHUTTING_DOWN)
        ctrl.executors_info = [live]
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))


class TestPartialFillTopUp(_OobHarness):

    def test_closed_exit_retries_while_still_out_of_band(self):
        """Event-refresh mode: a CLOSED exit level carries no cooldown, so an exit that only
        partially filled tops up on the next cycle from the residual budget."""
        residual = "0.40000000"
        ctrl, mdp, balances = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                                out_of_range_action="market_exit")
        closed = _resting("sell_oob", TradeType.SELL, "360", BASE_OWNED, "x0",
                          status=RunnableStatus.TERMINATED, close_ts=1000.5)
        ctrl.executors_info = [closed]
        # Simulate the partial fill having consumed part of the base.
        balances["XMR"] = [D(residual), D(residual)]
        ctrl._state["owned_base"] = residual
        actions = self._full(ctrl, mdp, 1001.0)
        oob = self._oob_creates(actions)
        self.assertEqual(1, len(oob))
        cfg = oob[0].executor_config
        self.assertEqual("sell_oob", cfg.level_id)
        budget = ctrl.processed_data["free_sell_budget_base"]
        self.assertGreater(budget, D(0))
        self.assertEqual(ctrl._quantize_amount_down(budget), cfg.amount)


class TestBudgetEdgeCases(_OobHarness):

    def test_above_range_with_zero_base_budget_no_action(self):
        ctrl, mdp, _ = self._quote_funded(mid=370, bid=369.9, ask=370.1,
                                          out_of_range_action="market_exit")
        self.assertEqual("above_sell_range", ctrl.processed_data["price_regime"])
        self.assertEqual(D(0), ctrl.processed_data["free_sell_budget_base"])
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        self.assertEqual([], self._events(ctrl, "range_ladder_out_of_range_exit"))

    def test_below_range_with_zero_quote_budget_no_action(self):
        ctrl, mdp, _ = self._base_funded(mid=300, bid=299.9, ask=300.1,
                                         out_of_range_action="market_exit")
        self.assertEqual("below_buy_range", ctrl.processed_data["price_regime"])
        self.assertEqual(D(0), ctrl.processed_data["free_buy_budget_quote"])
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))

    def test_dust_budget_below_min_notional_no_action(self):
        # 0.01 XMR * 360 = 3.6 quote < min_order_quote 5 -> the builder's quantization
        # guard yields no action (dust cannot be exited).
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1, base="0.01",
                                         out_of_range_action="market_exit")
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        self.assertEqual([], self._events(ctrl, "range_ladder_out_of_range_exit"))
        skips = self._events(ctrl, "range_ladder_sell_level_skipped_post_quantization")
        self.assertTrue(any(e.kwargs["level_id"] == "sell_oob" for e in skips))


class TestRegimeDwellGate(_OobHarness):

    def test_unconfirmed_regime_no_action_until_dwell_elapses(self):
        # Prime IN band so between_ladders is the confirmed regime, then jump the price
        # out of band: the raw regime flips immediately, the confirmed one only after
        # regime_dwell_seconds (default 30).
        ctrl, mdp, _ = self._base_funded(mid=335, bid=334.9, ask=335.1,
                                         out_of_range_action="market_exit")
        self.assertEqual("between_ladders", ctrl.processed_data["price_regime"])
        _set_prices(mdp, 370, 369.9, 370.1)

        actions = self._full(ctrl, mdp, 1001.0)   # raw=above_sell_range, unconfirmed
        self.assertEqual("between_ladders", ctrl.processed_data["price_regime"])
        self.assertEqual([], self._oob_creates(actions))

        actions = self._full(ctrl, mdp, 1020.0)   # still inside the 30s dwell
        self.assertEqual([], self._oob_creates(actions))

        actions = self._full(ctrl, mdp, 1032.0)   # dwell elapsed -> confirmed -> fire
        self.assertEqual("above_sell_range", ctrl.processed_data["price_regime"])
        oob = self._oob_creates(actions)
        self.assertEqual(1, len(oob))
        self.assertEqual("sell_oob", oob[0].executor_config.level_id)


class TestWaveGates(_OobHarness):

    def test_cancels_in_flight_defer_the_exit(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        shutting = _resting("sell_350", TradeType.SELL, "350", "0.5", "s0",
                            status=RunnableStatus.SHUTTING_DOWN)
        ctrl.executors_info = [shutting]
        ctrl._refresh_wave["sell"] = ctrl._new_wave_record(1000.0, D("0.5"), {"s0"})
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        # Once the wave's cancel closes, the exit fires on a later cycle.
        shutting.status = RunnableStatus.TERMINATED
        shutting.is_active = False
        shutting.close_timestamp = 1001.5
        ctrl._refresh_wave["sell"] = None  # wave resolved
        actions = self._full(ctrl, mdp, 1002.0)
        self.assertEqual(1, len(self._oob_creates(actions)))


# ============================================================ config plumbing

class TestOutOfRangeActionValidator(_OobHarness):

    def _cfg(self, value):
        return RangeInventoryLadderConfig(
            id="cfg-oob",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("200"),
            buy_prices=[D("321"), D("318"), D("315")],
            sell_prices=[D("350"), D("355"), D("360")],
            out_of_range_action=value,
        )

    def test_accepts_both_values_any_case_and_whitespace(self):
        for raw, expected in (
            ("dormant", "dormant"),
            ("market_exit", "market_exit"),
            ("MARKET_EXIT", "market_exit"),
            ("  Dormant  ", "dormant"),
            ("Market_Exit", "market_exit"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(expected, self._cfg(raw).out_of_range_action)

    def test_rejects_anything_else(self):
        for bad in ("exit", "market", "", "none", "market exit", "dormant,market_exit"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self._cfg(bad)


class TestHotUpdateNoRebuild(_OobHarness):

    def test_not_part_of_runtime_config_signature(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1)
        sig_before = ctrl._runtime_config_signature()
        ctrl.config.out_of_range_action = "market_exit"
        self.assertEqual(sig_before, ctrl._runtime_config_signature())

    def test_hot_enable_takes_effect_next_cycle_without_rebuild(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1)
        actions = self._full(ctrl, mdp, 1001.0)
        self.assertEqual([], self._oob_creates(actions))
        ctrl.config.out_of_range_action = "market_exit"
        actions = self._full(ctrl, mdp, 1002.0)
        self.assertEqual(1, len(self._oob_creates(actions)))
        self.assertFalse(ctrl._config_rebuild_pending)


class TestObservability(_OobHarness):

    def test_custom_info_reports_the_action(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        self.assertEqual("market_exit", ctrl.get_custom_info()["out_of_range_action"])

    def test_format_status_shows_armed_exit_when_out_of_band(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        status = "\n".join(ctrl.to_format_status())
        self.assertIn("Out-of-range exit: armed", status)
        self.assertIn("limit 360", status)

    def test_format_status_shows_placed_when_exit_live(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1,
                                         out_of_range_action="market_exit")
        ctrl.executors_info = [_resting("sell_oob", TradeType.SELL, "360", BASE_OWNED, "x0")]
        self._full(ctrl, mdp, 1001.0)
        status = "\n".join(ctrl.to_format_status())
        self.assertIn("Out-of-range exit: placed (live)", status)

    def test_format_status_silent_when_dormant_or_in_band(self):
        ctrl, mdp, _ = self._base_funded(mid=370, bid=369.9, ask=370.1)
        self.assertNotIn("Out-of-range exit", "\n".join(ctrl.to_format_status()))
        ctrl2, mdp2, _ = self._dual_funded(mid=335, bid=334.9, ask=335.1,
                                           out_of_range_action="market_exit")
        self.assertNotIn("Out-of-range exit", "\n".join(ctrl2.to_format_status()))


if __name__ == "__main__":
    unittest.main()
