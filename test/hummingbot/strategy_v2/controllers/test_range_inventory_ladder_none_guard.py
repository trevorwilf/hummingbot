"""Phase 7 hardening: determine_executor_actions survives a stop for an unknown executor.

_find_executor_by_id returns None on a miss, and the stop-detection comprehensions used to
call _executor_side(None) -> AttributeError on None.config, crashing the controller cycle in
a narrow race (a stop action racing an executors_info refresh). The side lookup is hoisted
into _executor_side_by_id, which treats a missing executor as neither side.
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
from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction  # noqa: E402

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


def _order_exec(level_id, side, price, eid):
    ex = MagicMock()
    ex.id = eid
    ex.status = RunnableStatus.RUNNING
    ex.is_active = True
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


class TestNoneGuard(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "state.json"
        balances = {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
        mdp = _make_mdp(balances=balances, mid=335, bid=334.9, ask=335.1)
        config = RangeInventoryLadderConfig(
            id="ctrl-ng",
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")],
            buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("350")],
            sell_amounts_pct=[Decimal("1")],
        )
        self.ctrl = RangeInventoryLadderController(
            config, market_data_provider=mdp, actions_queue=MagicMock()
        )
        self.ctrl._emit_structured = MagicMock()
        patcher = patch.object(
            type(self.ctrl), "state_path", new_callable=PropertyMock,
            return_value=self._state_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctrl._state = {
            "initialized": True,
            "owned_quote": "100", "owned_base": "0", "seed_value_quote": "100",
            "initial_managed_quote": "100", "initial_claimed_base_amount": "0",
            "initial_reference_price": "335",
            "reserve_quote_balance": "0", "reserve_base_balance": "0",
            "tracked_fill_executor_ids": [],
        }
        self.ctrl._state_loaded = True
        self.ctrl.executors_info = []
        self.ctrl.positions_held = []
        asyncio.run(self.ctrl.update_processed_data())
        self.ctrl.create_actions_proposal = MagicMock(return_value=[])

    def _stop(self, executor_id):
        return StopExecutorAction(controller_id="ctrl-ng", executor_id=executor_id)

    def test_stop_for_unknown_executor_does_not_raise(self):
        self.ctrl.executors_info = []
        self.ctrl.stop_actions_proposal = MagicMock(return_value=[self._stop("ghost-id")])

        actions = self.ctrl.determine_executor_actions()  # must not raise

        self.assertEqual(1, len(actions))
        self.assertFalse(self.ctrl._defer_buy_creates_this_cycle)
        self.assertFalse(self.ctrl._defer_sell_creates_this_cycle)

    def test_defer_flags_computed_from_resolvable_executors_only(self):
        buy_exec = _order_exec("buy_321", TradeType.BUY, "321", "B1")
        self.ctrl.executors_info = [buy_exec]
        self.ctrl.stop_actions_proposal = MagicMock(
            return_value=[self._stop("B1"), self._stop("ghost-id")])

        self.ctrl.determine_executor_actions()

        self.assertTrue(self.ctrl._defer_buy_creates_this_cycle)    # B1 resolved to BUY
        self.assertFalse(self.ctrl._defer_sell_creates_this_cycle)  # ghost is neither side

    def test_sell_stop_still_detected_alongside_ghost(self):
        sell_exec = _order_exec("sell_350", TradeType.SELL, "350", "S1")
        self.ctrl.executors_info = [sell_exec]
        self.ctrl.stop_actions_proposal = MagicMock(
            return_value=[self._stop("ghost-id"), self._stop("S1")])

        self.ctrl.determine_executor_actions()

        self.assertFalse(self.ctrl._defer_buy_creates_this_cycle)
        self.assertTrue(self.ctrl._defer_sell_creates_this_cycle)

    def test_executor_side_by_id_returns_none_on_miss(self):
        self.ctrl.executors_info = [_order_exec("buy_321", TradeType.BUY, "321", "B1")]
        self.assertEqual(TradeType.BUY, self.ctrl._executor_side_by_id("B1"))
        self.assertIsNone(self.ctrl._executor_side_by_id("nope"))


if __name__ == "__main__":
    unittest.main()
