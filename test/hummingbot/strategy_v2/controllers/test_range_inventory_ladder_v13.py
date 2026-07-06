"""v13 behavior-contract tests: robust per-order fill booking (Part A), self-growing managed
fund + deposit-ignore + withdrawal (Part B), guarded one-shot re-seed (Part C), the booked-fill
recycle trigger, and regression.

Every test drives the REAL controller. Fills are supplied either via an executor's custom_info
(the OrderExecutor accounting channel) or via a fake in-flight order wired into the connector mock
(`executed_amount_base` / `executed_amount_quote` / `order_fills` / `cumulative_fee_paid`), so the
source-robust booking paths are exercised without a live exchange.
"""
import asyncio
import json
import sys
import tempfile
import types
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _make_mdp(*, balances, mid, bid, ask, now=1000.0, in_flight=None):
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
    connector.in_flight_orders = in_flight if in_flight is not None else {}
    mdp.get_connector.return_value = connector
    return mdp


# --- fake order / fee objects (mimic InFlightOrder + TradeUpdate.fee.flat_fees) ----------------

def _flat_fee(token, amount):
    return types.SimpleNamespace(token=token, amount=Decimal(str(amount)))


def _trade_update(flat_fees):
    return types.SimpleNamespace(fee=types.SimpleNamespace(flat_fees=flat_fees))


def _fake_order(*, executed_base=None, executed_quote=None, order_fills=None,
                fee_paid_quote=None, cum_fees_quote=None, price=None):
    o = types.SimpleNamespace()
    if executed_base is not None:
        o.executed_amount_base = Decimal(str(executed_base))
    if executed_quote is not None:
        o.executed_amount_quote = Decimal(str(executed_quote))
    if order_fills is not None:
        o.order_fills = order_fills
    if cum_fees_quote is not None:
        o.cum_fees_quote = Decimal(str(cum_fees_quote))
    if price is not None:
        o.price = Decimal(str(price))
    if fee_paid_quote is not None:
        quote = Decimal(str(fee_paid_quote))
        o.cumulative_fee_paid = lambda token, _q=quote: (_q if token == "USDT" else Decimal("0"))
    return o


def _cust_exec(level_id, side, price, eid, *, filled_base=None, filled_quote=None,
               fees=None, active=False, close_ts=None, order_id=None):
    """Executor whose custom_info carries the fill (and optionally an order_id to look up an
    in-flight order registered on the connector mock)."""
    ex = MagicMock()
    ex.id = eid
    ex.status = RunnableStatus.RUNNING if active else RunnableStatus.TERMINATED
    ex.is_active = active
    ex.timestamp = 0.0
    ex.close_timestamp = close_ts
    ex.connector_name = "nonkyc"
    ci = {}
    if order_id is not None:
        ci["order_id"] = order_id
    if filled_base is not None:
        ci["filled_amount_base"] = Decimal(str(filled_base))
    if filled_quote is not None:
        ci["filled_amount_quote"] = Decimal(str(filled_quote))
    if fees is not None:
        ci["cum_fees_quote"] = Decimal(str(fees))
    ex.custom_info = ci
    ex.filled_amount_quote = Decimal("0")
    ex.cum_fees_quote = Decimal("0")
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = Decimal(str(price))
    cfg.amount = Decimal("1")
    ex.config = cfg
    return ex


