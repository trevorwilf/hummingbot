"""XEMM / Arbitrage executor hardening tests (V2 strategy fixes phase 6 — B8).

Under test:
- Arbitrage: the failed-order event handler now increments BEFORE the retry gate, so a
  burst of failure events cannot re-place market legs past max_retries at stale prices.
- XEMM: a degenerate maker-target-price denominator (target_profitability + tx_cost_pct
  >= 1) skips maker placement with a rate-limited warning instead of exploding/flipping
  the maker price; shutdown with None legs terminates instead of raising; the default
  taker hedge stays a MARKET order (no config added — DEFERRED slippage bound).
"""
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.events import MarketOrderFailureEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.arbitrage_executor.arbitrage_executor import ArbitrageExecutor
from hummingbot.strategy_v2.executors.arbitrage_executor.data_types import ArbitrageExecutorConfig
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.strategy_v2.executors.xemm_executor.xemm_executor import XEMMExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus


def create_mock_strategy():
    market = MagicMock()
    market_info = MagicMock()
    market_info.market = market

    strategy = MagicMock(spec=StrategyV2Base)
    type(strategy).market_info = PropertyMock(return_value=market_info)
    type(strategy).trading_pair = PropertyMock(return_value="ETH-USDT")
    strategy.current_timestamp = 1000.0
    strategy.buy.side_effect = [f"OID-BUY-{i}" for i in range(1, 30)]
    strategy.sell.side_effect = [f"OID-SELL-{i}" for i in range(1, 30)]
    strategy.cancel.return_value = None
    binance_connector = MagicMock(spec=ExchangePyBase)
    binance_connector.supported_order_types = MagicMock(return_value=[OrderType.LIMIT, OrderType.MARKET])
    kucoin_connector = MagicMock(spec=ExchangePyBase)
    kucoin_connector.supported_order_types = MagicMock(return_value=[OrderType.LIMIT, OrderType.MARKET])
    strategy.connectors = {
        "binance": binance_connector,
        "kucoin": kucoin_connector,
    }
    return strategy


