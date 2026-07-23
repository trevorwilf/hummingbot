"""hbpurse Phase 1 behavior-contract tests: re-anchor trigger/cut symmetry (F4/CLA-M01),
re-anchor netting visibility (CLA-M02), and re-anchor/booking double-debit offset credits (F5).

Every test drives the REAL controller (`RangeInventoryLadderController`) with mocked balances,
prices, `time()` and `executors_info`, exactly like the v12/v13 suites. Every expected value is
derived BY HAND from the finding/spec arithmetic (HBPURSE_FINDINGS.md F4/F5, ADDENDUM A1),
never from running the implementation:

- A1 collapse trace: owned=600, stale reserve=400, wallet 570 -> the cut must land at
  min(600, 570) = 570 (wallet truth). The pre-fix arithmetic cut to
  min(600, max(0, 570 - 400)) = 170 -- these tests FAIL on that arithmetic.
- Offset credits: a re-anchor cut of X becomes a credit; a late fill's debit consumes
  min(debit, credit) instead of debiting owned_* a second time.
"""
import asyncio
import json
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
    """balances: {asset: (total, available)}. Mutate the dict between cycles to simulate
    fills/deposits/withdrawals; the side_effect lambdas read it live."""
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
        id="ctrl-hbpurse-p1",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        total_amount_quote=Decimal("170"),
        max_fund_value_quote=Decimal("100000"),
        buy_prices=[Decimal("321"), Decimal("318"), Decimal("315")],
        buy_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        sell_prices=[Decimal("350"), Decimal("355"), Decimal("360")],
        sell_amounts_pct=[Decimal("1"), Decimal("1"), Decimal("1")],
        min_order_quote=Decimal("5"),
        cooldown_time=3600,
        event_refresh_enabled=False,
        fee_rate=Decimal("0"),
    )
    defaults.update(overrides)
    return RangeInventoryLadderConfig(**defaults)


def _filling_executor(level_id, side, price, executor_id, *, filled_base, filled_quote,
                      fees="0", active=True):
    """An executor whose custom_info reports CUMULATIVE fills -- the v13 booking source."""
    ex = MagicMock()
    ex.id = executor_id
    ex.status = RunnableStatus.RUNNING if active else RunnableStatus.TERMINATED
    ex.is_active = active
    ex.timestamp = 0.0
    ex.close_timestamp = None
    ex.connector_name = "nonkyc"
    ex.custom_info = {
        "filled_amount_base": Decimal(str(filled_base)),
        "filled_amount_quote": Decimal(str(filled_quote)),
        "cum_fees_quote": Decimal(str(fees)),
    }
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


class _Harness(unittest.TestCase):
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
        controller.executors_info = []
        controller.positions_held = []
        return controller

    def _init_state(self, ctrl, *, owned_quote, owned_base, seed_value,
                    reserve_quote="0", reserve_base="0", **extra):
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
        ctrl._state.update(extra)
        ctrl._state_loaded = True

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    @staticmethod
    def _emit_events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]

    def _persisted(self):
        with self._state_path.open() as f:
            return json.load(f)


# ==================================================== F4 / CLA-M01: trigger/cut symmetry

