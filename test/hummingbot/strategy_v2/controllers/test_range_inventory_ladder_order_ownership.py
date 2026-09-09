import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_CONTROLLER_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))

from range_inventory_ladder import RangeInventoryLadderController
from hummingbot.core.data_type.common import TradeType


class TestConnectorOrderOwnershipGuard(unittest.TestCase):

    @staticmethod
    def _order(order_id, *, controller_id=None, pair="XMR-USD", side=TradeType.BUY):
        return SimpleNamespace(
            client_order_id=order_id,
            exchange_order_id=f"EX-{order_id}",
            controller_id=controller_id,
            trading_pair=pair,
            trade_type=side,
            amount=Decimal("2"),
            executed_amount_base=Decimal("0.5"),
            price=Decimal("100"),
            is_done=False,
        )

    def _controller(self, orders):
        controller = object.__new__(RangeInventoryLadderController)
        controller.config = SimpleNamespace(
            id="xmr-ladder", connector_name="kraken", trading_pair="XMR-USD"
        )
        connector = SimpleNamespace(in_flight_orders={o.client_order_id: o for o in orders})
        controller.market_data_provider = SimpleNamespace(
            get_connector=MagicMock(return_value=connector),
            time=MagicMock(return_value=1000.0),
        )
        controller._order_executors_active_or_shutting_down = MagicMock(return_value=[])
        controller._emit_structured = MagicMock()
        controller._unowned_order_signature = None
        controller._unowned_order_last_warning_ts = 0.0
        controller._unowned_order_summary = {}
        return controller

    def test_untagged_same_pair_order_blocks_creates_and_reports_remaining_notional(self):
        controller = self._controller([self._order("orphan")])

        self.assertTrue(controller._refresh_unowned_order_guard())
        self.assertEqual(1, controller._unowned_order_summary["count"])
        self.assertEqual(Decimal("150"), controller._unowned_order_summary["buy_quote"])
        controller._emit_structured.assert_called_once()

    def test_order_tagged_to_another_controller_is_not_claimed_as_an_orphan(self):
        controller = self._controller([self._order("other", controller_id="other-ladder")])

        self.assertFalse(controller._refresh_unowned_order_guard())
        self.assertEqual(0, controller._unowned_order_summary["count"])

    def test_live_executor_order_id_satisfies_ownership(self):
        order = self._order("owned", controller_id="xmr-ladder")
        controller = self._controller([order])
        executor = SimpleNamespace(custom_info={"order_id": "owned"})
        controller._order_executors_active_or_shutting_down.return_value = [executor]

        self.assertFalse(controller._refresh_unowned_order_guard())
        self.assertEqual(0, controller._unowned_order_summary["count"])


if __name__ == "__main__":
    unittest.main()