class _V13Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        defaults = dict(
            id="ctrl-v13",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("300"),
            max_fund_value_quote=Decimal("1000"),
            buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
            buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
            sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
            min_order_quote=Decimal("5"),
            cooldown_time=3600,
            # v13 booking/fund tests predate the per-side refresh model; the recycle-window
            # trigger tests exercise the LEGACY recycle path. Pin the legacy mode so booking,
            # fund-growth and recycle assertions stay valid (booking itself is mode-independent).
            event_refresh_enabled=False,
            # Budget assertions here predate the buy-side fee haircut, which has its own suite
            # (test_range_inventory_ladder_fee_headroom.py) -- pin it off.
            fee_rate=Decimal("0"),
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

    def _set_state(self, ctrl, *, owned_quote, owned_base, seed_value="300",
                   progress=None, reserve_quote="0", reserve_base="0"):
        st = {
            "initialized": True,
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
        if progress is not None:
            st["booked_fill_progress"] = progress
        ctrl._state = st
        ctrl._state_loaded = True
        ctrl.executors_info = []
        ctrl.positions_held = []

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    def _full(self, ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())
        return ctrl.determine_executor_actions()

    @staticmethod
    def _emit_events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]

    def _book(self, ctrl):
        ctrl._book_fills_from_orders()


# ===================================================================== Part A: booking math

class TestPartABooking(_V13Harness):

    def _ctrl(self, **st):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, **st)
        return ctrl

    def test_full_buy_fill_booked_once(self):
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.1", filled_quote="32.1", fees="0.06")]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32.1") - D("0.06"))
        self.assertIn("B1", ctrl._state["booked_fill_progress"])
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fill_booked")), 1)
        # Re-running with no new execution books nothing (no double-count).
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fill_booked")), 1)

    def test_sell_fill_books_inverse(self):
        ctrl = self._ctrl(owned_quote="100", owned_base="0.5")
        ctrl.executors_info = [_cust_exec("sell_350", TradeType.SELL, "350", "S1",
                                          filled_base="0.08", filled_quote="27.72", fees="0.05")]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.42"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("127.67"))

    def test_partial_fills_across_cycles_each_increment_booked_once(self):
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ex = _cust_exec("buy_321", TradeType.BUY, "321", "B1",
                        filled_base="0.03", filled_quote="10", fees="0", active=True)
        ctrl.executors_info = [ex]
        self._book(ctrl)                                  # +0.03 / -10
        ex.custom_info["filled_amount_base"] = Decimal("0.06")
        ex.custom_info["filled_amount_quote"] = Decimal("20")
        self._book(ctrl)                                  # +0.03 / -10
        ex.custom_info["filled_amount_base"] = Decimal("0.1")
        ex.custom_info["filled_amount_quote"] = Decimal("33")
        self._book(ctrl)                                  # +0.04 / -13

        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))           # sum of increments
        self.assertEqual(D(ctrl._state["owned_quote"]), D("267"))         # 300 - 33
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fill_booked")), 3)

    def test_momentary_zero_read_does_not_unbook(self):
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ex = _cust_exec("buy_321", TradeType.BUY, "321", "B1",
                        filled_base="0.05", filled_quote="16", fees="0", active=True)
        ctrl.executors_info = [ex]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.05"))
        # Momentary glitch: the fill reads back as 0 -> must NOT un-book (monotonic guard).
        ex.custom_info["filled_amount_base"] = Decimal("0")
        ex.custom_info["filled_amount_quote"] = Decimal("0")
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.05"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("284"))

    def test_deposit_ignored_no_order_no_booking(self):
        # No executor execution -> a wallet deposit is invisible to per-order booking.
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1")]  # no fill fields
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0"))
        self.assertEqual(self._emit_events(ctrl, "range_ladder_fill_booked"), [])

    def test_final_capture_then_prune(self):
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ex = _cust_exec("buy_321", TradeType.BUY, "321", "B1",
                        filled_base="0.1", filled_quote="32", fees="0")
        ctrl.executors_info = [ex]
        self._book(ctrl)                                  # final capture booked
        self.assertIn("B1", ctrl._state["booked_fill_progress"])
        owned_after = ctrl._state["owned_base"]
        # Executor leaves executors_info -> its progress entry is pruned, ledger untouched.
        ctrl.executors_info = []
        self._book(ctrl)
        self.assertNotIn("B1", ctrl._state["booked_fill_progress"])
        self.assertEqual(ctrl._state["owned_base"], owned_after)


# ===================================================================== Part A: fees

