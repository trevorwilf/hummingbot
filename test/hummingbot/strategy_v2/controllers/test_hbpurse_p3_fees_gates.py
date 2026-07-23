"""hbpurse Phase 3 -- fee correctness (F6) + reseed freshness/plausibility (F19) + grace (F21)
+ legacy clamp visibility (F20) + init-time hold recording (F11).

Every test drives the REAL controller (booking loop / reseed / init / config), never a mock of
the method under test. Expected money values are DERIVED FROM THE FINDING/SPEC ARITHMETIC, never
captured by running the implementation. Each test is written to FAIL under the named single-line
mutation called out in its docstring (the TEST INTEGRITY AUDIT contract).

Harness mirrors test_range_inventory_ladder_v13.py (the booking/reseed suite).
"""
import asyncio
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

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


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
               fees=None, active=True, order_id=None):
    ex = MagicMock()
    ex.id = eid
    ex.status = RunnableStatus.RUNNING if active else RunnableStatus.TERMINATED
    ex.is_active = active
    ex.timestamp = 0.0
    ex.close_timestamp = None
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


def _order_exec(level_id, side, price, eid, status=RunnableStatus.RUNNING):
    ex = MagicMock()
    ex.id = eid
    ex.status = status
    ex.is_active = status in (RunnableStatus.NOT_STARTED, RunnableStatus.RUNNING)
    ex.timestamp = 0.0
    ex.close_timestamp = None
    ex.connector_name = "nonkyc"
    ex.custom_info = {}
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = D(price)
    cfg.amount = D("0.1")
    ex.config = cfg
    return ex


def _make_mdp(*, balances, mid=335, bid=334.9, ask=335.1, now=1000.0, in_flight=None):
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
    connector.in_flight_orders = in_flight if in_flight is not None else {}
    mdp.get_connector.return_value = connector
    return mdp


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **overrides):
        defaults = dict(
            id="ctrl-p3",
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
            event_refresh_enabled=False,
            fee_rate=Decimal("0"),
        )
        defaults.update(overrides)
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
                   reserve_quote="0", reserve_base="0"):
        ctrl._state = {
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
        ctrl._state_loaded = True
        ctrl.executors_info = []
        ctrl.positions_held = []

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]


# ============================================================ F6: fee correctness

