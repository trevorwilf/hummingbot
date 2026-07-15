"""
CSF-V1 Phase 10 regression tests — classic pure market making family.

Covers PMM-1 (NaN price gate), PMM-3 (order-optimization NaN skip), PMM-4 (pair list
cleared per proposal execution), PMM-5 (dual hanging-order check in completion handlers),
PMM-6 (inventory-cost delegate DivisionByZero), PMM-7 (negative-price levels dropped),
PMM-9 (split-level index carried through repricing) and PMM-11 (next() defaults).
"""
import logging
import unittest
from decimal import Decimal
from typing import List

import pandas as pd

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange.paper_trade.paper_trade_exchange import QuantizationParams
from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.core.clock import Clock, ClockMode
from hummingbot.core.data_type.common import OrderType
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_row import OrderBookRow
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import BuyOrderCompletedEvent, MarketEvent
from hummingbot.model.inventory_cost import InventoryCost
from hummingbot.model.sql_connection_manager import SQLConnectionManager, SQLConnectionType
from hummingbot.strategy.data_types import HangingOrder
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from hummingbot.strategy.pure_market_making.data_types import PriceSize, Proposal
from hummingbot.strategy.pure_market_making.inventory_cost_price_delegate import InventoryCostPriceDelegate
from hummingbot.strategy.pure_market_making.pure_market_making import PureMarketMakingStrategy


def empty_order_book_side(order_book: OrderBook, bid_side: bool):
    """Removes every entry on one side of the order book (simulates a one-sided book)."""
    update_id: int = order_book.last_diff_uid + 1
    rows = order_book.bid_entries() if bid_side else order_book.ask_entries()
    diffs: List[OrderBookRow] = [OrderBookRow(row.price, 0, update_id) for row in rows]
    if bid_side:
        order_book.apply_diffs(diffs, [], update_id)
    else:
        order_book.apply_diffs([], diffs, update_id)