class TestPartAFees(_V13Harness):

    def _ctrl(self, in_flight=None, **st):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=335, bid=334.9, ask=335.1, in_flight=in_flight)
        ctrl = self._build(mdp)
        self._set_state(ctrl, **st)
        return ctrl

    def test_fee_reduces_fund_on_buy(self):
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.1", filled_quote="32", fees="0.064")]
        self._book(ctrl)
        # owned_quote lower by quote AND fee.
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.064"))

    def test_fee_fallback_used_when_connector_reports_none(self):
        # custom_info carries no fee and there is no in-flight order -> derive from fee_rate.
        in_flight = {}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.config.fee_rate = Decimal("0.002")  # harness pins 0; this test IS about the fallback
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.1", filled_quote="32")]  # no fees key
        self._book(ctrl)
        expected_fee = D("32") * D("0.002")
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - expected_fee)
        ev = self._emit_events(ctrl, "range_ladder_fill_booked")[0]
        self.assertEqual(D(ev.kwargs["d_fees"]), expected_fee)

    def test_actual_connector_fee_used_not_fallback(self):
        # In-flight order exposes order_fills with a real quote fee -> use it, not fee_rate.
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32",
                                        order_fills={"t1": _trade_update([_flat_fee("USDT", "0.5")])})}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.5"))

    def test_cumulative_fee_paid_method_used(self):
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32", fee_paid_quote="0.4")}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.4"))

    def test_alternate_fee_asset_not_subtracted_but_recorded(self):
        # Fee charged in a THIRD asset (BNB) -> must not touch owned_quote/owned_base.
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32",
                                        order_fills={"t1": _trade_update([_flat_fee("BNB", "0.001")])})}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        self._book(ctrl)
        # No quote fee subtracted (only the quote itself).
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32"))
        ev = self._emit_events(ctrl, "range_ladder_fill_booked")[0]
        self.assertIn("BNB", ev.kwargs["alt_fees"])
        self.assertEqual(D(ev.kwargs["alt_fees"]["BNB"]), D("0.001"))


# ===================================================================== Part A: source robustness

class TestPartASourceRobustness(_V13Harness):

    def _ctrl(self, in_flight=None, **st):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))},
                        mid=335, bid=334.9, ask=335.1, in_flight=in_flight)
        ctrl = self._build(mdp)
        self._set_state(ctrl, **st)
        return ctrl

    def test_in_flight_executed_amounts_preferred(self):
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32.5", cum_fees_quote="0")}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID",
                                          filled_base="999", filled_quote="999")]  # ignored: order wins
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32.5"))

    def test_quote_derived_when_connector_lacks_executed_quote(self):
        # Order exposes base but NOT executed_amount_quote -> derive base * limit price.
        in_flight = {"OID": _fake_order(executed_base="0.1", cum_fees_quote="0")}  # no executed_quote
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("0.1") * D("321"))

    def test_custom_info_used_when_no_in_flight(self):
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.1", filled_quote="32", fees="0")]
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))

    def test_base_derived_from_custom_info_quote_when_no_base(self):
        # Only filled_amount_quote present -> base derived = quote / price.
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_quote="32.1", fees="0")]  # no filled_amount_base
        self._book(ctrl)
        self.assertEqual(D(ctrl._state["owned_base"]), D("32.1") / D("321"))


# ===================================================================== Part B: fund growth