class TestReanchorSymmetry(_Harness):

    def test_a1_collapse_trace_reanchors_to_wallet_truth(self):
        # THE arbitrated A1 trace: owned=600, stale reserve=400 (withdrawn from the wallet
        # long ago), wallet 570 after a 30 over-claim. Spec: cut = min(600, 570) = 570.
        # Pre-fix arithmetic: min(600, max(0, 570 - 400)) = 170 -- this test fails on it.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(570), D(570))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=600, owned_base=0, seed_value=600,
                         reserve_quote="400")

        self._cycle(ctrl, mdp, 1000.0)  # over-claim observed, grace timer starts
        self.assertEqual(D(ctrl._state["owned_quote"]), D(600))  # no cut within grace

        self._cycle(ctrl, mdp, 1020.0)  # 20s > 10s grace -> cut
        self.assertEqual(D(ctrl._state["owned_quote"]), D(570))
        self.assertEqual(D(ctrl._state["owned_base"]), D(0))

        events = self._emit_events(ctrl, "range_ladder_ledger_reanchored")
        self.assertEqual(1, len(events))
        self.assertEqual("600", events[0].kwargs["old_owned_quote"])
        self.assertEqual("570", events[0].kwargs["new_owned_quote"])

        persisted = self._persisted()
        self.assertEqual(D(persisted["owned_quote"]), D(570))
        # reserve_* remains persisted (diagnostics) -- inert, not deleted.
        self.assertEqual(D(persisted["reserve_quote_balance"]), D(400))

    def test_huge_stale_reserve_is_arithmetic_inert(self):
        # reserve larger than the whole wallet: pre-fix cut = max(0, 570-10000) = 0 on quote
        # and max(0, 1-5) = 0 on base -- a total collapse. Spec: quote -> 570, base -> 1.0.
        balances = {"XMR": (D("1.0"), D("1.0")), "USDT": (D(570), D(570))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=600, owned_base="1.0", seed_value=900,
                         reserve_quote="10000", reserve_base="5")

        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1020.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(570))
        self.assertEqual(D(ctrl._state["owned_base"]), D("1.0"))

    def test_quote_overclaim_never_cuts_base_below_its_own_wallet_bound(self):
        # Per-asset independence: the quote side over-claims by 50 (owned 100 vs total 50);
        # the base side is fully backed (owned 1.0 vs total 2.0). The stale base reserve of
        # 1.5 must NOT drag owned_base to min(1.0, 2.0-1.5) = 0.5 (pre-fix cross-asset
        # leakage). Spec: owned_base stays exactly 1.0; owned_quote cuts to 50.
        balances = {"XMR": (D("2.0"), D("2.0")), "USDT": (D(50), D(50))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=100, owned_base="1.0", seed_value=400,
                         reserve_base="1.5")

        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1020.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(50))
        self.assertEqual(D(ctrl._state["owned_base"]), D("1.0"))
        # Only the quote side was cut: counters and offsets say so.
        self.assertEqual(D(ctrl._state["reanchor_cut_quote_cum"]), D(50))
        self.assertEqual(D(ctrl._state["reanchor_cut_base_cum"]), D(0))
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(50))
        self.assertEqual(D(ctrl._state["reanchor_offset_base"]), D(0))


# ==================================================== CLA-M02: netting visibility

class TestReanchorNettingVisibility(_Harness):

    def test_deposit_withdrawal_netting_recorded_when_later_overclaim_fires(self):
        # A deposit (+50) then an equal withdrawal (-50) nets to zero in the total-wallet
        # trigger: owned stays untouched and NOTHING fires (the CLA-M02 blindness -- full
        # resolution is P5's declared flows). When a LATER over-claim fires (a further -30
        # withdrawal), the persisted reanchor_events entry + counters make the shrink
        # visible: the event's wallet totals let the operator reconstruct what netted.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=100)

        self._cycle(ctrl, mdp, 1000.0)
        balances["USDT"] = (D(150), D(150))  # external deposit +50
        self._cycle(ctrl, mdp, 1010.0)
        balances["USDT"] = (D(100), D(100))  # equal withdrawal -50: nets to zero
        self._cycle(ctrl, mdp, 1020.0)

        # Netting is invisible: owned untouched, no re-anchor recorded.
        self.assertEqual(D(ctrl._state["owned_quote"]), D(100))
        self.assertNotIn("reanchor_events", ctrl._state)
        self.assertEqual(self._emit_events(ctrl, "range_ladder_ledger_reanchored"), [])

        balances["USDT"] = (D(70), D(70))  # later over-claim: further withdrawal -30
        self._cycle(ctrl, mdp, 1030.0)  # observed
        self._cycle(ctrl, mdp, 1045.0)  # 15s > 10s grace -> cut 100 -> 70

        self.assertEqual(D(ctrl._state["owned_quote"]), D(70))
        events = ctrl._state["reanchor_events"]
        self.assertEqual(1, len(events))
        self.assertEqual("100", events[0]["old_owned_quote"])
        self.assertEqual("70", events[0]["new_owned_quote"])
        self.assertEqual("70", events[0]["wallet_quote_total"])
        self.assertEqual("30", events[0]["overclaim_quote"])
        self.assertEqual(1045.0, events[0]["ts"])
        # Cumulative counters record the cut (30) -- the visible shrink understates the
        # total outflow (80), which is exactly the netting the record makes auditable.
        self.assertEqual(D(ctrl._state["reanchor_cut_quote_cum"]), D(30))
        self.assertEqual(D(ctrl._state["reanchor_cut_base_cum"]), D(0))
        # Persisted to disk, not just in memory.
        persisted = self._persisted()
        self.assertEqual(1, len(persisted["reanchor_events"]))
        self.assertEqual("70", persisted["reanchor_events"][0]["new_owned_quote"])
        self.assertEqual("30", persisted["reanchor_cut_quote_cum"])

    def test_reanchor_events_list_capped_at_newest_50_counters_uncapped(self):
        # 52 successive cuts of 2 quote each: the list keeps the NEWEST 50 (rounds 3..52),
        # the cumulative counter keeps the full 52 * 2 = 104.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(1000), D(1000))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=0)
        self._init_state(ctrl, owned_quote=1000, owned_base=0, seed_value=1000)

        t = 1000.0
        for i in range(1, 53):
            wallet = D(1000 - 2 * i)
            balances["USDT"] = (wallet, wallet)
            self._cycle(ctrl, mdp, t)        # observe (grace timer arms)
            self._cycle(ctrl, mdp, t + 1.0)  # grace 0 elapsed -> cut
            t += 10.0

        self.assertEqual(D(ctrl._state["owned_quote"]), D(896))  # 1000 - 104
        events = ctrl._state["reanchor_events"]
        self.assertEqual(50, len(events))
        # Oldest kept entry is round 3 (1000-4 -> 1000-6); newest is round 52.
        self.assertEqual("996", events[0]["old_owned_quote"])
        self.assertEqual("994", events[0]["new_owned_quote"])
        self.assertEqual("898", events[-1]["old_owned_quote"])
        self.assertEqual("896", events[-1]["new_owned_quote"])
        self.assertEqual(D(ctrl._state["reanchor_cut_quote_cum"]), D(104))


