"""
Regression tests for the CSF-V1 Phase 11 fixes to the cross exchange market making strategy
(findings ARB-1, ARB-2, ARB-3, ARB-6, ARB-7 and ARB-14 in STRATEGY_CONNECTOR_REVIEW_FINDINGS_V1.md).
"""
import asyncio
import unittest
from decimal import Decimal
from typing import Awaitable, List
from unittest.mock import AsyncMock, patch

import pandas as pd

from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.client.config.config_var import ConfigVar
from hummingbot.client.settings import ConnectorSetting, ConnectorType
from hummingbot.connector.exchange.paper_trade.paper_trade_exchange import QuantizationParams
from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.core.clock import Clock, ClockMode
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_row import OrderBookRow
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TradeFeeSchema
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import (
    MarketEvent,
    MarketOrderFailureEvent,
    OrderBookTradeEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
)
from hummingbot.strategy.cross_exchange_market_making.cross_exchange_market_making import (
    CrossExchangeMarketMakingStrategy,
)
from hummingbot.strategy.cross_exchange_market_making.cross_exchange_market_making_config_map_pydantic import (
    ActiveOrderRefreshMode,
    CrossExchangeMarketMakingConfigMap,
    TakerToMakerConversionRateMode,
)
from hummingbot.strategy.maker_taker_market_pair import MakerTakerMarketPair
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple

s_decimal_nan = Decimal("nan")