class TestPartBFundGrowth(_V13Harness):

    def _ctrl(self, *, usdt=(D(0), D(0)), xmr=(D(0), D(0)), max_fund="1000", **st):
        mdp = _make_mdp(balances={"XMR": xmr, "USDT": usdt}, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, max_fund_value_quote=Decimal(max_fund))
        self._set_state(ctrl, **st)
        return ctrl, mdp

    def test_profitable_round_trip_raises_ceiling(self):
        ctrl, mdp = self._ctrl(owned_quote="300", owned_base="0", seed_value="300")
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(ctrl.processed_data["deploy_ceiling_quote"], D("300"))
        # Round trip: buy 0.2 @ 60, then sell the 0.2 @ 120 -> net +60 realized.
        ctrl.executors_info = [
            _cust_exec("buy_315", TradeType.BUY, "315", "B1", filled_base="0.2", filled_quote="60", fees="0"),
            _cust_exec("sell_350", TradeType.SELL, "350", "S1", filled_base="0.2", filled_quote="120", fees="0"),
        ]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("360"))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0"))
        self.assertEqual(ctrl.processed_data["managed_fund_value_quote"], D("360"))
        self.assertEqual(ctrl.processed_data["deploy_ceiling_quote"], D("360"))

    def test_ceiling_capped_at_max_fund(self):
        ctrl, mdp = self._ctrl(owned_quote="300", owned_base="0", seed_value="300", max_fund="340")
        ctrl.executors_info = [
            _cust_exec("buy_315", TradeType.BUY, "315", "B1", filled_base="0.2", filled_quote="60", fees="0"),
            _cust_exec("sell_350", TradeType.SELL, "350", "S1", filled_base="0.2", filled_quote="120", fees="0"),
        ]
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("360"))
        self.assertEqual(ctrl.processed_data["deploy_ceiling_quote"], D("340"))  # capped

    def test_deposit_does_not_raise_ceiling(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote="300", owned_base="0", seed_value="300")
        self._cycle(ctrl, mdp, 1000.0)
        ceiling_before = ctrl.processed_data["deploy_ceiling_quote"]
        managed_before = ctrl.processed_data["managed_fund_value_quote"]
        # Deposit $1000 (no order execution).
        balances["USDT"] = (D(1300), D(1300))
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(ctrl.processed_data["managed_fund_value_quote"], managed_before)
        self.assertEqual(ctrl.processed_data["deploy_ceiling_quote"], ceiling_before)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))  # unchanged

    def test_withdrawal_shrinks_fund_via_reanchor(self):
        # owned 300 but wallet only holds 100 -> over-claim -> re-anchor down after grace.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._set_state(ctrl, owned_quote="300", owned_base="0", seed_value="300")
        self._cycle(ctrl, mdp, 1000.0)                 # over-claim observed
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))
        self._cycle(ctrl, mdp, 1020.0)                 # past grace -> re-anchor
        self.assertEqual(D(ctrl._state["owned_quote"]), D("100"))
        self.assertEqual(ctrl.processed_data["managed_fund_value_quote"], D("100"))

    def test_sizing_tracks_wallet_not_owned(self):
        # owned 500 but only 50 USDT available (total 600 -> no over-claim) -> free buy budget 50.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(600), D(50))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote="500", owned_base="0", seed_value="600")
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(ctrl.processed_data["free_buy_budget_quote"], D("50"))


# ===================================================================== Part C: re-seed

class TestPartCReseed(_V13Harness):

    def test_reseed_from_wallet_sets_baseline_and_clears_progress(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, total_amount_quote=Decimal("300"), reseed_fund_from_wallet_once=True)
        # Under-seeded live fund + stale progress.
        self._set_state(ctrl, owned_quote="193", owned_base="0", seed_value="193",
                        progress={"OLD": {"base": "1", "quote": "1", "fees": "0"}})
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))
        self.assertEqual(D(ctrl._state["seed_value_quote"]), D("300"))
        self.assertEqual(D(ctrl._state["initial_managed_quote"]), D("300"))
        self.assertEqual(ctrl._state["booked_fill_progress"], {})
        self.assertEqual(ctrl._state["tracked_fill_executor_ids"], [])
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fund_reseeded")), 1)

    def test_reseed_target_quote_overrides_total_amount(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, total_amount_quote=Decimal("300"),
                           reseed_fund_from_wallet_once=True, reseed_fund_target_quote=Decimal("600"))
        self._set_state(ctrl, owned_quote="193", owned_base="0", seed_value="193")
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("600"))
        self.assertEqual(D(ctrl._state["seed_value_quote"]), D("600"))

    def test_reseed_is_idempotent_then_rearms_on_generation_bump(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, total_amount_quote=Decimal("300"), reseed_fund_from_wallet_once=True)
        self._set_state(ctrl, owned_quote="193", owned_base="0", seed_value="193")

        self._cycle(ctrl, mdp, 1000.0)                 # re-seed #1
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fund_reseeded")), 1)
        # Mutate owned to prove a second cycle does NOT re-seed (token already applied).
        ctrl._state["owned_quote"] = "999"
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fund_reseeded")), 1)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("999"))  # untouched

        # Bump the generation -> re-arm exactly once.
        ctrl.config.reseed_generation = 1
        self._cycle(ctrl, mdp, 1020.0)
        self.assertEqual(len(self._emit_events(ctrl, "range_ladder_fund_reseeded")), 2)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))

    def test_no_reseed_when_flag_false(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, reseed_fund_from_wallet_once=False)
        self._set_state(ctrl, owned_quote="193", owned_base="0", seed_value="193")
        self._cycle(ctrl, mdp, 1000.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("193"))
        self.assertEqual(self._emit_events(ctrl, "range_ladder_fund_reseeded"), [])


