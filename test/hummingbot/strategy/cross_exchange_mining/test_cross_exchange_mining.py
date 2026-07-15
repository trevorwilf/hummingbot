"""
Regression tests for the CSF-V1 Phase 11 fixes to the cross exchange mining strategy
(findings ARB-5 and ARB-13 in STRATEGY_CONNECTOR_REVIEW_FINDINGS_V1.md).
"""
import asyncio
import unittest
from decimal import Decimal
from typing import List
from unittest.mock import patch

import pandas as pd

from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.client.config.config_var import ConfigVar
from hummingbot.client.settings import ConnectorSetting, ConnectorType
from hummingbot.connector.exchange.paper_trade.paper_trade_exchange import QuantizationParams
from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.core.clock import Clock, ClockMode
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_row import OrderBookRow
from hummingbot.core.data_type.trade_fee import TradeFeeSchema
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import BuyOrderCreatedEvent, MarketEvent
from hummingbot.strategy.cross_exchange_mining.cross_exchange_mining import CrossExchangeMiningStrategy
from hummingbot.strategy.cross_exchange_mining.cross_exchange_mining_config_map_pydantic import (
    CrossExchangeMiningConfigMap,
)
from hummingbot.strategy.cross_exchange_mining.cross_exchange_mining_pair import CrossExchangeMiningPair
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple


