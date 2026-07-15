"""
CSF-V1 Phase 10 regression tests — strategy base event listeners.

Covers PMM-10: an exception raised in the strategy-side event handler must not skip the
order-tracker cleanup call, otherwise a completed/cancelled order stays tracked forever
and the strategy stops quoting.
"""
import logging
import unittest
from decimal import Decimal

from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.core.data_type.common import OrderType
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    MarketEvent,
    OrderCancelledEvent,
    SellOrderCompletedEvent,
)
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from hummingbot.strategy.strategy_py_base import StrategyPyBase

rs_logger = None


class RaisingHandlerStrategy(StrategyPyBase):
    """Strategy whose user-facing completion/cancel handlers raise (e.g. a broken notifier)."""

    @classmethod
    def logger(cls) -> logging.Logger:
        global rs_logger
        if rs_logger is None:
            rs_logger = logging.getLogger(__name__)
        return rs_logger

    def did_complete_buy_order(self, order_completed_event):
        raise RuntimeError("handler boom (buy)")

    def did_complete_sell_order(self, order_completed_event):
        raise RuntimeError("handler boom (sell)")

    def did_cancel_order(self, cancelled_event):
        raise RuntimeError("handler boom (cancel)")


class StrategyBaseListenerCsfPhase10Test(unittest.TestCase):
    trading_pair = "COINALPHA-HBOT"

    def setUp(self):
        self.market: MockPaperExchange = MockPaperExchange()
        self.market_info: MarketTradingPairTuple = MarketTradingPairTuple(
            self.market, self.trading_pair, *self.trading_pair.split("-")
        )
        self.market.set_balanced_order_book(trading_pair=self.trading_pair,
                                            mid_price=100, min_price=1,
                                            max_price=200, price_step_size=1, volume_step_size=10)
        self.strategy = RaisingHandlerStrategy()
        self.strategy.add_markets([self.market])
        self.strategy.order_tracker._set_current_timestamp(1640001112.223)

    def _track_order(self, order_id: str, is_buy: bool):
        self.strategy.order_tracker.start_tracking_limit_order(
            self.market_info, order_id, is_buy, Decimal("100"), Decimal("1"))

    def test_tracker_cleanup_runs_when_buy_completed_handler_raises(self):
        self._track_order("ORDER-BUY", True)
        self.assertIsNotNone(self.strategy.order_tracker.get_limit_order(self.market_info, "ORDER-BUY"))

        self.market.trigger_event(
            MarketEvent.BuyOrderCompleted,
            BuyOrderCompletedEvent(1640001113.0, "ORDER-BUY", "COINALPHA", "HBOT",
                                   Decimal("1"), Decimal("100"), OrderType.LIMIT))

        # Before the fix the RuntimeError from the strategy handler skipped the tracker
        # cleanup and the completed order stayed in the tracker forever.
        self.assertIsNone(self.strategy.order_tracker.get_limit_order(self.market_info, "ORDER-BUY"))

    def test_tracker_cleanup_runs_when_sell_completed_handler_raises(self):
        self._track_order("ORDER-SELL", False)

        self.market.trigger_event(
            MarketEvent.SellOrderCompleted,
            SellOrderCompletedEvent(1640001113.0, "ORDER-SELL", "COINALPHA", "HBOT",
                                    Decimal("1"), Decimal("100"), OrderType.LIMIT))

        self.assertIsNone(self.strategy.order_tracker.get_limit_order(self.market_info, "ORDER-SELL"))

    def test_tracker_cleanup_runs_when_cancel_handler_raises(self):
        self._track_order("ORDER-CXL", True)

        self.market.trigger_event(
            MarketEvent.OrderCancelled,
            OrderCancelledEvent(1640001113.0, "ORDER-CXL"))

        self.assertIsNone(self.strategy.order_tracker.get_limit_order(self.market_info, "ORDER-CXL"))


if __name__ == "__main__":
    unittest.main()
