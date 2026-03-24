"""Tests for Fix 4: Controller cross-order prevention."""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def _make_executor_info(
    executor_id: str,
    level_id: str,
    is_active: bool = True,
    is_trading: bool = True,
    avg_price: Decimal = Decimal("100"),
    side: TradeType = TradeType.BUY,
    close_type=None,
    close_timestamp=None,
):
    """Create a mock ExecutorInfo with the given parameters."""
    config = PositionExecutorConfig(
        id=executor_id,
        timestamp=1234567890.0,
        connector_name="binance_perpetual",
        trading_pair="ETH-USDT",
        side=side,
        entry_price=avg_price,
        amount=Decimal("1"),
    )

    return ExecutorInfo(
        id=executor_id,
        timestamp=1234567890.0,
        type="position_executor",
        status=RunnableStatus.RUNNING if is_active else RunnableStatus.TERMINATED,
        config=config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=Decimal("10") if is_trading else Decimal("0"),
        is_active=is_active,
        is_trading=is_trading,
        custom_info={
            "level_id": level_id,
            "current_position_average_price": avg_price,
            "side": side,
        },
        close_type=close_type,
        close_timestamp=close_timestamp,
    )


class TestCrossOrderPrevention(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        self.mock_controller_config = MarketMakingControllerConfigBase(
            id="test-cross",
            controller_name="mm_test",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal(100.0),
            buy_spreads=[0.01, 0.02],
            sell_spreads=[0.01, 0.02],
            buy_amounts_pct=[Decimal(50), Decimal(50)],
            sell_amounts_pct=[Decimal(50), Decimal(50)],
            executor_refresh_time=300,
            cooldown_time=15,
            leverage=20,
            position_mode=PositionMode.HEDGE,
        )
        self.mock_market_data_provider = MagicMock(spec=MarketDataProvider)
        self.mock_market_data_provider.time.return_value = 1234567890.0
        self.mock_actions_queue = AsyncMock(spec=asyncio.Queue)
        self.controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue,
        )
        # Set up processed data with a reference price
        self.controller.processed_data = {
            "reference_price": Decimal("100"),
            "spread_multiplier": Decimal("1"),
        }

    def _setup_executor_config_mock(self):
        """Patch get_executor_config to return a simple config."""
        patcher = patch.object(
            MarketMakingControllerBase,
            "get_executor_config",
            side_effect=lambda level_id, price, amount: PositionExecutorConfig(
                timestamp=1234,
                controller_id=self.controller.config.id,
                connector_name="binance_perpetual",
                trading_pair="ETH-USDT",
                side=TradeType.BUY if level_id.startswith("buy") else TradeType.SELL,
                entry_price=price,
                amount=amount,
            ),
        )
        return patcher

    def test_sell_not_created_below_active_buy(self):
        """A sell order priced at or below an active buy position should be suppressed."""
        # Active buy executor at price 99 (buy_0 spread=1%)
        active_buy = _make_executor_info(
            "exec-buy-0", "buy_0", is_active=True, is_trading=True,
            avg_price=Decimal("99"), side=TradeType.BUY,
        )
        self.controller.executors_info = [active_buy]

        with self._setup_executor_config_mock():
            # get_levels_to_execute will see buy_0 is active, so it won't recreate it.
            # But sell levels should be created. sell_0 has spread=1%, so price=101.
            # sell_1 has spread=2%, price=102. Both above buy at 99, so they should pass.
            # Let's test with a buy at a very high price to block sells.
            active_buy_high = _make_executor_info(
                "exec-buy-high", "buy_0", is_active=True, is_trading=True,
                avg_price=Decimal("103"), side=TradeType.BUY,
            )
            self.controller.executors_info = [active_buy_high]

            actions = self.controller.create_actions_proposal()

        # sell_0 price = 100 * (1 + 0.01) = 101, which is <= 103 (highest buy) -> suppressed
        # sell_1 price = 100 * (1 + 0.02) ~= 102, which is <= 103 (highest buy) -> suppressed
        # buy_1 should still be created (buy_0 is active, buy_1 is not)
        sell_actions = [a for a in actions if hasattr(a, 'executor_config')
                        and a.executor_config.side == TradeType.SELL]
        self.assertEqual(len(sell_actions), 0, "Sell orders below active buy should be suppressed")

    def test_buy_not_created_above_active_sell(self):
        """A buy order priced at or above an active sell position should be suppressed."""
        # Active sell at very low price
        active_sell = _make_executor_info(
            "exec-sell-low", "sell_0", is_active=True, is_trading=True,
            avg_price=Decimal("97"), side=TradeType.SELL,
        )
        self.controller.executors_info = [active_sell]

        with self._setup_executor_config_mock():
            actions = self.controller.create_actions_proposal()

        # buy_0 price = 100 * (1 - 0.01) = 99, which >= 97 (lowest sell) -> suppressed
        # buy_1 price = 100 * (1 - 0.02) = 98, which >= 97 (lowest sell) -> suppressed
        buy_actions = [a for a in actions if hasattr(a, 'executor_config')
                       and a.executor_config.side == TradeType.BUY]
        self.assertEqual(len(buy_actions), 0, "Buy orders above active sell should be suppressed")

    def test_sell_created_when_above_active_buy(self):
        """A sell order well above the active buy should be created normally."""
        # Active buy at a low price
        active_buy = _make_executor_info(
            "exec-buy-0", "buy_0", is_active=True, is_trading=True,
            avg_price=Decimal("95"), side=TradeType.BUY,
        )
        self.controller.executors_info = [active_buy]

        with self._setup_executor_config_mock():
            actions = self.controller.create_actions_proposal()

        # sell_0 price = 101, sell_1 = 102. Both > 95 -> created
        sell_actions = [a for a in actions if hasattr(a, 'executor_config')
                        and a.executor_config.side == TradeType.SELL]
        self.assertEqual(len(sell_actions), 2, "Sell orders above active buy should be created")

    def test_no_suppression_when_no_active_trading_executors(self):
        """When no executors are active and trading, all levels should be created."""
        self.controller.executors_info = []

        with self._setup_executor_config_mock():
            actions = self.controller.create_actions_proposal()

        # All 4 levels (buy_0, buy_1, sell_0, sell_1) should be created
        self.assertEqual(len(actions), 4)