class DisappearingRestoredOrdersExchange(MockPaperExchange):
    """Paper exchange whose limit_orders are visible on the first read and gone afterwards
    (simulates a restored order vanishing between the two reads in c_start)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fake_restored_order = None
        self.limit_orders_read_count = 0

    @property
    def limit_orders(self):
        self.limit_orders_read_count += 1
        if self.limit_orders_read_count == 1 and self.fake_restored_order is not None:
            return [self.fake_restored_order]
        return []


class PMMCsfPhase10Test(unittest.TestCase):
    level = 0
    start: pd.Timestamp = pd.Timestamp("2019-01-01", tz="UTC")
    end: pd.Timestamp = pd.Timestamp("2019-01-01 01:00:00", tz="UTC")
    start_timestamp: float = start.timestamp()
    end_timestamp: float = end.timestamp()
    trading_pair = "HBOT-ETH"
    base_asset = trading_pair.split("-")[0]
    quote_asset = trading_pair.split("-")[1]

    def setUp(self):
        self.log_records = []
        self.clock_tick_size = 1
        self.clock: Clock = Clock(ClockMode.BACKTEST, self.clock_tick_size, self.start_timestamp, self.end_timestamp)
        self.market: MockPaperExchange = MockPaperExchange()
        self.mid_price = 100
        self.market.set_balanced_order_book(self.trading_pair,
                                            mid_price=self.mid_price,
                                            min_price=1,
                                            max_price=200,
                                            price_step_size=1,
                                            volume_step_size=10)
        self.market.set_balance("HBOT", 500)
        self.market.set_balance("ETH", 5000)
        self.market.set_quantization_param(
            QuantizationParams(
                self.trading_pair, 6, 6, 6, 6
            )
        )
        self.market_info = MarketTradingPairTuple(self.market, self.trading_pair,
                                                  self.base_asset, self.quote_asset)
        self.clock.add_iterator(self.market)
        self.cancel_order_logger: EventLogger = EventLogger()
        self.market.add_listener(MarketEvent.OrderCancelled, self.cancel_order_logger)

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message_start: str) -> bool:
        return any(record.levelname == log_level and record.getMessage().startswith(message_start)
                   for record in self.log_records)

    def _make_strategy(self, **kwargs) -> PureMarketMakingStrategy:
        params = dict(
            bid_spread=Decimal("0.01"),
            ask_spread=Decimal("0.01"),
            order_amount=Decimal("1"),
            order_refresh_time=5.0,
            filled_order_delay=5.0,
            order_refresh_tolerance_pct=Decimal("-1"),
            minimum_spread=-1,
        )
        params.update(kwargs)
        strategy = PureMarketMakingStrategy()
        strategy.init_params(self.market_info, **params)
        strategy.order_tracker._set_current_timestamp(1640001112.223)
        strategy.logger().setLevel(1)
        strategy.logger().addHandler(self)
        return strategy

    def _start(self, strategy: PureMarketMakingStrategy, extra_ticks: int = 1):
        self.clock.add_iterator(strategy)
        self.clock.backtest_til(self.start_timestamp + extra_ticks)

    # PMM-1 — a NaN reference price must cancel active orders and end the tick, not raise.
    def test_nan_price_tick_cancels_orders_and_returns(self):
        strategy = self._make_strategy(price_ceiling=Decimal("105"), price_floor=Decimal("95"))
        self._start(strategy)

        self.assertEqual(1, len(strategy.active_buys))
        self.assertEqual(1, len(strategy.active_sells))

        # One-sided book -> mid price NaN. Before the fix the tick raised InvalidOperation
        # (price band / min-spread comparisons) and the stale orders survived.
        empty_order_book_side(self.market.order_books[self.trading_pair], bid_side=True)
        self.assertTrue(strategy.get_price().is_nan())

        self.clock.backtest_til(self.start_timestamp + 2)

        self.assertEqual(0, len(strategy.active_buys))
        self.assertEqual(0, len(strategy.active_sells))
        self.assertEqual(2, len(self.cancel_order_logger.event_log))
        self.assertTrue(self._is_logged("WARNING", f"({self.trading_pair}) Reference price is NaN"))

    # PMM-1 — the warning is rate limited (one warning per 30s window).
    def test_nan_price_warning_is_rate_limited(self):
        strategy = self._make_strategy()
        self._start(strategy)
        empty_order_book_side(self.market.order_books[self.trading_pair], bid_side=True)

        self.clock.backtest_til(self.start_timestamp + 10)

        nan_warnings = [r for r in self.log_records
                        if r.levelname == "WARNING" and "Reference price is NaN" in r.getMessage()]
        self.assertEqual(1, len(nan_warnings))

    # PMM-3 — NaN result from get_price_for_volume must skip optimization, not abort the tick.
    def test_order_optimization_nan_skipped_orders_still_created(self):
        strategy = self._make_strategy(
            order_optimization_enabled=True,
            # far beyond total book depth -> get_price_for_volume returns NaN
            bid_order_optimization_depth=Decimal("10000000"),
            ask_order_optimization_depth=Decimal("10000000"),
        )
        # Before the fix this raised ValueError (ceil(NaN)) inside the tick and no orders
        # were ever created.
        self._start(strategy)

        self.assertEqual(1, len(strategy.active_buys))
        self.assertEqual(1, len(strategy.active_sells))
        self.assertEqual(Decimal("99"), strategy.active_buys[0].price)
        self.assertEqual(Decimal("101"), strategy.active_sells[0].price)

    # PMM-4 — stale created-pairs must not survive into the next proposal execution.
    def test_created_pairs_of_orders_cleared_on_execute(self):
        strategy = self._make_strategy(hanging_orders_enabled=True)
        self._start(strategy)

        tracker = strategy.hanging_orders_tracker
        self.assertEqual(1, len(tracker.current_created_pairs_of_orders))

        # Execute a second proposal directly (simulates the both-sides-filled cycle where the
        # cancel wave never ran update_strategy_orders_with_equivalent_orders).
        proposal = Proposal([PriceSize(Decimal("98"), Decimal("1"))],
                            [PriceSize(Decimal("102"), Decimal("1"))])
        strategy.execute_orders_proposal(proposal)

        pairs = tracker.current_created_pairs_of_orders
        # Before the fix the stale pair stayed at index 0, the new pair was appended at index 1
        # and the new sell order was attached to the STALE pair.
        self.assertEqual(1, len(pairs))
        self.assertIsNotNone(pairs[0].buy_order)
        self.assertIsNotNone(pairs[0].sell_order)
        self.assertEqual(Decimal("98"), pairs[0].buy_order.price)
        self.assertEqual(Decimal("102"), pairs[0].sell_order.price)

    # PMM-5 — a completed hanging order must not be misclassified as a regular fill when the
    # tracker's completion listener ran first.
    def test_completed_hanging_order_fill_not_misclassified(self):
        strategy = self._make_strategy(hanging_orders_enabled=True)
        self._start(strategy)

        buy_order = strategy.active_buys[0]
        order_id = buy_order.client_order_id
        # Simulate the tracker listener having processed the completion first: the order is
        # already moved from the current hanging orders to the completed set.
        strategy.hanging_orders_tracker.completed_hanging_orders.add(
            HangingOrder(order_id, self.trading_pair, True, buy_order.price, buy_order.quantity,
                         self.start_timestamp))

        self.market.trigger_event(
            MarketEvent.BuyOrderCompleted,
            BuyOrderCompletedEvent(
                self.market.current_timestamp, order_id, self.base_asset, self.quote_asset,
                buy_order.quantity, buy_order.quantity * buy_order.price, OrderType.LIMIT))

        # Before the fix `order_id in self.hanging_order_ids` was False (already moved to the
        # completed set) and the fill was logged/processed as a regular maker fill.
        self.assertTrue(self._is_logged("INFO", f"({self.trading_pair}) Hanging maker buy order {order_id}"))
        self.assertFalse(self._is_logged("INFO", f"({self.trading_pair}) Maker buy order {order_id}"))

    # PMM-7 — deep buy levels whose price goes non-positive are dropped from the proposal.
    def test_negative_price_levels_dropped(self):
        strategy = self._make_strategy(
            order_levels=3,
            order_level_spread=Decimal("0.5"),
            order_level_amount=Decimal("0"),
        )
        self._start(strategy)

        # levels: 99, 49, -1 -> the negative level must be dropped
        self.assertEqual(2, len(strategy.active_buys))
        self.assertTrue(all(order.price > 0 for order in strategy.active_buys))
        self.assertEqual(3, len(strategy.active_sells))

    # PMM-9 — split-level repricing must follow the configured level indices, not the list
    # position, once earlier levels have been dropped (e.g. by ping-pong).
    def test_split_order_levels_repricing_uses_carried_level_index(self):
        bid_spreads = [Decimal("1"), Decimal("2"), Decimal("4")]
        ask_spreads = [Decimal("1"), Decimal("2"), Decimal("4")]
        strategy = self._make_strategy(
            order_optimization_enabled=True,
            bid_order_optimization_depth=Decimal("1"),
            ask_order_optimization_depth=Decimal("1"),
            split_order_levels_enabled=True,
            bid_order_level_spreads=bid_spreads,
            ask_order_level_spreads=ask_spreads,
            order_override={
                "split_level_0": ["buy", 1, 1],
                "split_level_1": ["buy", 2, 1],
                "split_level_2": ["buy", 4, 1],
                "split_level_3": ["sell", 1, 1],
            },
        )
        self._start(strategy)

        # Level 0 was dropped upstream (ping-pong style); levels 1 and 2 remain.
        proposal = Proposal(
            [PriceSize(Decimal("98"), Decimal("1"), 1), PriceSize(Decimal("96"), Decimal("1"), 2)],
            [],
        )
        strategy.apply_order_optimization(proposal)

        anchor = self.market.quantize_order_price(self.trading_pair, Decimal("98"))
        expected_first = (anchor
                          * (1 - bid_spreads[1] / Decimal("100"))
                          / (1 - bid_spreads[1] / Decimal("100")))
        expected_second = (anchor
                           * (1 - bid_spreads[2] / Decimal("100"))
                           / (1 - bid_spreads[1] / Decimal("100")))
        self.assertEqual(expected_first, proposal.buys[0].price)
        # Before the fix the second order was priced with spreads[1]/spreads[0] (list-position
        # indexing), i.e. anchor * (1 - 2%) / (1 - 1%).
        buggy_second = (anchor
                        * (1 - bid_spreads[1] / Decimal("100"))
                        / (1 - bid_spreads[0] / Decimal("100")))
        self.assertNotEqual(buggy_second, proposal.buys[1].price)
        self.assertEqual(expected_second, proposal.buys[1].price)

    # PMM-11 — a restored hanging order that vanished from the connector between the two
    # limit_orders reads must not raise StopIteration into the clock.
    def test_restored_hanging_order_missing_does_not_raise(self):
        market = DisappearingRestoredOrdersExchange()
        market.set_balanced_order_book(self.trading_pair, mid_price=self.mid_price,
                                       min_price=1, max_price=200,
                                       price_step_size=1, volume_step_size=10)
        market.set_balance("HBOT", 500)
        market.set_balance("ETH", 5000)
        market.set_quantization_param(QuantizationParams(self.trading_pair, 6, 6, 6, 6))
        market.fake_restored_order = LimitOrder("RESTORED-1", self.trading_pair, True,
                                                self.base_asset, self.quote_asset,
                                                Decimal("99"), Decimal("1"))
        market_info = MarketTradingPairTuple(market, self.trading_pair,
                                             self.base_asset, self.quote_asset)
        strategy = PureMarketMakingStrategy()
        strategy.init_params(
            market_info,
            bid_spread=Decimal("0.01"),
            ask_spread=Decimal("0.01"),
            order_amount=Decimal("1"),
            order_refresh_time=5.0,
            hanging_orders_enabled=True,
        )
        self.clock.add_iterator(market)
        self.clock.add_iterator(strategy)
        # Before the fix this raised StopIteration out of c_start / the clock: the id from the
        # first limit_orders read is no longer present in the second read.
        self.clock.backtest_til(self.start_timestamp)

        self.assertGreaterEqual(market.limit_orders_read_count, 2)
        self.assertEqual([], strategy.hanging_order_ids)


class InventoryCostDelegateCsfPhase10Test(unittest.TestCase):
    """PMM-6 — DivisionByZero paths in the inventory-cost delegate."""

    @classmethod
    def setUpClass(cls):
        cls.trade_fill_sql = SQLConnectionManager(
            ClientConfigAdapter(ClientConfigMap()), SQLConnectionType.TRADE_FILLS, db_path=""
        )
        cls.trading_pair = "BTC-USDT"
        cls.base_asset, cls.quote_asset = cls.trading_pair.split("-")

    def setUp(self):
        with self.trade_fill_sql.get_new_session() as session:
            for table in [InventoryCost.__table__]:
                with session.begin():
                    session.execute(table.delete())
        self.delegate = InventoryCostPriceDelegate(self.trade_fill_sql, self.trading_pair)

    def _add_record(self, base_volume: Decimal, quote_volume: Decimal):
        with self.trade_fill_sql.get_new_session() as session:
            with session.begin():
                session.add(InventoryCost(
                    base_asset=self.base_asset,
                    quote_asset=self.quote_asset,
                    base_volume=base_volume,
                    quote_volume=quote_volume,
                ))

    def test_get_price_zero_base_volume_with_quote_returns_none(self):
        # base fully sold at a profit: base_volume == 0 but quote_volume > 0.
        # Before the fix this raised decimal.DivisionByZero (only InvalidOperation was caught).
        self._add_record(Decimal("0"), Decimal("9000"))
        self.assertIsNone(self.delegate.get_price())

    def test_process_sell_with_zero_base_volume_record_raises_runtime_error(self):
        from hummingbot.core.data_type.common import TradeType
        from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
        from hummingbot.core.event.events import OrderFilledEvent

        self._add_record(Decimal("0"), Decimal("9000"))
        event = OrderFilledEvent(
            timestamp=1,
            order_id="order1",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            order_type=OrderType.LIMIT,
            price=Decimal("10000"),
            amount=Decimal("1"),
            trade_fee=AddedToCostTradeFee(percent=Decimal("0"), flat_fees=[]),
        )
        # Before the fix this raised an uncaught DivisionByZero instead of the intended
        # RuntimeError used for the missing-record case.
        with self.assertRaises(RuntimeError):
            self.delegate.process_order_fill_event(event)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
