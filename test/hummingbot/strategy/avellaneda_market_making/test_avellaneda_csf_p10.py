"""
CSF-V1 Phase 10 regression tests — Avellaneda market making.

Covers PMM-2 (NaN mid-price sample/reservation-price guards + cancel wave), PMM-3
(order-optimization NaN skip), PMM-4 (created-pairs cleared per proposal execution),
PMM-12 (old hanging-orders tracker unregistered on config toggle) and PMM-13
(execution-timeframe end > start validation).
"""
import unittest
from decimal import Decimal
from typing import Dict, List
from unittest.mock import MagicMock

import pandas as pd
from pydantic import ValidationError

from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.client.settings import AllConnectorSettings
from hummingbot.connector.exchange.paper_trade.paper_trade_exchange import QuantizationParams
from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.core.clock import Clock, ClockMode
from hummingbot.core.data_type.common import OrderType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_row import OrderBookRow
from hummingbot.core.data_type.trade_fee import TradeFeeSchema
from hummingbot.core.event.events import MarketEvent
from hummingbot.strategy.__utils__.trailing_indicators.trading_intensity import TradingIntensityIndicator
from hummingbot.strategy.avellaneda_market_making import AvellanedaMarketMakingStrategy
from hummingbot.strategy.avellaneda_market_making.avellaneda_market_making_config_map_pydantic import (
    AvellanedaMarketMakingConfigMap,
    DailyBetweenTimesModel,
    FromDateToDateModel,
    TrackHangingOrdersModel,
)
from hummingbot.strategy.data_types import PriceSize, Proposal
from hummingbot.strategy.hanging_orders_tracker import CreatedPairOfOrders
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from hummingbot.strategy.order_book_asset_price_delegate import OrderBookAssetPriceDelegate


def empty_order_book_side(order_book: OrderBook, bid_side: bool):
    update_id: int = order_book.last_diff_uid + 1
    rows = order_book.bid_entries() if bid_side else order_book.ask_entries()
    diffs: List[OrderBookRow] = [OrderBookRow(row.price, 0, update_id) for row in rows]
    if bid_side:
        order_book.apply_diffs(diffs, [], update_id)
    else:
        order_book.apply_diffs([], diffs, update_id)