class TestUnderSeededWarning(IsolatedAsyncioWrapperTestCase):
    """Tests for Fix 6: under-seeded startup warning."""

    def setUp(self):
        super().setUp()
        self.config = MarketMakingControllerConfigBase(
            id="test",
            controller_name="test_controller",
            connector_name="nonkyc",  # spot, not perpetual
            trading_pair="ARRR-USDT",
            total_amount_quote=Decimal(100),
            buy_spreads=[0.01],
            sell_spreads=[0.01],
            buy_amounts_pct=[Decimal(100)],
            sell_amounts_pct=[Decimal(100)],
            executor_refresh_time=300,
            cooldown_time=15,
            skip_rebalance=True,
        )
        self.mock_mdp = MagicMock(spec=MarketDataProvider)
        self.controller = MarketMakingControllerBase(
            config=self.config,
            market_data_provider=self.mock_mdp,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        self.controller.processed_data = {"reference_price": Decimal("0.25")}

    def test_under_seeded_warning_emitted(self):
        """Warning when available base < 50% of sell-side requirement."""
        mock_connector = MagicMock()
        mock_connector.available_balances = {"ARRR": Decimal("10")}
        self.mock_mdp.connectors = {"nonkyc": mock_connector}

        with self.assertLogs(self.controller.logger(), level="WARNING") as cm:
            self.controller.check_position_rebalance()
        self.assertTrue(any("SHORTFALL" in msg for msg in cm.output))

    def test_under_seeded_warning_only_once(self):
        """Warning should only be emitted once."""
        mock_connector = MagicMock()
        mock_connector.available_balances = {"ARRR": Decimal("10")}
        self.mock_mdp.connectors = {"nonkyc": mock_connector}

        with self.assertLogs(self.controller.logger(), level="WARNING"):
            self.controller.check_position_rebalance()

        # Second call: should NOT warn again
        with patch.object(self.controller.logger(), 'warning') as mock_warn:
            self.controller.check_position_rebalance()
            for call in mock_warn.call_args_list:
                self.assertNotIn("SHORTFALL", str(call))

    def test_no_warning_for_perpetual(self):
        """Perpetual connector should never get under-seeded warning."""
        self.config.connector_name = "binance_perpetual"
        controller = MarketMakingControllerBase(
            config=self.config,
            market_data_provider=self.mock_mdp,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        controller.processed_data = {"reference_price": Decimal("0.25")}
        result = controller.check_position_rebalance()
        self.assertIsNone(result)