class TestF6FeeCorrectness(_Harness):
    """The fee path: a base ledger that never under-books fees."""

    def _ctrl(self, in_flight=None, fee_rate="0", **st):
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))}, in_flight=in_flight)
        ctrl = self._build(mdp, fee_rate=Decimal(str(fee_rate)))
        self._set_state(ctrl, **st)
        return ctrl

    def test_fills_present_zero_quote_fee_falls_through_to_percent_estimate(self):
        """Fills present but NO recognized fee -> percent estimate (fee_rate * exec_quote).
        MUTATION that this catches: restoring the pre-fix line that pins fee_in_quote=Decimal("0")
        the moment order_fills is non-empty (which blocks every fallback) -> fee 0, owned 268."""
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32",
                                        order_fills={"t1": _trade_update([])})}  # fills, no fee token
        ctrl = self._ctrl(in_flight=in_flight, fee_rate="0.002", owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        ctrl._book_fills_from_orders()
        # Spec: fee = fee_rate * exec_quote = 0.002 * 32 = 0.064.
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.064"))
        ev = self._events(ctrl, "range_ladder_fill_booked")[0]
        self.assertEqual(D(ev.kwargs["d_fees"]), D("0.064"))

    def test_base_denominated_fee_debits_quote_at_order_price(self):
        """A base-asset (XMR) fee is valued at the order price into quote and debited.
        MUTATION: dropping the base-fee conversion (base_fee_in_quote stays 0) -> recognized 0,
        fall through to percent (fee_rate 0) -> fee 0, owned 268 (not 267.679)."""
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32",
                                        order_fills={"t1": _trade_update([_flat_fee("XMR", "0.001")])})}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        ctrl._book_fills_from_orders()
        # Spec: base fee 0.001 XMR * order_price 321 = 0.321 quote.
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.321"))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))  # base fee does NOT touch owned_base
        ev = self._events(ctrl, "range_ladder_fill_booked")[0]
        self.assertEqual(D(ev.kwargs["alt_fees"]["XMR"]), D("0.001"))  # native amount still visible

    def test_fee_only_delta_books_the_fee_debit(self):
        """A late-reported fee on an already-booked fill (d_fees>0, d_base==d_quote==0) must BOOK.
        MUTATION: reverting the guard to `if d_base<=0 and d_quote<=0: continue` (dropping the
        d_fees term) -> the fee-only cycle early-continues, owned stays 268 (not 267.95)."""
        ctrl = self._ctrl(owned_quote="300", owned_base="0")
        ex = _cust_exec("buy_321", TradeType.BUY, "321", "B1",
                        filled_base="0.1", filled_quote="32", fees="0")
        ctrl.executors_info = [ex]
        ctrl._book_fills_from_orders()                       # cycle 1: base+quote, fee 0
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32"))
        ex.custom_info["cum_fees_quote"] = Decimal("0.05")   # fee arrives late, same base/quote
        ctrl._book_fills_from_orders()                       # cycle 2: fee-only delta
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.05"))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))  # base unchanged by the fee delta

    def test_flat_quote_fee_books_byte_identically(self):
        """Regression guard: the live NonKYC path (a flat QUOTE fee on the fill) is unchanged.
        MUTATION: forcing the fall-through even when recognized_fee>0 (e.g. `if False:` on the
        recognized branch) -> fee goes to the 0 percent estimate, owned 268 (not 267.5)."""
        in_flight = {"OID": _fake_order(executed_base="0.1", executed_quote="32",
                                        order_fills={"t1": _trade_update([_flat_fee("USDT", "0.5")])})}
        ctrl = self._ctrl(in_flight=in_flight, owned_quote="300", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1", order_id="OID")]
        ctrl._book_fills_from_orders()
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300") - D("32") - D("0.5"))


# ============================================================ F19: reseed freshness + plausibility

class TestF19ReseedGates(_Harness):

    def _armed_ctrl(self, balances, **overrides):
        mdp = _make_mdp(balances=balances)
        ctrl = self._build(mdp, total_amount_quote=Decimal("300"),
                           reseed_fund_from_wallet_once=True, **overrides)
        self._set_state(ctrl, owned_quote="193", owned_base="0", seed_value="193")
        return ctrl, mdp

    def test_implausible_claim_defers_without_consuming_token(self):
        """A wallet claim below reseed_min_claim_fraction*target must DEFER, token NOT consumed.
        MUTATION: deleting the plausibility gate -> the 100 claim applies, owned becomes 100
        (not the pinned 193) and last_reseed_token is set."""
        # target=300, floor=0.5*300=150; wallet available 100 -> seed_value 100 < 150.
        ctrl, mdp = self._armed_ctrl({"XMR": (D(0), D(0)), "USDT": (D(100), D(100))})
        ctrl._maybe_reseed_fund(Decimal("335"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("193"))          # NOT re-seeded
        self.assertNotIn("last_reseed_token", ctrl._state)                 # token NOT consumed
        self.assertEqual(self._events(ctrl, "range_ladder_fund_reseeded"), [])
        deferred = self._events(ctrl, "range_ladder_reseed_deferred_implausible_claim")
        self.assertEqual(1, len(deferred))
        self.assertEqual(D(deferred[0].kwargs["seed_value_quote"]), D("100"))
        self.assertEqual(D(deferred[0].kwargs["min_claim_quote"]), D("150"))

    def test_plausible_claim_applies(self):
        """A claim at/above the floor applies and consumes the token."""
        ctrl, mdp = self._armed_ctrl({"XMR": (D(0), D(0)), "USDT": (D(300), D(300))})
        ctrl._maybe_reseed_fund(Decimal("335"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))          # re-seeded to wallet claim
        self.assertEqual(ctrl._state["last_reseed_token"], "0:300")        # token consumed
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_fund_reseeded")))
        self.assertEqual(self._events(ctrl, "range_ladder_reseed_deferred_implausible_claim"), [])

    def test_zero_fraction_disables_the_floor(self):
        """reseed_min_claim_fraction=0 lets any positive claim apply (operator escape hatch)."""
        ctrl, mdp = self._armed_ctrl({"XMR": (D(0), D(0)), "USDT": (D(100), D(100))},
                                     reseed_min_claim_fraction=Decimal("0"))
        ctrl._maybe_reseed_fund(Decimal("335"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("100"))          # applies with floor off
        self.assertEqual(ctrl._state["last_reseed_token"], "0:300")

    def test_balance_settling_defers_freshness_without_consuming_token(self):
        """A balance sync in flight defers the re-seed (fresh-wallet gate), token NOT consumed.
        MUTATION: deleting the freshness gate -> the re-seed applies mid-settle, owned 300, token
        consumed -- so this test (asserting 193 + no token) fails."""
        ctrl, mdp = self._armed_ctrl({"XMR": (D(0), D(0)), "USDT": (D(300), D(300))})
        mdp.get_connector.return_value.is_balance_settling = True
        ctrl._maybe_reseed_fund(Decimal("335"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("193"))          # NOT re-seeded
        self.assertNotIn("last_reseed_token", ctrl._state)                 # token NOT consumed
        self.assertEqual(self._events(ctrl, "range_ladder_fund_reseeded"), [])
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_reseed_deferred_unsettled")))

    def test_within_fill_settle_grace_defers_freshness(self):
        """A fill booked within the settle-grace window defers the re-seed."""
        ctrl, mdp = self._armed_ctrl({"XMR": (D(0), D(0)), "USDT": (D(300), D(300))})
        ctrl._last_fill_booked_ts = 999.0  # 1s before now (1000) -> within 150s grace
        ctrl._maybe_reseed_fund(Decimal("335"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D("193"))
        self.assertNotIn("last_reseed_token", ctrl._state)
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_reseed_deferred_unsettled")))


# ============================================================ F21: grace default

class TestF21GraceDefault(_Harness):

    def test_grace_default_is_150(self):
        """MUTATION: reverting the field default to 90 -> this assertion fails."""
        cfg = RangeInventoryLadderConfig(
            id="t", controller_name="range_inventory_ladder", controller_type="market_making",
            connector_name="kraken", trading_pair="XMR-USD", total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("300")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")])
        self.assertEqual(150, cfg.fill_settle_grace_seconds)

    def test_explicit_90_is_preserved(self):
        """A deployed yml that PINS 90 keeps 90 (explicit value overrides the new default)."""
        cfg = RangeInventoryLadderConfig(
            id="t", controller_name="range_inventory_ladder", controller_type="market_making",
            connector_name="kraken", trading_pair="XMR-USD", total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("300")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")],
            fill_settle_grace_seconds=90)
        self.assertEqual(90, cfg.fill_settle_grace_seconds)


# ============================================================ F20: legacy clamp visibility

class TestF20LegacyClampTruncation(_Harness):

    def _drive_negative_quote(self, *, ledger_funded_budgets):
        # owned_quote=3, a buy debits 10 (+0 fee) -> owned_quote -> -7 -> clamp 0, truncated 7 (>5 dust).
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))})
        ctrl = self._build(mdp, ledger_funded_budgets=ledger_funded_budgets)
        self._set_state(ctrl, owned_quote="3", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.03", filled_quote="10", fees="0")]
        ctrl._book_fills_from_orders()
        return ctrl

    def test_legacy_mode_emits_truncation_event(self):
        """MUTATION: deleting the range_ladder_legacy_clamp_truncation emit -> no event."""
        ctrl = self._drive_negative_quote(ledger_funded_budgets=False)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("0"))  # behavior unchanged (still clamps)
        events = self._events(ctrl, "range_ladder_legacy_clamp_truncation")
        self.assertEqual(1, len(events))
        self.assertEqual(D(events[0].kwargs["truncated_quote"]), D("7"))
        self.assertEqual(D(events[0].kwargs["pre_clamp_owned_quote"]), D("-7"))

    def test_ledger_funded_mode_never_emits(self):
        """MUTATION: removing the `if not self.config.ledger_funded_budgets` guard -> fires here."""
        ctrl = self._drive_negative_quote(ledger_funded_budgets=True)
        self.assertEqual(D(ctrl._state["owned_quote"]), D("0"))  # same clamp, no event
        self.assertEqual(self._events(ctrl, "range_ladder_legacy_clamp_truncation"), [])

    def test_dust_truncation_below_min_order_quote_stays_silent(self):
        # owned_quote=8, buy debits 10 -> owned -2 -> truncated 2 <= min_order_quote 5 -> no event.
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))})
        ctrl = self._build(mdp, ledger_funded_budgets=False)
        self._set_state(ctrl, owned_quote="8", owned_base="0")
        ctrl.executors_info = [_cust_exec("buy_321", TradeType.BUY, "321", "B1",
                                          filled_base="0.03", filled_quote="10", fees="0")]
        ctrl._book_fills_from_orders()
        self.assertEqual(self._events(ctrl, "range_ladder_legacy_clamp_truncation"), [])