class AvellanedaCsfPhase10Test(unittest.TestCase):
    start: pd.Timestamp = pd.Timestamp("2019-01-01", tz="UTC")
    end: pd.Timestamp = pd.Timestamp("2019-01-01 01:00:00", tz="UTC")
    start_timestamp: float = start.timestamp()
    end_timestamp: float = end.timestamp()
    trading_pair: str = "COINALPHA-HBOT"

    def setUp(self):
        super().setUp()
        self.base_asset, self.quote_asset = self.trading_pair.split("-")
        trade_fee_schema = TradeFeeSchema(
            maker_percent_fee_decimal=Decimal("0.25"), taker_percent_fee_decimal=Decimal("0.25")
        )
        self.market: MockPaperExchange = MockPaperExchange(trade_fee_schema=trade_fee_schema)
        self.market_info: MarketTradingPairTuple = MarketTradingPairTuple(
            self.market, self.trading_pair, self.base_asset, self.quote_asset
        )
        self.market.set_balanced_order_book(trading_pair=self.trading_pair,
                                            mid_price=100,
                                            min_price=1,
                                            max_price=200,
                                            price_step_size=1,
                                            volume_step_size=10)
        self.market.set_balance(self.base_asset, 500)
        self.market.set_balance(self.quote_asset, 5000)
        self.market.set_quantization_param(
            QuantizationParams(self.base_asset, 6, 6, 6, 6)
        )
        self._original_paper_trade_exchanges = AllConnectorSettings.paper_trade_connectors_names
        AllConnectorSettings.paper_trade_connectors_names.append("mock_paper_exchange")

        config_settings = self.get_default_map()
        self.config_map = ClientConfigAdapter(AvellanedaMarketMakingConfigMap(**config_settings))

        self.strategy: AvellanedaMarketMakingStrategy = AvellanedaMarketMakingStrategy()
        self.strategy.init_params(
            config_map=self.config_map,
            market_info=self.market_info,
        )

        self.clock: Clock = Clock(ClockMode.BACKTEST, 1, self.start_timestamp, self.end_timestamp)
        self.clock.add_iterator(self.market)
        self.clock.add_iterator(self.strategy)
        self.strategy.start(self.clock, self.start_timestamp)
        self.clock.backtest_til(self.start_timestamp)

    def tearDown(self) -> None:
        self.strategy.stop(self.clock)
        if self._original_paper_trade_exchanges is not None:
            AllConnectorSettings.paper_trade_connectors_names = self._original_paper_trade_exchanges
        super().tearDown()

    def get_default_map(self) -> Dict[str, str]:
        return {
            "exchange": self.market.name,
            "market": self.trading_pair,
            "execution_timeframe_mode": "infinite",
            "order_amount": Decimal("10"),
            "order_optimization_enabled": "yes",
            "min_spread": Decimal("0"),
            "risk_factor": Decimal("0.8"),
            "order_refresh_time": "30",
            "inventory_target_base_pct": Decimal("50"),
            "add_transaction_costs": "yes",
        }

    def _set_real_trading_intensity(self):
        price_delegate = OrderBookAssetPriceDelegate(self.market, self.trading_pair)
        self.strategy.trading_intensity = TradingIntensityIndicator(
            order_book=self.market_info.order_book,
            price_delegate=price_delegate,
            sampling_length=20,
        )

    # PMM-2 — a NaN mid price must not be fed into the volatility indicator.
    def test_nan_price_sample_skipped(self):
        avg_vol_mock = MagicMock()
        self.strategy.avg_vol = avg_vol_mock
        self._set_real_trading_intensity()

        # sanity: with a healthy book the sample is added
        self.strategy.collect_market_variables(self.start_timestamp + 1)
        self.assertEqual(1, avg_vol_mock.add_sample.call_count)

        empty_order_book_side(self.market.order_books[self.trading_pair], bid_side=True)
        self.assertTrue(self.strategy.get_price().is_nan())

        # Before the fix the NaN sample entered the buffer and poisoned the volatility
        # calculation for a full buffer length.
        self.strategy.collect_market_variables(self.start_timestamp + 2)
        self.assertEqual(1, avg_vol_mock.add_sample.call_count)

    # PMM-2 — NaN mid price zeroes the optimal prices instead of raising mid-computation.
    def test_nan_price_reservation_calculation_fails_closed(self):
        avg_vol_mock = MagicMock()
        avg_vol_mock.current_value = 0.05
        self.strategy.avg_vol = avg_vol_mock
        self.strategy.alpha = Decimal("1")
        self.strategy.kappa = Decimal("1")
        self.strategy.optimal_bid = Decimal("99")
        self.strategy.optimal_ask = Decimal("101")

        empty_order_book_side(self.market.order_books[self.trading_pair], bid_side=True)

        # Before the fix this raised decimal.InvalidOperation in the max()/min() clamps.
        self.strategy.calculate_reservation_price_and_optimal_spread()

        self.assertEqual(Decimal("0"), self.strategy.optimal_bid)
        self.assertEqual(Decimal("0"), self.strategy.optimal_ask)

    # PMM-2 — when quoting is impossible the cancel wave must still run.
    def test_cancel_wave_runs_when_quoting_impossible(self):
        avg_vol_mock = MagicMock()
        avg_vol_mock.current_value = 0.0  # degenerate volatility -> optimal prices stay 0
        self.strategy.avg_vol = avg_vol_mock
        self._set_real_trading_intensity()

        self.strategy.buy_with_specific_market(
            self.market_info, amount=Decimal("1"), order_type=OrderType.LIMIT, price=Decimal("90"))
        self.assertEqual(1, len(self.strategy.active_orders))

        # Before the fix the else-branch didn't exist: with optimal bid/ask at 0 the tick
        # ended without any cancel wave and the stale order survived.
        self.strategy.process_tick(self.start_timestamp + 1)

        self.assertEqual(0, len(self.strategy.active_orders))

    # PMM-3 — NaN from get_price_for_volume must skip optimization for the side, not raise.
    def test_order_optimization_nan_skipped(self):
        empty_order_book_side(self.market.order_books[self.trading_pair], bid_side=True)

        proposal = Proposal([PriceSize(Decimal("98"), Decimal("1"))],
                            [PriceSize(Decimal("102"), Decimal("1"))])
        # Before the fix this raised ValueError (ceil(NaN)) for the bid side.
        self.strategy.apply_order_optimization(proposal)

        # bid side untouched (optimization skipped), ask side still optimized normally
        self.assertEqual(Decimal("98"), proposal.buys[0].price)
        self.assertEqual(1, len(proposal.sells))

    # PMM-4 — stale created-pairs are dropped at the start of every proposal execution.
    def test_created_pairs_of_orders_cleared_on_execute(self):
        tracker = self.strategy.hanging_orders_tracker
        stale_pair = CreatedPairOfOrders(None, None)
        stale_pair.filled_buy = True
        stale_pair.filled_sell = True
        tracker.current_created_pairs_of_orders.append(stale_pair)

        proposal = Proposal([PriceSize(Decimal("98"), Decimal("1"))],
                            [PriceSize(Decimal("102"), Decimal("1"))])
        self.strategy.execute_orders_proposal(proposal)

        # hanging orders are disabled by default -> no new pairs; the stale one must be gone
        self.assertEqual(0, len(tracker.current_created_pairs_of_orders))

    # PMM-12 — replacing the hanging-orders tracker must unregister the old one's listeners.
    def test_old_hanging_orders_tracker_unregistered_on_toggle(self):
        old_tracker = self.strategy.hanging_orders_tracker
        old_forwarder = old_tracker._cancel_order_forwarder
        self.assertIn(old_forwarder, self.market.get_listeners(MarketEvent.OrderCancelled))

        self.config_map.hanging_orders_mode = TrackHangingOrdersModel(
            hanging_orders_cancel_pct=Decimal("2"))
        self.strategy.get_config_map_hanging_orders()

        new_tracker = self.strategy.hanging_orders_tracker
        self.assertIsNot(old_tracker, new_tracker)
        # Before the fix the old tracker's forwarders stayed registered forever.
        self.assertNotIn(old_forwarder, self.market.get_listeners(MarketEvent.OrderCancelled))
        self.assertIn(new_tracker._cancel_order_forwarder,
                      self.market.get_listeners(MarketEvent.OrderCancelled))


