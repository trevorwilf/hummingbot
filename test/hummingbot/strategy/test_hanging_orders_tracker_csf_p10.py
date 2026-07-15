"""
CSF-V1 Phase 10 regression tests — hanging orders tracker.

Covers PMM-1 (NaN price mirror in remove_orders_far_from_price) and PMM-15
(is_hanging_order_in_strategy_active_orders was dead code: all() with 4 positional args).
"""
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock

from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.strategy.data_types import HangingOrder, OrderType
from hummingbot.strategy.hanging_orders_tracker import HangingOrdersTracker


class HangingOrdersTrackerCsfPhase10Test(unittest.TestCase):

    def setUp(self) -> None:
        super().setUp()
        self.current_market_price = Decimal("100.0")
        self.strategy = self.create_mock_strategy()
        self.tracker = HangingOrdersTracker(self.strategy, hanging_orders_cancel_pct=Decimal("0.1"))

    def create_mock_strategy(self):
        market = MagicMock()
        market.get_maker_order_type.return_value = OrderType.LIMIT

        market_info = MagicMock()
        market_info.market = market

        strategy = MagicMock()
        type(strategy).max_order_age = PropertyMock(return_value=1800.0)
        strategy.get_price.return_value = self.current_market_price
        type(strategy).market_info = PropertyMock(return_value=market_info)
        type(strategy).trading_pair = PropertyMock(return_value="BTC-USDT")
        return strategy

    # PMM-1 — a NaN reference price must not raise inside the distance check.
    def test_remove_orders_far_from_price_skips_nan_price(self):
        order = LimitOrder("Order-1", "BTC-USDT", True, "BTC", "USDT", Decimal("100"), Decimal("1"))
        self.tracker.add_order(order)
        self.strategy.get_price.return_value = Decimal("NaN")

        # Before the fix this raised decimal.InvalidOperation in the price-distance comparison.
        self.tracker.remove_orders_far_from_price()

        self.assertIn(order, self.tracker.original_orders)
        self.strategy.cancel_order.assert_not_called()

    def test_remove_orders_far_from_price_still_works_with_real_price(self):
        far_order = LimitOrder("Order-far", "BTC-USDT", True, "BTC", "USDT", Decimal("50"), Decimal("1"))
        self.tracker.add_order(far_order)
        self.strategy.active_orders = [far_order]

        self.tracker.remove_orders_far_from_price()

        self.strategy.cancel_order.assert_called_once_with("Order-far")

    # PMM-15 — the method must evaluate the four conditions, not raise TypeError.
    def test_is_hanging_order_in_strategy_active_orders_matches(self):
        active = LimitOrder("Order-1", "BTC-USDT", True, "BTC", "USDT", Decimal("100"), Decimal("1"))
        type(self.strategy).active_orders = PropertyMock(return_value=[active])
        hanging = HangingOrder("Order-1", "BTC-USDT", True, Decimal("100"), Decimal("1"), 1234567890)

        # Before the fix this raised TypeError: all() takes exactly one argument (4 given).
        self.assertTrue(self.tracker.is_hanging_order_in_strategy_active_orders(hanging))

    def test_is_hanging_order_in_strategy_active_orders_no_match(self):
        active = LimitOrder("Order-1", "BTC-USDT", True, "BTC", "USDT", Decimal("100"), Decimal("1"))
        type(self.strategy).active_orders = PropertyMock(return_value=[active])
        different_price = HangingOrder("Order-1", "BTC-USDT", True, Decimal("101"), Decimal("1"), 1234567890)

        self.assertFalse(self.tracker.is_hanging_order_in_strategy_active_orders(different_price))


if __name__ == "__main__":
    unittest.main()