# ============================================================ F11: init-time hold recording

class TestF11InitUnavailableHolds(_Harness):

    def test_init_with_holds_persists_keys_and_emits(self):
        """allow_initialize_with_unavailable_wallet_funds=true + material holds -> persist keys.
        MUTATION: deleting the two initial_state['init_unavailable_*'] assignments -> keys absent."""
        # USDT total 300 / available 200 -> unavailable 100 (>tolerance).
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(300), D(200))})
        ctrl = self._build(mdp, allow_initialize_with_unavailable_wallet_funds=True)
        self.assertTrue(ctrl._ensure_initialized(Decimal("335")))
        self.assertEqual(D(ctrl._state["init_unavailable_quote"]), D("100"))
        self.assertEqual(D(ctrl._state["init_unavailable_base"]), D("0"))
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_init_unavailable_holds_recorded")))

    def test_init_without_holds_omits_keys(self):
        """A clean init (fully available wallet) never materializes the hold keys."""
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(300), D(300))})
        ctrl = self._build(mdp, allow_initialize_with_unavailable_wallet_funds=True)
        self.assertTrue(ctrl._ensure_initialized(Decimal("335")))
        self.assertNotIn("init_unavailable_quote", ctrl._state)
        self.assertNotIn("init_unavailable_base", ctrl._state)
        self.assertEqual(self._events(ctrl, "range_ladder_init_unavailable_holds_recorded"), [])

    def test_default_is_not_flipped(self):
        """A6: the config default for allow_initialize_with_unavailable_wallet_funds is untouched."""
        cfg = RangeInventoryLadderConfig(
            id="t", controller_name="range_inventory_ladder", controller_type="market_making",
            connector_name="kraken", trading_pair="XMR-USD", total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("300")], buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("340")], sell_amounts_pct=[Decimal("1")])
        self.assertFalse(cfg.allow_initialize_with_unavailable_wallet_funds)

    def test_load_validation_accepts_and_rejects_hold_keys(self):
        """The optional hold keys follow the P2/CLA-M03 discipline: a clean file round-trips, a
        corrupt (non-numeric) value quarantines, a missing key is not materialized.
        MUTATION: dropping the init_unavailable validation loop -> the corrupt file loads clean."""
        mdp = _make_mdp(balances={"XMR": (D(0), D(0)), "USDT": (D(0), D(0))})
        ctrl = self._build(mdp)
        base = self._valid_state()

        ok = ctrl._validate_loaded_state({**base, "init_unavailable_quote": "12.5",
                                          "init_unavailable_base": "0"})
        self.assertEqual(ok["init_unavailable_quote"], "12.5")

        with self.assertRaises(ValueError):
            ctrl._validate_loaded_state({**base, "init_unavailable_quote": "not-a-number"})
        with self.assertRaises(ValueError):
            ctrl._validate_loaded_state({**base, "init_unavailable_base": "-1"})

        clean = ctrl._validate_loaded_state(dict(base))
        self.assertNotIn("init_unavailable_quote", clean)  # missing key is not materialized

    def _valid_state(self):
        return {
            "schema_version": 10,
            "controller_name": "range_inventory_ladder",
            "controller_type": "market_making",
            "controller_id": "ctrl-p3",
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
            "initialized_timestamp": "900",
            "owned_quote": "300",
            "owned_base": "0",
            "seed_value_quote": "300",
            "tracked_fill_executor_ids": [],
        }


if __name__ == "__main__":
    unittest.main()
