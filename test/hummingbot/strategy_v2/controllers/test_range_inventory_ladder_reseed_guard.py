"""Phase 4 hardening: the one-shot re-seed defers while orders are resting.

_maybe_reseed_fund claims from AVAILABLE balances only. Funds held in the controller's own
resting orders would be excluded from the new seed; when those orders later cancel the money
returns to the wallet, but a cancel is not a fill, so the ledger never re-grows -- the
capital strands outside the fund. The re-seed now defers while any order executor is active
or shutting down (warning + range_ladder_reseed_deferred_active_orders once per token, token
NOT consumed) and applies automatically on the first flat-book cycle.

Harness mirrors test_range_inventory_ladder_v13.py (the re-seed suite).
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


def _order_exec(level_id, side, price, eid, status=RunnableStatus.RUNNING):
    ex = MagicMock()
    ex.id = eid
    ex.status = status
    ex.is_active = status in (RunnableStatus.NOT_STARTED, RunnableStatus.RUNNING)
    ex.timestamp = 0.0
    ex.close_timestamp = None
    ex.connector_name = "nonkyc"
    ex.custom_info = {}
    ex.filled_amount_quote = Decimal("0")
    ex.cum_fees_quote = Decimal("0")
    cfg = MagicMock()
    cfg.type = "order_executor"
    cfg.level_id = level_id
    cfg.side = side
    cfg.price = D(price)
    cfg.amount = D("0.1")
    ex.config = cfg
    return ex


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp, **config_overrides):
        defaults = dict(
            id="ctrl-rsg",
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

    def _set_state(self, ctrl, *, owned_quote, owned_base, seed_value="300"):
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

    @staticmethod
    def _cycle(ctrl, mdp, t):
        mdp.time.return_value = t
        asyncio.run(ctrl.update_processed_data())

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]


class TestReseedDeferredWhileOrdersRest(_Harness):

    def _armed_ctrl(self, balances):
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        ctrl = self._build(mdp, total_amount_quote=Decimal("300"), reseed_fund_from_wallet_once=True)
        self._set_state(ctrl, owned_quote="193", owned_base="0", seed_value="193")
        return ctrl, mdp

    def test_reseed_deferred_with_active_executor_token_not_consumed(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        ctrl, mdp = self._armed_ctrl(balances)
        ctrl.executors_info = [_order_exec("buy_321", TradeType.BUY, "321", "B1")]

        self._cycle(ctrl, mdp, 1000.0)

        self.assertEqual(D(ctrl._state["owned_quote"]), D("193"))         # NOT re-seeded
        self.assertNotIn("last_reseed_token", ctrl._state)                # token NOT consumed
        self.assertEqual(self._events(ctrl, "range_ladder_fund_reseeded"), [])
        deferred = self._events(ctrl, "range_ladder_reseed_deferred_active_orders")
        self.assertEqual(1, len(deferred))
        self.assertEqual(1, deferred[0].kwargs["active_executor_count"])
        self.assertEqual("0:300", deferred[0].kwargs["reseed_token"])

    def test_deferred_event_emitted_once_per_token_not_per_cycle(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        ctrl, mdp = self._armed_ctrl(balances)
        ctrl.executors_info = [_order_exec("buy_321", TradeType.BUY, "321", "B1")]

        for t in (1000.0, 1010.0, 1020.0):
            self._cycle(ctrl, mdp, t)

        self.assertEqual(1, len(self._events(ctrl, "range_ladder_reseed_deferred_active_orders")))

    def test_shutting_down_executor_also_defers(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        ctrl, mdp = self._armed_ctrl(balances)
        ctrl.executors_info = [_order_exec("buy_321", TradeType.BUY, "321", "B1",
                                           status=RunnableStatus.SHUTTING_DOWN)]

        self._cycle(ctrl, mdp, 1000.0)

        self.assertEqual(self._events(ctrl, "range_ladder_fund_reseeded"), [])
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_reseed_deferred_active_orders")))

    def test_reseed_applies_once_on_first_flat_book_cycle_same_token(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        ctrl, mdp = self._armed_ctrl(balances)
        ctrl.executors_info = [_order_exec("buy_321", TradeType.BUY, "321", "B1")]
        self._cycle(ctrl, mdp, 1000.0)                                    # deferred
        self.assertEqual(self._events(ctrl, "range_ladder_fund_reseeded"), [])

        ctrl.executors_info = []                                          # book is now flat
        self._cycle(ctrl, mdp, 1010.0)                                    # applies

        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))
        self.assertEqual(ctrl._state["last_reseed_token"], "0:300")
        reseeded = self._events(ctrl, "range_ladder_fund_reseeded")
        self.assertEqual(1, len(reseeded))
        self.assertEqual("0:300", reseeded[0].kwargs["reseed_token"])     # SAME token as deferred

        self._cycle(ctrl, mdp, 1020.0)                                    # idempotent afterwards
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_fund_reseeded")))

    def test_terminated_executor_does_not_defer(self):
        balances = {"XMR": (D(0), D(0)), "USDT": (D(300), D(300))}
        ctrl, mdp = self._armed_ctrl(balances)
        ctrl.executors_info = [_order_exec("buy_321", TradeType.BUY, "321", "B1",
                                           status=RunnableStatus.TERMINATED)]

        self._cycle(ctrl, mdp, 1000.0)

        self.assertEqual(D(ctrl._state["owned_quote"]), D("300"))         # re-seeded immediately
        self.assertEqual(self._events(ctrl, "range_ladder_reseed_deferred_active_orders"), [])
        self.assertEqual(1, len(self._events(ctrl, "range_ladder_fund_reseeded")))


if __name__ == "__main__":
    unittest.main()
