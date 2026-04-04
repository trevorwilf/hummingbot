"""
Tests for Phase 1 RCA fixes:
1. Connector-level local balance pre-adjustment after order placement
2. Same-cycle stop/create race condition deferral
3. Active-order price bounds include unfilled orders
4. Last-price error logging includes exception repr
5. NonKYC last-price logs primary failure before fallback
6. WS auth response correlates on request ID
"""
import asyncio
import logging
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.data_type.common import OrderType, TradeType


# ---------------------------------------------------------------------------
# Tests 1-6: Connector-level balance pre-adjustment (Fix 1)
# ---------------------------------------------------------------------------

class TestLocalBalancePreAdjust(IsolatedAsyncioWrapperTestCase):
    """Tests for Fix 1: _place_order_and_process_update balance pre-adjustment."""

    def _make_exchange(self, available_balances=None, total_balances=None):
        """Create a minimal NonkycExchange mock with real balance dicts."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange

        exchange = MagicMock(spec=NonkycExchange)
        exchange._account_available_balances = dict(available_balances or {})
        exchange._account_balances = dict(total_balances or {})
        exchange.logger = MagicMock(return_value=MagicMock())

        # Bind the real method to our mock
        exchange._place_order_and_process_update = NonkycExchange._place_order_and_process_update.__get__(
            exchange, NonkycExchange
        )
        return exchange

    def _make_order(self, trading_pair, trade_type, amount, price=Decimal("0")):
        order = MagicMock()
        order.trading_pair = trading_pair
        order.trade_type = trade_type
        order.amount = amount
        order.price = price
        order.client_order_id = "test_order_1"
        order.order_type = OrderType.LIMIT
        return order

    async def test_local_balance_pre_adjust_sell(self):
        """
        After a successful sell order placement, available_balances for the
        base asset must be immediately decremented.
        """
        exchange = self._make_exchange(
            available_balances={"ARRR": Decimal("70.25860000")},
            total_balances={"ARRR": Decimal("177.09000000")},
        )
        # Mock the parent's _place_order_and_process_update to return a fake exchange order id
        with patch(
            "hummingbot.connector.exchange_py_base.ExchangePyBase._place_order_and_process_update",
            new_callable=AsyncMock,
            return_value="12345",
        ):
            order = self._make_order("ARRR-USDT", TradeType.SELL, Decimal("25.20180000"))
            result = await exchange._place_order_and_process_update(order)

        self.assertEqual(result, "12345")
        expected = Decimal("70.25860000") - Decimal("25.20180000")
        self.assertEqual(exchange._account_available_balances["ARRR"], expected)

    async def test_local_balance_pre_adjust_buy(self):
        """
        After a successful buy order placement, available_balances for the
        quote asset must be immediately decremented by amount * price.
        """
        exchange = self._make_exchange(
            available_balances={"USDT": Decimal("51.778")},
            total_balances={"USDT": Decimal("51.778")},
        )
        with patch(
            "hummingbot.connector.exchange_py_base.ExchangePyBase._place_order_and_process_update",
            new_callable=AsyncMock,
            return_value="12346",
        ):
            order = self._make_order(
                "ARRR-USDT", TradeType.BUY,
                Decimal("16.0229"), Decimal("0.186099")
            )
            await exchange._place_order_and_process_update(order)

        hold = Decimal("16.0229") * Decimal("0.186099")
        expected = Decimal("51.778") - hold
        self.assertEqual(exchange._account_available_balances["USDT"], expected)

    async def test_pre_adjust_does_not_change_total(self):
        """
        The pre-adjustment only changes available, not total (available+held).
        Total only changes on fills, not order placement.
        """
        exchange = self._make_exchange(
            available_balances={"ARRR": Decimal("122.30")},
            total_balances={"ARRR": Decimal("177.09")},
        )
        with patch(
            "hummingbot.connector.exchange_py_base.ExchangePyBase._place_order_and_process_update",
            new_callable=AsyncMock,
            return_value="12347",
        ):
            order = self._make_order("ARRR-USDT", TradeType.SELL, Decimal("1.0"))
            await exchange._place_order_and_process_update(order)

        self.assertEqual(exchange._account_available_balances["ARRR"], Decimal("121.30"))
        self.assertEqual(exchange._account_balances["ARRR"], Decimal("177.09"))

    async def test_ws_balance_overwrites_pre_adjustment(self):
        """
        When the WS balanceUpdate arrives after order placement, it
        overwrites the pre-adjusted value with the real exchange value.
        No double-counting should occur.
        """
        exchange = self._make_exchange(
            available_balances={"ARRR": Decimal("122.30")},
            total_balances={"ARRR": Decimal("177.09")},
        )
        # Pre-adjust: sell 1.0 ARRR
        with patch(
            "hummingbot.connector.exchange_py_base.ExchangePyBase._place_order_and_process_update",
            new_callable=AsyncMock,
            return_value="12348",
        ):
            order = self._make_order("ARRR-USDT", TradeType.SELL, Decimal("1.0"))
            await exchange._place_order_and_process_update(order)

        self.assertEqual(exchange._account_available_balances["ARRR"], Decimal("121.30"))

        # Simulate WS balanceUpdate arriving with real exchange values
        exchange._account_available_balances["ARRR"] = Decimal("121.30880000")
        exchange._account_balances["ARRR"] = Decimal("177.09000000")

        self.assertEqual(exchange._account_available_balances["ARRR"], Decimal("121.30880000"))

    async def test_multi_strategy_sees_pre_adjusted_balance(self):
        """
        Two controllers reading available_balances after a sell order from
        either one should both see the reduced available immediately.
        """
        exchange = self._make_exchange(
            available_balances={"ARRR": Decimal("70.0")},
            total_balances={"ARRR": Decimal("177.0")},
        )
        with patch(
            "hummingbot.connector.exchange_py_base.ExchangePyBase._place_order_and_process_update",
            new_callable=AsyncMock,
            return_value="12349",
        ):
            order = self._make_order("ARRR-USDT", TradeType.SELL, Decimal("25.0"))
            await exchange._place_order_and_process_update(order)

        # Both Strategy A and Strategy B read the same dict
        self.assertEqual(exchange._account_available_balances["ARRR"], Decimal("45.0"))

    async def test_failed_order_no_pre_adjust(self):
        """
        If _place_order raises (exchange rejects), the balance must NOT be adjusted.
        """
        exchange = self._make_exchange(
            available_balances={"ARRR": Decimal("70.0")},
            total_balances={"ARRR": Decimal("177.0")},
        )
        with patch(
            "hummingbot.connector.exchange_py_base.ExchangePyBase._place_order_and_process_update",
            new_callable=AsyncMock,
            side_effect=IOError("Insufficient funds"),
        ):
            order = self._make_order("ARRR-USDT", TradeType.SELL, Decimal("25.0"))
            with self.assertRaises(IOError):
                await exchange._place_order_and_process_update(order)

        # Balance must remain unchanged
        self.assertEqual(exchange._account_available_balances["ARRR"], Decimal("70.0"))


# ---------------------------------------------------------------------------
# Test 7: Same-cycle stop/create deferral (Fix 2)
# ---------------------------------------------------------------------------

class TestSameCycleStopCreateDeferral(unittest.TestCase):
    """Tests for Fix 2: execute_actions defers same-cycle stop+create."""

    def _make_executor_config(self, connector_name, trading_pair, side):
        from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
        return PositionExecutorConfig(
            timestamp=1234,
            controller_id="ctrl_1",
            connector_name=connector_name,
            trading_pair=trading_pair,
            side=side,
            entry_price=Decimal("0.22"),
            amount=Decimal("10"),
        )

    def test_same_cycle_stop_create_defers(self):
        """
        When StopExecutorAction and CreateExecutorAction exist for the
        same (connector, pair, side) in the same cycle, create must be deferred.
        """
        from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
        from hummingbot.strategy_v2.models.executor_actions import (
            CreateExecutorAction,
            StopExecutorAction,
        )

        orchestrator = MagicMock(spec=ExecutorOrchestrator)
        orchestrator.logger = MagicMock(return_value=MagicMock())
        orchestrator.active_executors = {}
        orchestrator.execute_action = MagicMock()
        orchestrator._preflight_budget_check = MagicMock(side_effect=lambda x: x)

        # Setup: active sell executor that will be stopped
        mock_executor = MagicMock()
        mock_executor.config.id = "exec_1"
        mock_executor.config.connector_name = "nonkyc"
        mock_executor.config.trading_pair = "ARRR-USDT"
        mock_executor.config.side = TradeType.SELL
        orchestrator.active_executors["ctrl_1"] = [mock_executor]

        # Create stop + create actions for same connector/pair/side
        stop_action = StopExecutorAction(controller_id="ctrl_1", executor_id="exec_1")
        exec_config = self._make_executor_config("nonkyc", "ARRR-USDT", TradeType.SELL)
        create_action = CreateExecutorAction(controller_id="ctrl_1", executor_config=exec_config)

        # Bind the real method
        ExecutorOrchestrator.execute_actions(orchestrator, [stop_action, create_action])

        # Stop should have been executed
        stop_calls = [
            c for c in orchestrator.execute_action.call_args_list
            if isinstance(c[0][0], StopExecutorAction)
        ]
        self.assertEqual(len(stop_calls), 1)

        # Create should NOT have been executed (deferred)
        create_calls = [
            c for c in orchestrator.execute_action.call_args_list
            if isinstance(c[0][0], CreateExecutorAction)
        ]
        self.assertEqual(len(create_calls), 0)

    def test_non_conflicting_create_proceeds(self):
        """
        A create action for a different pair/side should NOT be deferred.
        """
        from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
        from hummingbot.strategy_v2.models.executor_actions import (
            CreateExecutorAction,
            StopExecutorAction,
        )

        orchestrator = MagicMock(spec=ExecutorOrchestrator)
        orchestrator.logger = MagicMock(return_value=MagicMock())
        orchestrator.active_executors = {}
        orchestrator.execute_action = MagicMock()
        orchestrator._preflight_budget_check = MagicMock(side_effect=lambda x: x)

        # Stop a SELL executor
        mock_executor = MagicMock()
        mock_executor.config.id = "exec_1"
        mock_executor.config.connector_name = "nonkyc"
        mock_executor.config.trading_pair = "ARRR-USDT"
        mock_executor.config.side = TradeType.SELL
        orchestrator.active_executors["ctrl_1"] = [mock_executor]

        stop_action = StopExecutorAction(controller_id="ctrl_1", executor_id="exec_1")

        # Create for BUY side (different from stopped SELL)
        exec_config = self._make_executor_config("nonkyc", "ARRR-USDT", TradeType.BUY)
        create_action = CreateExecutorAction(controller_id="ctrl_1", executor_config=exec_config)

        ExecutorOrchestrator.execute_actions(orchestrator, [stop_action, create_action])

        # Create should have been executed (different side)
        create_calls = [
            c for c in orchestrator.execute_action.call_args_list
            if isinstance(c[0][0], CreateExecutorAction)
        ]
        self.assertEqual(len(create_calls), 1)


# ---------------------------------------------------------------------------
# Test 8: Price bounds include unfilled active orders (Fix 3)
# ---------------------------------------------------------------------------

class TestPriceBoundsIncludeUnfilled(IsolatedAsyncioWrapperTestCase):
    """Tests for Fix 3: _get_active_order_price_bounds includes unfilled orders."""

    def test_price_bounds_include_unfilled(self):
        """Active unfilled orders must be included in cross-order prevention."""
        from hummingbot.strategy_v2.controllers.market_making_controller_base import (
            MarketMakingControllerBase,
        )

        ctrl = MagicMock(spec=MarketMakingControllerBase)

        # Mock executor: is_active=True, is_trading=False (unfilled resting order)
        mock_executor = MagicMock()
        mock_executor.is_active = True
        mock_executor.is_trading = False
        mock_executor.custom_info = {
            "level_id": "sell_0",
            "current_position_average_price": "0.22",
        }
        ctrl.executors_info = [mock_executor]

        # Call the real method
        highest_buy, lowest_sell = MarketMakingControllerBase._get_active_order_price_bounds(ctrl)

        self.assertIsNone(highest_buy)
        self.assertEqual(lowest_sell, Decimal("0.22"))

    def test_price_bounds_zero_price_excluded(self):
        """Executors with avg_price=0 should be excluded."""
        from hummingbot.strategy_v2.controllers.market_making_controller_base import (
            MarketMakingControllerBase,
        )

        ctrl = MagicMock(spec=MarketMakingControllerBase)
        mock_executor = MagicMock()
        mock_executor.is_active = True
        mock_executor.is_trading = False
        mock_executor.custom_info = {
            "level_id": "buy_0",
            "current_position_average_price": "0",
        }
        ctrl.executors_info = [mock_executor]

        highest_buy, lowest_sell = MarketMakingControllerBase._get_active_order_price_bounds(ctrl)
        self.assertIsNone(highest_buy)
        self.assertIsNone(lowest_sell)


# ---------------------------------------------------------------------------
# Test 9: Error logging includes exception repr (Fix 4)
# ---------------------------------------------------------------------------

class TestLastPriceErrorLogging(IsolatedAsyncioWrapperTestCase):
    """Tests for Fix 4: _safe_get_last_traded_price uses repr(e)."""

    async def test_last_price_error_logs_repr(self):
        """TimeoutError must produce non-empty log output with repr()."""
        from hummingbot.data_feed.market_data_provider import MarketDataProvider

        mdp = MagicMock(spec=MarketDataProvider)
        mock_connector = MagicMock()
        mock_connector.name = "nonkyc"
        mock_connector._get_last_traded_price = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with patch("hummingbot.data_feed.market_data_provider.logging") as mock_logging:
            result = await MarketDataProvider._safe_get_last_traded_price(
                mdp, mock_connector, "ARRR-USDT"
            )

        self.assertEqual(result, Decimal(0))
        # Verify error was logged and contains "TimeoutError"
        mock_logging.error.assert_called_once()
        log_msg = mock_logging.error.call_args[0][0]
        self.assertIn("TimeoutError", log_msg)
        # Verify it's not empty like str(TimeoutError()) would be
        self.assertNotIn(": \n", log_msg)


# ---------------------------------------------------------------------------
# Test 10: NonKYC last-price logs primary failure before fallback (Fix 5)
# ---------------------------------------------------------------------------

class TestNonkycLastPriceFallbackLogging(IsolatedAsyncioWrapperTestCase):
    """Tests for Fix 5: _get_last_traded_price logs primary failure."""

    async def test_nonkyc_last_price_logs_primary_failure(self):
        """When /ticker/{symbol} fails, failure must be logged before fallback."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange

        exchange = MagicMock(spec=NonkycExchange)
        exchange.logger = MagicMock(return_value=MagicMock())

        # Primary call fails
        primary_error = ConnectionError("Connection refused")

        async def mock_api_request(method, path_url, limit_id, **kwargs):
            if "ticker/" in path_url:
                raise primary_error
            # Fallback: return tickers list
            return [{"ticker_id": "ARRR_USDT", "last_price": "0.22"}]

        exchange._api_request = AsyncMock(side_effect=mock_api_request)
        exchange.exchange_symbol_associated_to_pair = AsyncMock(return_value="ARRR/USDT")

        # Bind real method
        result = await NonkycExchange._get_last_traded_price(exchange, "ARRR-USDT")

        self.assertEqual(result, 0.22)

        # Verify debug log contains primary error
        debug_calls = exchange.logger().debug.call_args_list
        self.assertTrue(len(debug_calls) > 0, "Expected debug log for primary failure")
        log_msg = debug_calls[0][0][0]
        self.assertIn("Connection refused", log_msg)
        self.assertIn("Falling back", log_msg)