class TestArbitrageRetryCeiling(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    def setUp(self):
        super().setUp()
        self.strategy = create_mock_strategy()
        config = ArbitrageExecutorConfig(
            timestamp=1234,
            order_amount=Decimal("10"),
            min_profitability=Decimal("0.01"),
            buying_market=ConnectorPair(connector_name="binance", trading_pair="ETH-USDT"),
            selling_market=ConnectorPair(connector_name="kucoin", trading_pair="ETH-USDT"),
        )
        self.executor = ArbitrageExecutor(self.strategy, config)
        self.set_loggers(loggers=[self.executor.logger()])

    def test_failure_events_stop_replacing_at_the_ceiling(self):
        self.executor._status = RunnableStatus.SHUTTING_DOWN
        max_retries = self.executor.max_retries

        with patch.object(ArbitrageExecutor, "place_buy_arbitrage_order") as place_mock:
            # Keep the failed order id stable so every event matches the buy leg.
            self.executor.buy_order.order_id = "OID-BUY-X"
            for i in range(max_retries + 5):
                event = MarketOrderFailureEvent(timestamp=1.0, order_id="OID-BUY-X", order_type=MagicMock())
                self.executor.process_order_failed_event("tag", MagicMock(), event)

        # Re-placed only while failures <= max_retries — never past the ceiling.
        self.assertEqual(max_retries, place_mock.call_count)
        self.assertEqual(max_retries + 5, self.executor._cumulative_failures)

    async def test_control_loop_still_terminates_failed_past_ceiling(self):
        self.executor._status = RunnableStatus.SHUTTING_DOWN
        self.executor._cumulative_failures = self.executor.max_retries + 1
        await self.executor.control_task()
        from hummingbot.strategy_v2.models.executors import CloseType
        self.assertEqual(CloseType.FAILED, self.executor.close_type)
        self.assertEqual(RunnableStatus.TERMINATED, self.executor.status)


class TestXEMMHardening(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    def setUp(self):
        super().setUp()
        self.strategy = create_mock_strategy()
        config = XEMMExecutorConfig(
            timestamp=1234,
            buying_market=ConnectorPair(connector_name="binance", trading_pair="ETH-USDT"),
            selling_market=ConnectorPair(connector_name="kucoin", trading_pair="ETH-USDT"),
            maker_side=TradeType.BUY,
            order_amount=Decimal("100"),
            min_profitability=Decimal("0.01"),
            target_profitability=Decimal("0.015"),
            max_profitability=Decimal("0.02"),
        )
        self.executor = XEMMExecutor(self.strategy, config)
        self.set_loggers(loggers=[self.executor.logger()])

    async def test_degenerate_denominator_skips_maker_placement_with_warning(self):
        # maker BUY -> taker SELL -> denominator = 1 + target + tx_cost is safe; force
        # the SELL-maker branch by rebuilding with maker_side=SELL.
        config = XEMMExecutorConfig(
            timestamp=1234,
            buying_market=ConnectorPair(connector_name="binance", trading_pair="ETH-USDT"),
            selling_market=ConnectorPair(connector_name="kucoin", trading_pair="ETH-USDT"),
            maker_side=TradeType.SELL,
            order_amount=Decimal("100"),
            min_profitability=Decimal("0.01"),
            target_profitability=Decimal("0.5"),
            max_profitability=Decimal("0.9"),
        )
        executor = XEMMExecutor(self.strategy, config)
        self.set_loggers(loggers=[executor.logger()])
        executor._status = RunnableStatus.RUNNING
        with patch.object(XEMMExecutor, "get_resulting_price_for_amount",
                          AsyncMock(return_value=Decimal("100"))), \
                patch.object(XEMMExecutor, "update_tx_costs", AsyncMock()), \
                patch.object(XEMMExecutor, "control_maker_order", AsyncMock()) as maker_mock:
            executor._tx_cost_pct = Decimal("0.6")  # 0.5 + 0.6 > 1 -> denominator <= 0
            await executor.control_task()
        maker_mock.assert_not_called()
        self.assertTrue(self.is_partially_logged("WARNING", "degenerate maker target price"))

    async def test_healthy_denominator_places_maker(self):
        executor = self.executor
        executor._status = RunnableStatus.RUNNING
        with patch.object(XEMMExecutor, "get_resulting_price_for_amount",
                          AsyncMock(return_value=Decimal("100"))), \
                patch.object(XEMMExecutor, "update_tx_costs", AsyncMock()), \
                patch.object(XEMMExecutor, "control_maker_order", AsyncMock()) as maker_mock:
            executor._tx_cost_pct = Decimal("0.001")
            await executor.control_task()
        maker_mock.assert_called_once()
        # maker BUY -> taker SELL -> denominator = 1 + target + tx
        expected = Decimal("100") / (Decimal("1") + Decimal("0.015") + Decimal("0.001"))
        self.assertEqual(expected, executor._maker_target_price)

    async def test_shutdown_with_none_orders_does_not_raise(self):
        executor = self.executor
        executor._status = RunnableStatus.SHUTTING_DOWN
        executor.maker_order = None
        executor.taker_order = None
        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.TERMINATED, executor.status)

    async def test_shutdown_waits_for_live_taker(self):
        executor = self.executor
        executor._status = RunnableStatus.SHUTTING_DOWN
        executor.maker_order = None
        taker = MagicMock()
        taker.is_done = False
        executor.taker_order = taker
        await executor.control_shutdown_process()
        self.assertEqual(RunnableStatus.SHUTTING_DOWN, executor.status)

    def test_default_taker_hedge_is_market_order(self):
        # DEFERRED slippage bound: the default hedge path must remain a MARKET order.
        executor = self.executor
        executor.place_taker_order()
        call = self.strategy.sell.call_args  # maker BUY -> taker SELL
        self.assertEqual(OrderType.MARKET, call.kwargs.get("order_type", call.args[3] if len(call.args) > 3 else None))


if __name__ == "__main__":
    unittest.main()
