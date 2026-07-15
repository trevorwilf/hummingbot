"""
Regression tests for the CSF-V1 Phase 11 fixes to the amm_arb strategy
(finding ARB-8 in STRATEGY_CONNECTOR_REVIEW_FINDINGS_V1.md).
"""
import asyncio
import unittest
from decimal import Decimal
from typing import Awaitable
from unittest.mock import AsyncMock, patch

from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.core.event.events import MarketOrderFailureEvent, OrderCancelledEvent, OrderType
from hummingbot.strategy.amm_arb.amm_arb import AmmArbStrategy
from hummingbot.strategy.amm_arb.data_types import ArbProposalSide
from hummingbot.strategy.amm_arb.utils import ArbProposal
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple

TRADING_PAIR = "HBOT-USDT"


class AmmArbStrategyTest(unittest.TestCase):
    level = 0
    log_records = []

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and message in record.getMessage()
                   for record in self.log_records)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ev_loop = asyncio.get_event_loop()

    def setUp(self):
        self.log_records = []
        self.market_1: MockPaperExchange = MockPaperExchange()
        self.market_2: MockPaperExchange = MockPaperExchange()
        self.market_1.set_balanced_order_book(TRADING_PAIR, 100, 50, 150, 1, 10)
        self.market_2.set_balanced_order_book(TRADING_PAIR, 110, 50, 150, 1, 10)
        self.market_info_1 = MarketTradingPairTuple(self.market_1, TRADING_PAIR, "HBOT", "USDT")
        self.market_info_2 = MarketTradingPairTuple(self.market_2, TRADING_PAIR, "HBOT", "USDT")

        self.strategy = AmmArbStrategy()
        self.strategy.init_params(
            market_info_1=self.market_info_1,
            market_info_2=self.market_info_2,
            min_profitability=Decimal("0.01"),
            order_amount=Decimal("1"),
            concurrent_orders_submission=False,
        )
        self.strategy.logger().setLevel(1)
        self.strategy.logger().addHandler(self)

    def tearDown(self):
        self.strategy.logger().removeHandler(self)
        super().tearDown()

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: int = 3):
        return self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))

    def _make_side(self, market_info, is_buy: bool) -> ArbProposalSide:
        return ArbProposalSide(market_info, is_buy, Decimal("100"), Decimal("100"), Decimal("1"), [])

    def test_cancelled_order_terminates_the_wait_as_failed(self):
        """Would have caught ARB-8: an OrderCancelledEvent never set the side's completed_event,
        hanging the main task forever. Cancellation now completes the side as failed."""
        side = self._make_side(self.market_info_1, True)
        self.strategy._order_id_side_map["order_1"] = side

        self.strategy.did_cancel_order(OrderCancelledEvent(1640001112.0, "order_1"))

        self.assertTrue(side.is_completed)
        self.assertTrue(side.is_failed)
        # ARB-8: the terminal event also bounds the map growth
        self.assertNotIn("order_1", self.strategy._order_id_side_map)

    def test_order_id_side_map_is_bounded_by_terminal_events(self):
        """ARB-8: every terminal path removes its entry from _order_id_side_map."""
        for order_id, event_fn in [
            ("c1", lambda: self.strategy.set_order_completed("c1")),
            ("f1", lambda: self.strategy.did_fail_order(MarketOrderFailureEvent(1.0, "f1", OrderType.LIMIT))),
            ("x1", lambda: self.strategy.did_cancel_order(OrderCancelledEvent(1.0, "x1"))),
        ]:
            self.strategy._order_id_side_map[order_id] = self._make_side(self.market_info_1, True)
            event_fn()
            self.assertNotIn(order_id, self.strategy._order_id_side_map)

    def test_sequential_leg_wait_times_out_and_cancels(self):
        """Would have caught ARB-8: the un-bounded completed_event.wait() hung the main task
        forever when an order never reached a final state."""
        proposal = ArbProposal(self._make_side(self.market_info_1, True),
                               self._make_side(self.market_info_2, False))
        with patch.object(AmmArbStrategy, "ORDER_COMPLETION_WAIT_TIMEOUT", 0.05), \
                patch.object(self.strategy, "place_arb_order",
                             new=AsyncMock(return_value="order_1")) as place_mock, \
                patch.object(self.strategy, "cancel_order") as cancel_mock, \
                patch.object(self.strategy, "notify_hb_app_with_timestamp"):
            self.async_run_with_timeout(self.strategy.execute_arb_proposals([proposal]))

        # The first leg timed out: it was cancelled, and the second leg was never placed
        self.assertEqual(1, place_mock.call_count)
        cancel_mock.assert_called_once_with(self.market_info_1, "order_1")
        self.assertTrue(self._is_logged("WARNING", "did not reach a final state within"))

    def test_second_leg_failure_notifies_unhedged_position(self):
        """ARB-8: when the second leg fails after the first filled, the operator is alarmed."""
        proposal = ArbProposal(self._make_side(self.market_info_1, True),
                               self._make_side(self.market_info_2, False))
        order_ids = iter(["order_1", "order_2"])

        async def place_and_complete(market_info, is_buy, amount, order_price):
            order_id = next(order_ids)
            side = proposal.first_side if order_id == "order_1" else proposal.second_side
            if order_id == "order_1":
                side.set_completed()
            else:
                side.set_failed()
                side.set_completed()
            return order_id

        with patch.object(self.strategy, "place_arb_order", new=place_and_complete), \
                patch.object(self.strategy, "notify_hb_app_with_timestamp") as notify_mock:
            self.async_run_with_timeout(self.strategy.execute_arb_proposals([proposal]))

        notify_mock.assert_called_once()
        self.assertIn("unhedged", notify_mock.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