class CrossExchangeMiningStrategyTest(unittest.TestCase):
    start: pd.Timestamp = pd.Timestamp("2019-01-01", tz="UTC")
    end: pd.Timestamp = pd.Timestamp("2019-01-01 01:00:00", tz="UTC")
    start_timestamp: float = start.timestamp()
    end_timestamp: float = end.timestamp()
    maker_trading_pairs: List[str] = ["COINALPHA-WETH", "COINALPHA", "WETH"]
    taker_trading_pairs: List[str] = ["COINALPHA-ETH", "COINALPHA", "ETH"]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ev_loop = asyncio.get_event_loop()

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

    @patch("hummingbot.client.settings.AllConnectorSettings.get_exchange_names")
    @patch("hummingbot.client.settings.AllConnectorSettings.get_connector_settings")
    def setUp(self, get_connector_settings_mock, get_exchange_names_mock):
        get_exchange_names_mock.return_value = set(self.get_mock_connector_settings().keys())
        get_connector_settings_mock.return_value = self.get_mock_connector_settings()

        self.clock: Clock = Clock(ClockMode.BACKTEST, 1.0, self.start_timestamp, self.end_timestamp)
        self.maker_market: MockPaperExchange = MockPaperExchange()
        self.taker_market: MockPaperExchange = MockPaperExchange()
        self.maker_market.set_balanced_order_book(self.maker_trading_pairs[0], 1.0, 0.5, 1.5, 0.01, 10)
        self.taker_market.set_balanced_order_book(self.taker_trading_pairs[0], 1.0, 0.5, 1.5, 0.001, 10)
        self.maker_market.set_quantization_param(QuantizationParams(self.maker_trading_pairs[0], 5, 5, 5, 5))
        self.taker_market.set_quantization_param(QuantizationParams(self.taker_trading_pairs[0], 5, 5, 5, 5))

        self.market_pair: CrossExchangeMiningPair = CrossExchangeMiningPair(
            MarketTradingPairTuple(self.maker_market, *self.maker_trading_pairs),
            MarketTradingPairTuple(self.taker_market, *self.taker_trading_pairs),
        )

        config_map_raw = CrossExchangeMiningConfigMap(
            maker_market="mock_paper_exchange",
            taker_market="mock_paper_exchange",
            maker_market_trading_pair=self.maker_trading_pairs[0],
            taker_market_trading_pair=self.taker_trading_pairs[0],
            min_profitability=Decimal("0.5"),
            order_amount=Decimal("4"),
            balance_adjustment_duration=5.0,
            slippage_buffer=Decimal("1"),
            min_prof_tol_low=Decimal("0.1"),
            min_prof_tol_high=Decimal("0.1"),
            volatility_buffer_size=120,
            min_prof_adj_timer=3600.0,
            min_order_amount=Decimal("1"),
            rate_curve=Decimal("1"),
            trade_fee=Decimal("0.25"),
        )
        self.config_map = ClientConfigAdapter(config_map_raw)

        self.strategy: CrossExchangeMiningStrategy = CrossExchangeMiningStrategy()
        self.strategy.init_params(
            config_map=self.config_map,
            market_pairs=[self.market_pair],
        )
        self.clock.add_iterator(self.maker_market)
        self.clock.add_iterator(self.taker_market)
        self.clock.add_iterator(self.strategy)

        self.maker_order_created_logger: EventLogger = EventLogger()
        self.taker_order_created_logger: EventLogger = EventLogger()
        self.maker_market.add_listener(MarketEvent.BuyOrderCreated, self.maker_order_created_logger)
        self.maker_market.add_listener(MarketEvent.SellOrderCreated, self.maker_order_created_logger)
        self.taker_market.add_listener(MarketEvent.BuyOrderCreated, self.taker_order_created_logger)
        self.taker_market.add_listener(MarketEvent.SellOrderCreated, self.taker_order_created_logger)

    def _drain_event_loop(self):
        """The paper exchange emits order events via the event loop - let them flush."""
        self.ev_loop.run_until_complete(asyncio.sleep(0.2))

    @staticmethod
    def _empty_order_book(order_book: OrderBook):
        update_id = order_book.last_diff_uid + 1
        bid_diffs = [OrderBookRow(r.price, 0, update_id) for r in order_book.bid_entries()]
        ask_diffs = [OrderBookRow(r.price, 0, update_id) for r in order_book.ask_entries()]
        order_book.apply_diffs(bid_diffs, ask_diffs, update_id)

    def test_rebalance_with_depleted_taker_quote_does_not_crash(self):
        """Would have caught ARB-5: zero taker quote balance -> taker_qty == 0 -> `taker_price`
        was unbound when the maker-branch comparison evaluated -> UnboundLocalError every tick.
        Post-fix the maker rebalance option is still taken."""
        # Total base (0) far below order_amount (4) -> rebalance is required.
        self.maker_market.set_balance("COINALPHA", 0)
        self.taker_market.set_balance("COINALPHA", 0)
        # Taker cannot fund the buy (quote balance 0) but the maker can.
        self.taker_market.set_balance("ETH", 0)
        self.maker_market.set_balance("WETH", 100)

        # Would raise UnboundLocalError from check_balance before the fix
        self.clock.backtest_til(self.start_timestamp + 1)
        self._drain_event_loop()

        buy_created = [e for e in self.maker_order_created_logger.event_log
                       if isinstance(e, BuyOrderCreatedEvent)]
        self.assertEqual(1, len(buy_created))
        # The rebalance order is sized to the deficit (4 * (1 - tol)) via the maker leg
        self.assertAlmostEqual(float(Decimal("4") * (Decimal("1") - Decimal("0.001"))),
                               float(buy_created[0].amount), places=3)
        # Nothing was sent to the (unfunded) taker side
        self.assertEqual(0, len(self.taker_order_created_logger.event_log))

    def test_empty_taker_book_skips_tick_without_querying_wrong_exchange(self):
        """Would have caught ARB-13: the ZeroDivisionError fallback queried the OTHER exchange's
        book with this exchange's trading pair (KeyError crash). Post-fix the side is skipped."""
        self.maker_market.set_balance("COINALPHA", 2)
        self.taker_market.set_balance("COINALPHA", 2)
        self.maker_market.set_balance("WETH", 100)
        self.taker_market.set_balance("ETH", 100)
        self._empty_order_book(self.taker_market.get_order_book(self.taker_trading_pairs[0]))

        # Would raise (KeyError on the maker market's missing COINALPHA-ETH book) before the fix
        self.clock.backtest_til(self.start_timestamp + 3)
        self._drain_event_loop()

        self.assertEqual(0, len(self.maker_order_created_logger.event_log))
        self.assertEqual(0, len(self.taker_order_created_logger.event_log))

    def test_active_orders_indexed_by_market_pair(self):
        """ARB-13: active limit orders must be looked up under the strategy's own maker market pair;
        placed orders are found again on later ticks (no duplicates re-placed)."""
        # Total base within [order_amount - min_order_amount, order_amount + min_order_amount]
        # so no rebalancing interferes.
        self.maker_market.set_balance("COINALPHA", 2)
        self.taker_market.set_balance("COINALPHA", 2)
        self.maker_market.set_balance("WETH", 100)
        self.taker_market.set_balance("ETH", 100)

        # Orders are only placed once the initial balance check clears (_balance_flag)
        self.clock.backtest_til(self.start_timestamp + 2)
        self._drain_event_loop()
        self.assertEqual(2, len(self.strategy._sb_order_tracker.active_limit_orders))

        # On subsequent ticks the same orders are found (and kept) - none are duplicated
        self.clock.backtest_til(self.start_timestamp + 5)
        self._drain_event_loop()
        self.assertEqual(2, len(self.strategy._sb_order_tracker.active_limit_orders))
        self.assertEqual(2, len(self.maker_market.limit_orders))
        self.assertEqual(2, len(self.maker_order_created_logger.event_log))


if __name__ == "__main__":
    unittest.main()