class ExecutionTimeframeValidationTest(unittest.TestCase):
    """PMM-13 — end must be strictly after start for both window types."""

    def test_from_date_to_date_rejects_end_before_start(self):
        with self.assertRaises(ValidationError) as ctx:
            FromDateToDateModel(start_datetime="2021-01-01 12:00:00",
                                end_datetime="2021-01-01 11:00:00")
        self.assertIn("must be after", str(ctx.exception))

    def test_from_date_to_date_rejects_equal_boundaries(self):
        with self.assertRaises(ValidationError):
            FromDateToDateModel(start_datetime="2021-01-01 12:00:00",
                                end_datetime="2021-01-01 12:00:00")

    def test_from_date_to_date_accepts_valid_window(self):
        model = FromDateToDateModel(start_datetime="2021-01-01 11:00:00",
                                    end_datetime="2021-01-01 12:00:00")
        self.assertLess(model.start_datetime, model.end_datetime)

    def test_daily_between_times_rejects_overnight_window(self):
        with self.assertRaises(ValidationError) as ctx:
            DailyBetweenTimesModel(start_time="22:00:00", end_time="06:00:00")
        self.assertIn("not supported", str(ctx.exception))

    def test_daily_between_times_rejects_equal_boundaries(self):
        with self.assertRaises(ValidationError):
            DailyBetweenTimesModel(start_time="12:00:00", end_time="12:00:00")

    def test_daily_between_times_accepts_valid_window(self):
        model = DailyBetweenTimesModel(start_time="09:00:00", end_time="17:00:00")
        self.assertLess(model.start_time, model.end_time)


if __name__ == "__main__":
    unittest.main()