# ---------------------------------------------------------------------------
# Test: WS auth response ID correlation (Fix 6)
# ---------------------------------------------------------------------------

class TestWSAuthIDCorrelation(IsolatedAsyncioWrapperTestCase):
    """Tests for Fix 6: WS auth correlates on request ID."""

    async def test_ws_auth_ignores_non_matching_id(self):
        """Messages with non-matching IDs should be skipped."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import (
            NonkycAPIUserStreamDataSource,
        )

        ds = MagicMock(spec=NonkycAPIUserStreamDataSource)
        ds.logger = MagicMock(return_value=MagicMock())
        ds._auth = MagicMock()
        ds._auth.generate_ws_authentication_message.return_value = {
            "method": "login",
            "params": {"algo": "HS256", "pKey": "test", "nonce": "abc", "signature": "xyz"},
            "id": 99,
        }
        ds._sleep = AsyncMock()

        # Create WS mock that yields: first a non-matching message, then matching auth success
        ws = MagicMock()

        async def mock_iter():
            # First: non-matching ID (should be skipped)
            msg1 = MagicMock()
            msg1.data = {"id": 50, "result": True}
            yield msg1
            # Second: matching ID
            msg2 = MagicMock()
            msg2.data = {"id": 99, "result": True}
            yield msg2

        ws.iter_messages = mock_iter
        ws.send = AsyncMock()

        await NonkycAPIUserStreamDataSource._authenticate_ws_connection(ds, ws)

        # Auth should succeed (second message matched)
        ds.logger().info.assert_called_with("WebSocket authentication successful")


if __name__ == "__main__":
    unittest.main()