class XEMMArbFixesTest(unittest.TestCase):
    start: pd.Timestamp = pd.Timestamp("2019-01-01", tz="UTC")
    end: pd.Timestamp = pd.Timestamp("2019-01-01 01:00:00", tz="UTC")
    start_timestamp: float = start.timestamp()
    end_timestamp: float = end.timestamp()
    maker_trading_pairs: List[str] = ["COINALPHA-WETH", "COINALPHA", "WETH"]
    taker_trading_pairs: List[str] = ["COINALPHA-ETH", "COINALPHA", "ETH"]

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

    @patch("hummingbot.client.settings.AllConnectorSettings.get_exchange_names")
    @patch("hummingbot.client.settings.AllConnectorSettings.get_connector_settings")
    def setUp(self, get_connector_settings_mock, get_exchange_names_mock):
        self.log_records = []
        get_exchange_names_mock.return_value = set(self.get_mock_connector_settings().keys())
        get_connector_settings_mock.return_value = self.get_mock_connector_settings()

        self.clock: Clock = Clock(ClockMode.BACKTEST, 1.0, self.start_timestamp, self.end_timestamp)
        self.maker_market: MockPaperExchange = MockPaperExchange()
        self.taker_market: MockPaperExchange = MockPaperExchange()
        self.maker_market.set_balanced_order_book(self.maker_trading_pairs[0], 1.0, 0.5, 1.5, 0.01, 10)
        self.taker_market.set_balanced_order_book(self.taker_trading_pairs[0], 1.0, 0.5, 1.5, 0.001, 4)
        self.maker_market.set_balance("COINALPHA", 5)
        self.maker_market.set_balance("WETH", 5)
        self.taker_market.set_balance("COINALPHA", 5)
        self.taker_market.set_balance("ETH", 5)
        self.maker_market.set_quantization_param(QuantizationParams(self.maker_trading_pairs[0], 5, 5, 5, 5))
        self.taker_market.set_quantization_param(QuantizationParams(self.taker_trading_pairs[0], 5, 5, 5, 5))

        self.market_pair: MakerTakerMarketPair = MakerTakerMarketPair(
            MarketTradingPairTuple(self.maker_market, *self.maker_trading_pairs),
            MarketTradingPairTuple(self.taker_market, *self.taker_trading_pairs),
        )

        config_map_raw = CrossExchangeMarketMakingConfigMap(
            maker_market="mock_paper_exchange",
            taker_market="mock_paper_exchange",
            maker_market_trading_pair=self.maker_trading_pairs[0],
            taker_market_trading_pair=self.taker_trading_pairs[0],
            min_profitability=Decimal("0.5"),
            slippage_buffer=Decimal("5"),  # 5% - exercised by the ARB-3 sizing fix
            order_amount=Decimal("0"),
            order_size_taker_volume_factor=Decimal("25"),
            order_size_taker_balance_factor=Decimal("99.5"),
            order_size_portfolio_ratio_limit=Decimal("30"),
            adjust_order_enabled=True,
            anti_hysteresis_duration=60.0,
            order_refresh_mode=ActiveOrderRefreshMode(),
            top_depth_tolerance=Decimal(0),
            conversion_rate_mode=TakerToMakerConversionRateMode(),
        )
        config_map_raw.conversion_rate_mode.taker_to_maker_base_conversion_rate = Decimal("1.0")
        config_map_raw.conversion_rate_mode.taker_to_maker_quote_conversion_rate = Decimal("1.0")
        self.config_map = ClientConfigAdapter(config_map_raw)

        self.strategy: CrossExchangeMarketMakingStrategy = CrossExchangeMarketMakingStrategy()
        self.strategy.init_params(
            config_map=self.config_map,
            market_pairs=[self.market_pair],
            logging_options=CrossExchangeMarketMakingStrategy.OPTION_LOG_ALL,
        )
        self.strategy.logger().setLevel(1)
        self.strategy.logger().addHandler(self)

        self.clock.add_iterator(self.maker_market)
        self.clock.add_iterator(self.taker_market)
        self.clock.add_iterator(self.strategy)

        self.taker_order_created_logger: EventLogger = EventLogger()
        self.taker_market.add_listener(MarketEvent.BuyOrderCreated, self.taker_order_created_logger)
        self.taker_market.add_listener(MarketEvent.SellOrderCreated, self.taker_order_created_logger)

    def tearDown(self):
        self.strategy.logger().removeHandler(self)
        super().tearDown()

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: int = 1):
        return self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))

    def get_mock_connector_settings(self):
        conf_var_connector = ConfigVar(key='mock_paper_exchange', prompt="")
        conf_var_connector.value = 'mock_paper_exchange'
        return {
            "mock_paper_exchange": ConnectorSetting(
                name='mock_paper_exchange',
                type=ConnectorType.Exchange,
                example_pair='ZRX-ETH',
                centralised=True,
                use_ethereum_wallet=False,
                trade_fee_schema=TradeFeeSchema(
                    percent_fee_token=None,
                    maker_percent_fee_decimal=Decimal('0.001'),
                    taker_percent_fee_decimal=Decimal('0.001'),
                    buy_percent_fee_deducted_from_returns=False,
                    maker_fixed_fees=[],
                    taker_fixed_fees=[]),
                config_keys={'connector': conf_var_connector},
                is_sub_domain=False,
                parent_name=None,
                domain_parameter=None,
                use_eth_gas_lookup=False)
        }

    # --------------------------------------------------------------------------------------------------
    # Helpers

    def _register_hedged_maker_fill(self,
                                    maker_id: str = "maker_1",
                                    taker_id: str = "taker_1",
                                    trade_id: str = "trade_1",
                                    maker_is_buy: bool = True,
                                    amount: Decimal = Decimal("3"),
                                    price: Decimal = Decimal("0.99")):
        """Wire the strategy bookkeeping as if a maker fill is currently being hedged by a taker order."""
        strategy = self.strategy
        strategy._maker_to_taker_order_ids[maker_id] = [taker_id]
        strategy._taker_to_maker_order_ids[taker_id] = maker_id
        strategy._maker_to_hedging_trades[maker_id] = [trade_id]
        limit_order = LimitOrder(maker_id, self.maker_trading_pairs[0], maker_is_buy,
                                 self.maker_trading_pairs[1], self.maker_trading_pairs[2], price, amount)
        fill_event = OrderFilledEvent(
            self.start_timestamp, maker_id, self.maker_trading_pairs[0],
            TradeType.BUY if maker_is_buy else TradeType.SELL, OrderType.LIMIT,
            price, amount, AddedToCostTradeFee(Decimal(0)), trade_id)
        record = (limit_order, fill_event)
        if maker_is_buy:
            strategy._order_fill_buy_events[self.market_pair] = [record]
        else:
            strategy._order_fill_sell_events[self.market_pair] = [record]
        strategy._ongoing_hedging[(trade_id,)] = taker_id
        strategy._market_pair_tracker.start_tracking_order_id(maker_id, self.maker_market, self.market_pair)
        strategy._market_pair_tracker.start_tracking_order_id(taker_id, self.taker_market, self.market_pair)
        return record

    @staticmethod
    def _empty_order_book_side(order_book: OrderBook, bids: bool = True, asks: bool = True):
        update_id = order_book.last_diff_uid + 1
        bid_diffs = [OrderBookRow(r.price, 0, update_id) for r in order_book.bid_entries()] if bids else []
        ask_diffs = [OrderBookRow(r.price, 0, update_id) for r in order_book.ask_entries()] if asks else []
        order_book.apply_diffs(bid_diffs, ask_diffs, update_id)

    def simulate_maker_market_trade(self, is_buy: bool, quantity: Decimal, price: Decimal):
        order_book: OrderBook = self.maker_market.get_order_book(self.maker_trading_pairs[0])
        trade_event = OrderBookTradeEvent(
            self.maker_trading_pairs[0], self.clock.current_timestamp,
            TradeType.BUY if is_buy else TradeType.SELL, price, quantity)
        order_book.apply_trade(trade_event)

    # --------------------------------------------------------------------------------------------------
    # ARB-1

    def test_taker_hedge_failure_releases_wedge_and_rehedges_with_maker_id(self):
        """Would have caught ARB-1: the failed taker hedge must release the _ongoing_hedging entry
        (so the strategy can trade again) and the retry must be issued for the MAKER order id."""
        self._register_hedged_maker_fill()
        strategy = self.strategy

        self.assertFalse(strategy.ready_for_new_trades())

        with patch.object(CrossExchangeMarketMakingStrategy, "check_and_hedge_orders",
                          new_callable=AsyncMock) as hedge_mock:
            strategy.did_fail_order(MarketOrderFailureEvent(self.start_timestamp, "taker_1", OrderType.LIMIT))
            self.async_run_with_timeout(asyncio.sleep(0.05))

        # The stale ongoing hedging entry is gone -> main loop unblocked
        self.assertEqual(0, len(strategy._ongoing_hedging))
        self.assertTrue(strategy.ready_for_new_trades())
        self.assertNotIn("taker_1", strategy._taker_to_maker_order_ids)
        # The retry was issued with the MAKER order id (the old code passed the taker id)
        hedge_mock.assert_called_once()
        called_order_id, called_market_pair = hedge_mock.call_args[0]
        self.assertEqual("maker_1", called_order_id)
        self.assertEqual(self.market_pair, called_market_pair)
        # The fill record is unhedged again, available for the retry
        self.assertEqual(1, len(strategy.get_unhedged_buy_records(self.market_pair)))

    @patch("hummingbot.client.settings.AllConnectorSettings.get_exchange_names")
    @patch("hummingbot.client.settings.AllConnectorSettings.get_connector_settings")
    @patch("hummingbot.strategy.cross_exchange_market_making.cross_exchange_market_making."
           "CrossExchangeMarketMakingStrategy.is_gateway_market")
    def test_taker_hedge_failure_end_to_end_resubmits_taker_order(self, is_gateway_mock,
                                                                  get_connector_settings_mock,
                                                                  get_exchange_names_mock):
        """End to end: maker fill -> taker hedge -> taker failure -> a NEW taker hedge order is placed
        for the same maker order, and completing it unblocks trading."""
        is_gateway_mock.return_value = False
        get_exchange_names_mock.return_value = set(self.get_mock_connector_settings().keys())
        get_connector_settings_mock.return_value = self.get_mock_connector_settings()
        self.clock.backtest_til(self.start_timestamp + 5)
        if len(self.strategy.active_maker_bids) == 0:
            self.async_run_with_timeout(asyncio.sleep(0.5))
        self.assertGreaterEqual(len(self.strategy.active_maker_bids), 1)
        bid_order: LimitOrder = self.strategy.active_maker_bids[0][1]

        # Fill the maker bid -> the strategy hedges with a taker sell
        self.simulate_maker_market_trade(False, Decimal("10.0"), bid_order.price * Decimal("0.99"))
        self.async_run_with_timeout(asyncio.sleep(0.1))

        self.assertEqual(1, len(self.strategy._taker_to_maker_order_ids))
        first_taker_id = list(self.strategy._taker_to_maker_order_ids.keys())[0]
        maker_id = self.strategy._taker_to_maker_order_ids[first_taker_id]
        self.assertFalse(self.strategy.ready_for_new_trades())

        # The taker hedge fails
        self.taker_market.trigger_event(
            MarketEvent.OrderFailure,
            MarketOrderFailureEvent(self.start_timestamp + 6, first_taker_id, OrderType.LIMIT))
        self.async_run_with_timeout(asyncio.sleep(0.1))

        # A NEW taker hedge order was submitted for the same maker order
        self.assertEqual(1, len(self.strategy._taker_to_maker_order_ids))
        second_taker_id = list(self.strategy._taker_to_maker_order_ids.keys())[0]
        self.assertNotEqual(first_taker_id, second_taker_id)
        self.assertEqual(maker_id, self.strategy._taker_to_maker_order_ids[second_taker_id])
        self.assertTrue(self._is_logged("WARNING", "Resubmitting hedge"))

        # Completing the new hedge unblocks trading
        self.taker_market.trigger_event(
            MarketEvent.SellOrderCompleted,
            SellOrderCompletedEvent(self.start_timestamp + 7, second_taker_id,
                                    self.taker_trading_pairs[1], self.taker_trading_pairs[2],
                                    Decimal("3"), Decimal("3"), OrderType.LIMIT))
        self.assertTrue(self.strategy.ready_for_new_trades())

    def test_consecutive_hedge_failures_escalate(self):
        """ARB-2c: after HEDGE_FAILURE_ALERT_THRESHOLD consecutive failures an ERROR escalation is logged."""
        strategy = self.strategy
        with patch.object(CrossExchangeMarketMakingStrategy, "check_and_hedge_orders", new_callable=AsyncMock):
            for attempt in range(strategy.HEDGE_FAILURE_ALERT_THRESHOLD):
                taker_id = f"taker_{attempt}"
                self._register_hedged_maker_fill(maker_id="maker_1", taker_id=taker_id,
                                                 trade_id=f"trade_{attempt}")
                strategy.did_fail_order(
                    MarketOrderFailureEvent(self.start_timestamp, taker_id, OrderType.LIMIT))
            self.async_run_with_timeout(asyncio.sleep(0.05))
        self.assertTrue(self._is_logged(
            "ERROR",
            f"Hedging maker order maker_1 has failed {strategy.HEDGE_FAILURE_ALERT_THRESHOLD} consecutive times"))

    # --------------------------------------------------------------------------------------------------
    # ARB-2

    def test_taker_completion_keeps_unhedged_fill_records(self):
        """Would have caught ARB-2a: completing one hedge must NOT blanket-discard fill records that
        were never part of that hedge."""
        strategy = self.strategy
        self._register_hedged_maker_fill(maker_id="maker_1", taker_id="taker_1", trade_id="trade_A",
                                         maker_is_buy=False)
        # A second, NOT yet hedged fill record for another maker order
        unhedged_order = LimitOrder("maker_2", self.maker_trading_pairs[0], False,
                                    self.maker_trading_pairs[1], self.maker_trading_pairs[2],
                                    Decimal("1.01"), Decimal("2"))
        unhedged_fill = OrderFilledEvent(
            self.start_timestamp, "maker_2", self.maker_trading_pairs[0], TradeType.SELL, OrderType.LIMIT,
            Decimal("1.01"), Decimal("2"), AddedToCostTradeFee(Decimal(0)), "trade_B")
        strategy._maker_to_taker_order_ids["maker_2"] = []
        strategy._order_fill_sell_events[self.market_pair].append((unhedged_order, unhedged_fill))

        # The taker BUY hedge for trade_A completes (maker sell fills are hedged by taker buys)
        with patch.object(strategy, "notify_hb_app_with_timestamp"):
            strategy.did_complete_buy_order(
                SellOrderCompletedEvent(self.start_timestamp, "taker_1",
                                        self.taker_trading_pairs[1], self.taker_trading_pairs[2],
                                        Decimal("3"), Decimal("3"), OrderType.LIMIT))

        # trade_A (hedged) removed, trade_B (never hedged) preserved
        remaining = strategy._order_fill_sell_events.get(self.market_pair, [])
        self.assertEqual(1, len(remaining))
        self.assertEqual("trade_B", remaining[0][1].exchange_trade_id)
        self.assertEqual(0, len(strategy._ongoing_hedging))

    def test_zero_quantized_hedge_logs_warning_and_keeps_records(self):
        """ARB-2b: a hedge that quantizes to zero logs a WARNING (not INFO) and leaves the fill
        records intact for a later retry."""
        strategy = self.strategy
        record = self._register_hedged_maker_fill(maker_is_buy=True)
        # Release the ongoing hedge so the fill is considered unhedged
        strategy._ongoing_hedging.clear()
        # No taker base balance -> hedge amount quantizes to zero
        self.taker_market.set_balance("COINALPHA", 0)

        self.async_run_with_timeout(strategy.check_and_hedge_orders("maker_1", self.market_pair))

        self.assertTrue(self._is_logged("WARNING", "No hedging possible yet"))
        self.assertEqual([record], strategy._order_fill_buy_events[self.market_pair])
        self.assertEqual(0, len(strategy._ongoing_hedging))

    def test_nan_taker_price_skips_hedge_and_keeps_records(self):
        """ARB-2b: an empty taker book (NaN price) must not raise nor place a NaN order - the fill
        records stay for retry."""
        strategy = self.strategy
        record = self._register_hedged_maker_fill(maker_is_buy=False)
        strategy._ongoing_hedging.clear()
        self._empty_order_book_side(self.taker_market.get_order_book(self.taker_trading_pairs[0]))

        with patch.object(strategy, "place_order") as place_order_mock:
            self.async_run_with_timeout(strategy.check_and_hedge_orders("maker_1", self.market_pair))

        place_order_mock.assert_not_called()
        self.assertTrue(self._is_logged("WARNING", "Hedge will be retried"))
        self.assertEqual([record], strategy._order_fill_sell_events[self.market_pair])

    # --------------------------------------------------------------------------------------------------
    # ARB-3

    def test_taker_buy_hedge_sizing_accounts_for_slippage_buffer(self):
        """Would have caught ARB-3: the balance-capped taker BUY hedge must divide by the
        slippage-adjusted price, otherwise the submitted order needs ~1.05x the available balance."""
        strategy = self.strategy
        self._register_hedged_maker_fill(maker_is_buy=False, amount=Decimal("3"))
        strategy._ongoing_hedging.clear()
        # Make the quote balance the binding constraint
        self.taker_market.set_balance("ETH", 1)

        taker_pair = self.taker_trading_pairs[0]
        taker_price = self.taker_market.get_price_for_volume(taker_pair, True, Decimal("3")).result_price
        slippage_factor = Decimal("1") + Decimal("5") / Decimal("100")
        balance_factor = Decimal("99.5") / Decimal("100")
        expected_amount = self.taker_market.quantize_order_amount(
            taker_pair,
            min(Decimal("3"),
                self.taker_market.get_available_balance("ETH") / (taker_price * slippage_factor) * balance_factor))

        with patch.object(strategy, "place_order") as place_order_mock:
            self.async_run_with_timeout(strategy.check_and_hedge_orders("maker_1", self.market_pair))

        place_order_mock.assert_called_once()
        placed_amount = place_order_mock.call_args[0][3]
        placed_price = place_order_mock.call_args[0][4]
        self.assertEqual(expected_amount, placed_amount)
        # The submitted order is affordable at its own (slippage adjusted) price
        self.assertLessEqual(placed_amount * placed_price, self.taker_market.get_available_balance("ETH"))

    # --------------------------------------------------------------------------------------------------
    # ARB-6 / ARB-7

    def test_empty_maker_bid_book_does_not_crash_market_making_price(self):
        """Would have caught ARB-6: an empty maker BID book raised UnboundLocalError on price_above_bid."""
        self._empty_order_book_side(self.maker_market.get_order_book(self.maker_trading_pairs[0]),
                                    bids=True, asks=False)
        price = self.async_run_with_timeout(
            self.strategy.get_market_making_price(self.market_pair, True, Decimal("1")))
        self.assertFalse(Decimal.is_nan(price))

    def test_nan_hedging_price_cancels_maker_order(self):
        """Would have caught ARB-7: a NaN hedging price raised InvalidOperation instead of cancelling."""
        strategy = self.strategy
        active_order = LimitOrder("maker_1", self.maker_trading_pairs[0], True,
                                  self.maker_trading_pairs[1], self.maker_trading_pairs[2],
                                  Decimal("0.99"), Decimal("3"))
        with patch.object(strategy, "cancel_maker_order") as cancel_mock:
            stays = self.async_run_with_timeout(
                strategy.check_if_still_profitable(self.market_pair, active_order, s_decimal_nan))
        self.assertFalse(stays)
        cancel_mock.assert_called_once_with(self.market_pair, "maker_1")

    def test_market_making_size_returns_zero_on_empty_taker_book(self):
        """ARB-7: the ZeroDivisionError asserts are replaced with a fail-closed zero size."""
        self._empty_order_book_side(self.taker_market.get_order_book(self.taker_trading_pairs[0]))
        bid_size = self.async_run_with_timeout(self.strategy.get_market_making_size(self.market_pair, True))
        ask_size = self.async_run_with_timeout(self.strategy.get_market_making_size(self.market_pair, False))
        self.assertEqual(Decimal("0"), bid_size)
        self.assertEqual(Decimal("0"), ask_size)

    # --------------------------------------------------------------------------------------------------
    # ARB-14

    def test_stop_tracking_limit_order_override_is_not_recursive(self):
        """Would have caught ARB-14: the override called itself with shifted args (TypeError/recursion)."""
        strategy = self.strategy
        strategy._market_pair_tracker.tick(self.start_timestamp)
        strategy._market_pair_tracker.start_tracking_order_id("oid_1", self.maker_market, self.market_pair)
        # Must not raise
        strategy.stop_tracking_limit_order(self.market_pair.maker, "oid_1")
        strategy.stop_tracking_market_order(self.market_pair.maker, "oid_1")

        # The tracker entry now expires after the keep-alive window
        tracker = strategy._market_pair_tracker
        tracker.tick(self.start_timestamp + 10 * 60)
        self.assertIsNone(tracker.get_market_pair_from_order_id("oid_1"))

    def test_terminated_maker_order_expires_from_market_pair_tracker(self):
        """ARB-14: cancel/fail/expire events for non-taker order ids mark the tracker entry for expiry."""
        strategy = self.strategy
        tracker = strategy._market_pair_tracker
        tracker.tick(self.start_timestamp)
        tracker.start_tracking_order_id("maker_x", self.maker_market, self.market_pair)

        strategy.did_cancel_order(OrderCancelledEvent(self.start_timestamp, "maker_x"))
        # Entry survives the grace window ...
        self.assertEqual(self.market_pair, tracker.get_market_pair_from_order_id("maker_x"))
        # ... and is purged after it
        tracker.tick(self.start_timestamp + 10 * 60)
        self.assertIsNone(tracker.get_market_pair_from_order_id("maker_x"))


if __name__ == "__main__":
    unittest.main()