# ==================================================== F5: offset credits

class TestReanchorOffsetCredits(_Harness):

    def _reanchored_ctrl(self, *, base_total="0.1", owned_base="0", **config_overrides):
        """owned_quote=100 vs wallet 70: the 30 the late BUY already spent re-anchors away
        at t=1020, leaving offset_quote=30 (ts=1020). Returns (ctrl, mdp, balances)."""
        balances = {"XMR": (D(base_total), D(base_total)), "USDT": (D(70), D(70))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, ledger_overclaim_reanchor_seconds=10, **config_overrides)
        self._init_state(ctrl, owned_quote=100, owned_base=owned_base, seed_value=130)
        self._cycle(ctrl, mdp, 1000.0)
        self._cycle(ctrl, mdp, 1020.0)
        self.assertEqual(D(ctrl._state["owned_quote"]), D(70))  # cut applied
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(30))
        return ctrl, mdp, balances

    def test_offset_credit_prevents_double_debit_on_late_buy_fill(self):
        # The wallet already reflects the BUY (70 = 100 - 30 spent); the re-anchor deducted
        # those 30 from owned_quote. When the fill then books (d_quote=30, d_base=0.1) the
        # offset absorbs the debit: owned_quote STAYS 70 (not 70-30=40) and the base credit
        # books in full. Would fail with offsets removed (owned_quote would read 40).
        ctrl, mdp, balances = self._reanchored_ctrl()
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy", filled_base="0.1", filled_quote="30",
        )]
        self._cycle(ctrl, mdp, 1030.0)

        self.assertEqual(D(ctrl._state["owned_quote"]), D(70))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.1"))
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(0))  # fully consumed
        booked = self._emit_events(ctrl, "range_ladder_fill_booked")
        self.assertEqual(1, len(booked))
        self.assertEqual("30", booked[0].kwargs["offset_quote_consumed"])
        self.assertEqual("0", booked[0].kwargs["offset_base_consumed"])
        # Consumption persisted.
        self.assertEqual("0", self._persisted()["reanchor_offset_quote"])

    def test_partial_offset_only_absorbs_up_to_credit(self):
        # Debit 50 > credit 30: exactly min(50, 30) = 30 absorbed, the remaining 20 debits
        # owned_quote (70 - 20 = 50). A credit never over-absorbs.
        ctrl, mdp, balances = self._reanchored_ctrl(base_total="0.2")
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy", filled_base="0.2", filled_quote="50",
        )]
        self._cycle(ctrl, mdp, 1030.0)

        self.assertEqual(D(ctrl._state["owned_quote"]), D(50))
        self.assertEqual(D(ctrl._state["owned_base"]), D("0.2"))
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(0))

    def test_expired_offset_does_not_absorb_fresh_debit(self):
        # Same trace but the fill books 80s after the cut with a 60s expiry: the stale
        # credit is cleared (fail closed) and the debit applies in full: 70 - 30 = 40.
        ctrl, mdp, balances = self._reanchored_ctrl(reanchor_offset_expiry_seconds=60)
        ctrl.executors_info = [_filling_executor(
            "buy_300", TradeType.BUY, 300, "late-buy", filled_base="0.1", filled_quote="30",
        )]
        self._cycle(ctrl, mdp, 1100.0)  # cut ts=1020, age 80 >= 60 -> expired

        self.assertEqual(D(ctrl._state["owned_quote"]), D(40))
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(0))
        expired = self._emit_events(ctrl, "range_ladder_reanchor_offset_expired")
        self.assertEqual(1, len(expired))
        booked = self._emit_events(ctrl, "range_ladder_fill_booked")
        self.assertEqual("0", booked[0].kwargs["offset_quote_consumed"])

    def test_quote_offset_never_absorbs_sell_side_base_debit(self):
        # Offsets are per-asset: a quote credit of 30 must NOT absorb a SELL's base debit,
        # and the SELL's quote CREDIT books in full (credits are never offset). owned_base
        # 0.5 - 0.2 = 0.3; owned_quote 70 + 60 = 130; the quote credit survives untouched.
        ctrl, mdp, balances = self._reanchored_ctrl(base_total="0.5", owned_base="0.5")
        balances["XMR"] = (D("0.3"), D("0.3"))    # the sold base already left the wallet
        balances["USDT"] = (D(130), D(130))       # its proceeds already arrived
        ctrl.executors_info = [_filling_executor(
            "sell_350", TradeType.SELL, 300, "late-sell", filled_base="0.2", filled_quote="60",
        )]
        self._cycle(ctrl, mdp, 1030.0)

        self.assertEqual(D(ctrl._state["owned_base"]), D("0.3"))
        self.assertEqual(D(ctrl._state["owned_quote"]), D(130))
        self.assertEqual(D(ctrl._state["reanchor_offset_quote"]), D(30))  # untouched
        booked = self._emit_events(ctrl, "range_ladder_fill_booked")
        self.assertEqual("0", booked[0].kwargs["offset_base_consumed"])
        self.assertEqual("0", booked[0].kwargs["offset_quote_consumed"])

    def test_reseed_clears_outstanding_offset_credits(self):
        # A re-seed re-baselines owned_* from the live wallet and re-primes open-order
        # baselines, so an outstanding credit no longer matches any pending late fill --
        # leaving it live would let it absorb a genuine post-reseed debit (fabricated
        # equity). The reseed must zero both offsets.
        balances = {"XMR": (D(0), D(0)), "USDT": (D(200), D(200))}
        mdp = _make_mdp(balances=balances, mid=300, bid=299, ask=301)
        ctrl = self._build(mdp, reseed_fund_from_wallet_once=True, reseed_generation=1)
        self._init_state(ctrl, owned_quote=100, owned_base=0, seed_value=100,
                         reanchor_offset_quote="30", reanchor_offset_base="0.1",
                         reanchor_offset_ts=900.0)
        self._cycle(ctrl, mdp, 1000.0)  # flat book -> reseed applies

        self.assertEqual(D(ctrl._state["owned_quote"]), D(170))  # min(200, target 170)
        self.assertEqual("0", ctrl._state["reanchor_offset_quote"])
        self.assertEqual("0", ctrl._state["reanchor_offset_base"])


# ==================================================== config plumbing

class TestOffsetExpiryConfig(unittest.TestCase):

    def test_default_is_600(self):
        self.assertEqual(600, _make_config().reanchor_offset_expiry_seconds)

    def test_explicit_value_and_string_coercion(self):
        self.assertEqual(60, _make_config(reanchor_offset_expiry_seconds=60).reanchor_offset_expiry_seconds)
        self.assertEqual(120, _make_config(reanchor_offset_expiry_seconds="120").reanchor_offset_expiry_seconds)

    def test_negative_rejected(self):
        with self.assertRaises(Exception):
            _make_config(reanchor_offset_expiry_seconds=-1)
        # Zero is allowed: it means "credits expire immediately" (offsets disabled).
        self.assertEqual(0, _make_config(reanchor_offset_expiry_seconds=0).reanchor_offset_expiry_seconds)


if __name__ == "__main__":
    unittest.main()