# ===================================================================== recycle trigger (v13)

class TestV13RecycleTrigger(_V13Harness):

    def test_booked_sell_fill_opens_buy_window(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._set_state(ctrl, owned_quote="0", owned_base="1", seed_value="300")
        self._cycle(ctrl, mdp, 1000.0)
        ctrl.executors_info = [_cust_exec("sell_350", TradeType.SELL, "350", "S1",
                                          filled_base="0.1", filled_quote="35", fees="0.07")]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(ctrl._recycle_bypass_buy_until, 1070.0)
        self.assertEqual(ctrl._recycle_bypass_sell_until, 0.0)

    def test_booked_buy_fill_opens_sell_window(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._set_state(ctrl, owned_quote="300", owned_base="0", seed_value="300")
        self._cycle(ctrl, mdp, 1000.0)
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.1", filled_quote="32", fees="0.06")]
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(ctrl._recycle_bypass_sell_until, 1070.0)
        self.assertEqual(ctrl._recycle_bypass_buy_until, 0.0)

    def test_pure_deposit_opens_no_window(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, recycle_max_latency_seconds=60)
        self._set_state(ctrl, owned_quote="0", owned_base="0", seed_value="300")
        self._cycle(ctrl, mdp, 1000.0)
        balances["USDT"] = (D(1100), D(1100))          # one-sided deposit
        self._cycle(ctrl, mdp, 1010.0)
        self.assertEqual(ctrl._recycle_bypass_buy_until, 0.0)
        self.assertEqual(ctrl._recycle_bypass_sell_until, 0.0)
        self.assertEqual(self._emit_events(ctrl, "range_ladder_recycle_window_opened"), [])


# ===================================================================== regression

class TestV13Regression(_V13Harness):

    def test_schema_unchanged(self):
        self.assertEqual(RangeInventoryLadderController.STATE_SCHEMA_VERSION, 10)

    def test_v10_state_without_progress_loads_and_books_forward(self):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        # A genuine v10 state: has tracked_fill_executor_ids, NO booked_fill_progress.
        v10 = {
            "schema_version": 10,
            "controller_name": "range_inventory_ladder",
            "controller_type": "market_making",
            "controller_id": "ctrl-v13",
            "connector_name": "nonkyc",
            "trading_pair": "XMR-USDT",
            "base_asset": "XMR",
            "quote_asset": "USDT",
            "initialized": True,
            "reserve_quote_balance": "0",
            "reserve_base_balance": "0",
            "initial_managed_quote": "300",
            "initial_claimed_base_amount": "0",
            "initial_reference_price": "335",
            "initialized_timestamp": "1000",
            "owned_quote": "300",
            "owned_base": "0",
            "seed_value_quote": "300",
            "tracked_fill_executor_ids": ["legacy-1", "legacy-2"],
        }
        self._state_path.write_text(json.dumps(v10), encoding="utf-8")
        ctrl._load_state()
        self.assertIsNone(ctrl._state_recovery_reason)        # not quarantined
        self.assertNotIn("booked_fill_progress", ctrl._state)  # absent is fine
        # Books forward from the legacy state with no double-count or crash.
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.1", filled_quote="32", fees="0")]
        ctrl.positions_held = []
        ctrl._book_fills_from_orders()
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertIn("B1", ctrl._state["booked_fill_progress"])

    def test_idle_one_sided_produces_no_buys_no_crash(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D("0.04"), D("0.04"))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp)
        self._set_state(ctrl, owned_quote="300", owned_base="0", seed_value="300")
        asyncio.run(ctrl.update_processed_data())
        actions = ctrl.create_actions_proposal()
        self.assertEqual([], actions)


if __name__ == "__main__":
    unittest.main()
