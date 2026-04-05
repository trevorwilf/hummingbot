import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.data_types import PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TrailingStop
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class TestMarketMakingControllerBase(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        # Mocking the MarketMakingControllerConfigBase
        self.mock_controller_config = MarketMakingControllerConfigBase(
            id="test",
            controller_name="market_making_test_controller",
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

        # Mocking dependencies
        self.mock_market_data_provider = MagicMock(spec=MarketDataProvider)
        self.mock_actions_queue = AsyncMock(spec=asyncio.Queue)

        # Instantiating the MarketMakingControllerBase
        self.controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )

    async def test_update_processed_data(self):
        type(self.mock_market_data_provider).get_price_by_type = MagicMock(return_value=Decimal("100"))
        await self.controller.update_processed_data()
        self.assertEqual(self.controller.processed_data["reference_price"], Decimal("100"))
        self.assertEqual(self.controller.processed_data["spread_multiplier"], Decimal("1"))

    @patch("hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerBase.get_executor_config", new_callable=MagicMock)
    async def test_determine_executor_actions(self, executor_config_mock: MagicMock):
        executor_config_mock.return_value = PositionExecutorConfig(
            timestamp=1234, controller_id=self.controller.config.id, connector_name="binance_perpetual",
            trading_pair="ETH-USDT", side=TradeType.BUY, entry_price=Decimal(100), amount=Decimal(10))
        type(self.mock_market_data_provider).get_price_by_type = MagicMock(return_value=Decimal("100"))
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertIsInstance(actions, list)
        for action in actions:
            self.assertIsInstance(action, ExecutorAction)

    def test_stop_actions_proposal(self):
        stop_actions = self.controller.stop_actions_proposal()
        self.assertIsInstance(stop_actions, list)
        for action in stop_actions:
            self.assertIsInstance(action, StopExecutorAction)

    def test_validate_order_type(self):
        for order_type_name in OrderType.__members__:
            self.assertEqual(
                MarketMakingControllerConfigBase.validate_order_type(order_type_name),
                OrderType[order_type_name]
            )

        with self.assertRaises(ValueError):
            MarketMakingControllerConfigBase.validate_order_type("invalid_order_type")

    def test_triple_barrier_config(self):
        triple_barrier_config = self.mock_controller_config.triple_barrier_config
        self.assertEqual(triple_barrier_config.stop_loss, self.mock_controller_config.stop_loss)
        self.assertEqual(triple_barrier_config.take_profit, self.mock_controller_config.take_profit)
        self.assertEqual(triple_barrier_config.time_limit, self.mock_controller_config.time_limit)
        self.assertEqual(triple_barrier_config.trailing_stop, self.mock_controller_config.trailing_stop)

    def test_validate_position_mode(self):
        for position_mode_name in PositionMode.__members__:
            self.assertEqual(
                MarketMakingControllerConfigBase.validate_position_mode(position_mode_name),
                PositionMode[position_mode_name]
            )

        with self.assertRaises(ValueError):
            MarketMakingControllerConfigBase.validate_position_mode("invalid_position_mode")

    def test_update_markets_new_connector(self):
        markets = MarketDict()
        updated_markets = self.mock_controller_config.update_markets(markets)

        self.assertIn("binance_perpetual", updated_markets)
        self.assertIn("ETH-USDT", updated_markets["binance_perpetual"])

    def test_update_markets_existing_connector(self):
        markets = MarketDict({"binance_perpetual": {"BTC-USDT"}})
        updated_markets = self.mock_controller_config.update_markets(markets)

        self.assertIn("binance_perpetual", updated_markets)
        self.assertIn("ETH-USDT", updated_markets["binance_perpetual"])
        self.assertIn("BTC-USDT", updated_markets["binance_perpetual"])

    def test_validate_target(self):
        self.assertEqual(None, self.mock_controller_config.validate_target(""))
        self.assertEqual(Decimal("2.0"), self.mock_controller_config.validate_target("2.0"))

    def test_parse_trailing_stop(self):
        self.assertEqual(None, self.mock_controller_config.parse_trailing_stop(""))
        trailing_stop = TrailingStop(activation_price=Decimal("2"), trailing_delta=Decimal(0.5))
        self.assertEqual(trailing_stop, self.mock_controller_config.parse_trailing_stop(trailing_stop))

    def test_get_required_base_amount(self):
        # Test that get_required_base_amount calculates correctly
        controller_config = MarketMakingControllerConfigBase(
            id="test",
            controller_name="market_making_test_controller",
            connector_name="binance",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("1000"),
            buy_spreads=[0.01, 0.02],
            sell_spreads=[0.01, 0.02],
            buy_amounts_pct=[Decimal(50), Decimal(50)],
            sell_amounts_pct=[Decimal(60), Decimal(40)],
            executor_refresh_time=300,
            cooldown_time=15,
            leverage=1,
            position_mode=PositionMode.HEDGE,
        )

        reference_price = Decimal("100")
        required_base_amount = controller_config.get_required_base_amount(reference_price)

        self.assertEqual(required_base_amount, Decimal("5"))

    def test_check_position_rebalance_perpetual(self):
        # Test that perpetual markets skip position rebalancing
        self.mock_controller_config.connector_name = "binance_perpetual"
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100")}

        result = controller.check_position_rebalance()
        self.assertIsNone(result)

    def test_check_position_rebalance_no_reference_price(self):
        # Test early return when reference price is not available
        self.mock_controller_config.connector_name = "binance"  # Spot market
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {}  # No reference price

        result = controller.check_position_rebalance()
        self.assertIsNone(result)

    def test_check_position_rebalance_active_rebalance_exists(self):
        # Test that no new rebalance is created when one is already active
        self.mock_controller_config.connector_name = "binance"  # Spot market
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100")}

        # Create a mock active rebalance executor
        mock_executor = MagicMock(spec=ExecutorInfo)
        mock_executor.is_active = True
        mock_executor.id = "rebalance_executor_0"
        mock_executor.custom_info = {"level_id": "position_rebalance"}
        controller.executors_info = [mock_executor]

        result = controller.check_position_rebalance()
        self.assertIsNone(result)

    def test_check_position_rebalance_below_threshold(self):
        # Test that no rebalance happens when difference is below threshold
        self.mock_controller_config.connector_name = "binance"  # Spot market
        self.mock_controller_config.position_rebalance_threshold_pct = Decimal("0.05")  # 5% threshold
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100")}
        controller.executors_info = []  # No active executors

        # Mock positions_held to have almost enough base asset
        mock_position = MagicMock(spec=PositionSummary)
        mock_position.connector_name = "binance"
        mock_position.trading_pair = "ETH-USDT"
        mock_position.side = TradeType.BUY
        mock_position.amount = Decimal("0.99")  # Just slightly below 1.0 required
        controller.positions_held = [mock_position]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("1.0")):
            with patch.object(self.mock_market_data_provider, 'time', return_value=1234567890):
                result = controller.check_position_rebalance()

        # 0.99 vs 1.0 = 0.01 difference, which is 1% (below 5% threshold)
        self.assertIsNone(result)

    def test_check_position_rebalance_buy_needed(self):
        # Test that buy order is created when base asset is insufficient
        self.mock_controller_config.connector_name = "binance"  # Spot market
        self.mock_controller_config.position_rebalance_threshold_pct = Decimal("0.05")  # 5% threshold
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100")}
        controller.executors_info = []  # No active executors
        controller.positions_held = []  # No positions held

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            with patch.object(self.mock_market_data_provider, 'time', return_value=1234567890):
                result = controller.check_position_rebalance()

        # Should create a buy order for 10.0 base asset
        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.controller_id, "test")
        self.assertIsInstance(result.executor_config, OrderExecutorConfig)
        self.assertEqual(result.executor_config.side, TradeType.BUY)
        self.assertEqual(result.executor_config.amount, Decimal("10.0"))
        self.assertEqual(result.executor_config.execution_strategy, ExecutionStrategy.MARKET)

    def test_check_position_rebalance_sell_needed(self):
        # Test that sell order is created when base asset is excessive
        self.mock_controller_config.connector_name = "binance"  # Spot market
        self.mock_controller_config.position_rebalance_threshold_pct = Decimal("0.05")  # 5% threshold
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100")}
        controller.executors_info = []  # No active executors

        # Mock positions_held to have too much base asset
        mock_position = MagicMock(spec=PositionSummary)
        mock_position.connector_name = "binance"
        mock_position.trading_pair = "ETH-USDT"
        mock_position.side = TradeType.BUY
        mock_position.amount = Decimal("15.0")  # More than required
        controller.positions_held = [mock_position]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            with patch.object(self.mock_market_data_provider, 'time', return_value=1234567890):
                result = controller.check_position_rebalance()

        # Should create a sell order for 5.0 base asset (15.0 - 10.0)
        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.controller_id, "test")
        self.assertIsInstance(result.executor_config, OrderExecutorConfig)
        self.assertEqual(result.executor_config.side, TradeType.SELL)
        self.assertEqual(result.executor_config.amount, Decimal("5.0"))
        self.assertEqual(result.executor_config.execution_strategy, ExecutionStrategy.MARKET)

    def test_get_current_base_position_buy_side(self):
        # Test calculation of current base position for buy side
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )

        # Mock buy position
        mock_position = MagicMock(spec=PositionSummary)
        mock_position.connector_name = "binance_perpetual"
        mock_position.trading_pair = "ETH-USDT"
        mock_position.side = TradeType.BUY
        mock_position.amount = Decimal("5.0")
        controller.positions_held = [mock_position]

        result = controller.get_current_base_position()
        self.assertEqual(result, Decimal("5.0"))

    def test_get_current_base_position_sell_side(self):
        # Test calculation of current base position for sell side
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )

        # Mock sell position
        mock_position = MagicMock(spec=PositionSummary)
        mock_position.connector_name = "binance_perpetual"
        mock_position.trading_pair = "ETH-USDT"
        mock_position.side = TradeType.SELL
        mock_position.amount = Decimal("3.0")
        controller.positions_held = [mock_position]

        result = controller.get_current_base_position()
        self.assertEqual(result, Decimal("-3.0"))

    def test_get_current_base_position_mixed(self):
        # Test calculation with both buy and sell positions
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )

        # Mock multiple positions
        mock_buy_position = MagicMock(spec=PositionSummary)
        mock_buy_position.connector_name = "binance_perpetual"
        mock_buy_position.trading_pair = "ETH-USDT"
        mock_buy_position.side = TradeType.BUY
        mock_buy_position.amount = Decimal("10.0")

        mock_sell_position = MagicMock(spec=PositionSummary)
        mock_sell_position.connector_name = "binance_perpetual"
        mock_sell_position.trading_pair = "ETH-USDT"
        mock_sell_position.side = TradeType.SELL
        mock_sell_position.amount = Decimal("3.0")

        # Include a position for different trading pair that should be ignored
        mock_other_position = MagicMock(spec=PositionSummary)
        mock_other_position.connector_name = "binance_perpetual"
        mock_other_position.trading_pair = "BTC-USDT"
        mock_other_position.side = TradeType.BUY
        mock_other_position.amount = Decimal("1.0")

        controller.positions_held = [mock_buy_position, mock_sell_position, mock_other_position]

        result = controller.get_current_base_position()
        self.assertEqual(result, Decimal("7.0"))  # 10.0 - 3.0

    def test_get_current_base_position_no_positions(self):
        # Test with no positions
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.positions_held = []

        result = controller.get_current_base_position()
        self.assertEqual(result, Decimal("0"))

    def test_create_position_rebalance_order(self):
        # Test creation of position rebalance order
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("150")}

        with patch.object(self.mock_market_data_provider, 'time', return_value=1234567890):
            result = controller.create_position_rebalance_order(TradeType.BUY, Decimal("2.5"))

        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.controller_id, "test")
        self.assertIsInstance(result.executor_config, OrderExecutorConfig)
        self.assertEqual(result.executor_config.timestamp, 1234567890)
        self.assertEqual(result.executor_config.connector_name, "binance_perpetual")
        self.assertEqual(result.executor_config.trading_pair, "ETH-USDT")
        self.assertEqual(result.executor_config.execution_strategy, ExecutionStrategy.MARKET)
        self.assertEqual(result.executor_config.side, TradeType.BUY)
        self.assertEqual(result.executor_config.amount, Decimal("2.5"))
        self.assertEqual(result.executor_config.price, Decimal("150"))  # Will be ignored for market orders
        self.assertEqual(result.executor_config.level_id, "position_rebalance")

    def test_create_actions_proposal_with_position_rebalance(self):
        # Test that position rebalance action is returned exclusively (no PMM levels)
        self.mock_controller_config.connector_name = "binance"  # Spot market
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100"), "spread_multiplier": Decimal("1")}
        controller.executors_info = []  # No active executors
        controller.positions_held = []  # No positions

        # Mock the methods
        mock_rebalance_action = CreateExecutorAction(
            controller_id="test",
            executor_config=OrderExecutorConfig(
                timestamp=1234,
                connector_name="binance",
                trading_pair="ETH-USDT",
                execution_strategy=ExecutionStrategy.MARKET,
                side=TradeType.BUY,
                amount=Decimal("1.0"),
                price=Decimal("100"),
                level_id="position_rebalance",
                controller_id="test"
            )
        )

        mock_executor_config = MagicMock()

        with patch.object(controller, 'check_position_rebalance', return_value=mock_rebalance_action):
            with patch.object(controller, 'get_levels_to_execute', return_value=["buy_0", "sell_0"]) as mock_levels:
                with patch.object(controller, 'get_price_and_amount', return_value=(Decimal("100"), Decimal("1"))):
                    with patch.object(controller, 'get_executor_config', return_value=mock_executor_config) as mock_get_config:
                        actions = controller.create_actions_proposal()

        # Should include ONLY the rebalance action, zero PMM levels
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0], mock_rebalance_action)
        # get_executor_config should NOT have been called (early return before PMM path)
        mock_get_config.assert_not_called()

    def test_create_actions_proposal_no_position_rebalance(self):
        # Test normal case where no position rebalance is needed
        self.mock_controller_config.connector_name = "binance"  # Spot market
        controller = MarketMakingControllerBase(
            config=self.mock_controller_config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue
        )
        controller.processed_data = {"reference_price": Decimal("100"), "spread_multiplier": Decimal("1")}
        controller.executors_info = []  # No active executors
        controller.positions_held = []  # No positions

        with patch.object(controller, 'check_position_rebalance', return_value=None):
            with patch.object(controller, 'get_levels_to_execute', return_value=[]):
                actions = controller.create_actions_proposal()

        # Should not include any rebalance actions
        self.assertEqual(len(actions), 0)

    def _make_spot_controller(self, **config_overrides):
        """Helper: create a spot controller with sensible defaults for rebalance tests."""
        config_kwargs = dict(
            id="test",
            controller_name="market_making_test_controller",
            connector_name="binance",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("1000"),
            buy_spreads=[0.01],
            sell_spreads=[0.01],
            buy_amounts_pct=[Decimal(50)],
            sell_amounts_pct=[Decimal(50)],
            executor_refresh_time=300,
            cooldown_time=15,
            leverage=1,
            position_mode=PositionMode.HEDGE,
            skip_rebalance=False,
        )
        config_kwargs.update(config_overrides)
        config = MarketMakingControllerConfigBase(**config_kwargs)
        # Provide a connector mock with ample balances for sell-side clipping
        mock_connector = MagicMock()
        mock_connector.available_balances = {
            config.trading_pair.split("-")[0]: Decimal("10000"),
            config.trading_pair.split("-")[1]: Decimal("10000"),
        }
        self.mock_market_data_provider.connectors = {config.connector_name: mock_connector}
        controller = MarketMakingControllerBase(
            config=config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue,
        )
        controller.processed_data = {"reference_price": Decimal("100"), "spread_multiplier": Decimal("1")}
        controller.executors_info = []
        controller.positions_held = []
        return controller

    def _make_rebalance_action(self):
        """Helper: create a standard rebalance CreateExecutorAction."""
        return CreateExecutorAction(
            controller_id="test",
            executor_config=OrderExecutorConfig(
                timestamp=1234, connector_name="binance", trading_pair="ETH-USDT",
                execution_strategy=ExecutionStrategy.MARKET, side=TradeType.BUY,
                amount=Decimal("5.0"), price=Decimal("100"), level_id="position_rebalance",
                controller_id="test",
            )
        )

    def _make_pmm_executor_config(self):
        """Helper: create a valid PositionExecutorConfig for PMM level tests."""
        return PositionExecutorConfig(
            timestamp=1234, controller_id="test", connector_name="binance",
            trading_pair="ETH-USDT", side=TradeType.BUY,
            entry_price=Decimal("100"), amount=Decimal("1"),
        )

    def test_rebalance_exclusive_no_pmm_levels(self):
        """When rebalance is needed, only the rebalance action is returned — no PMM levels."""
        controller = self._make_spot_controller()
        mock_rebalance_action = self._make_rebalance_action()

        with patch.object(controller, 'check_position_rebalance', return_value=mock_rebalance_action):
            with patch.object(controller, 'get_levels_to_execute', return_value=["buy_0", "sell_0"]):
                with patch.object(controller, 'get_executor_config', return_value=self._make_pmm_executor_config()) as mock_get_config:
                    actions = controller.create_actions_proposal()

        self.assertEqual(len(actions), 1)
        self.assertIs(actions[0], mock_rebalance_action)
        mock_get_config.assert_not_called()

    def test_no_rebalance_returns_pmm_levels(self):
        """When no rebalance is needed, PMM level actions are returned."""
        controller = self._make_spot_controller()
        pmm_config = self._make_pmm_executor_config()

        with patch.object(controller, 'check_position_rebalance', return_value=None):
            with patch.object(controller, 'get_levels_to_execute', return_value=["buy_0", "sell_0"]):
                with patch.object(controller, 'get_price_and_amount', return_value=(Decimal("100"), Decimal("1"))):
                    with patch.object(controller, 'get_executor_config', return_value=pmm_config):
                        actions = controller.create_actions_proposal()

        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertIsInstance(action, CreateExecutorAction)
            self.assertIs(action.executor_config, pmm_config)

    def test_rebalance_cooldown_suppresses_retry(self):
        """Rebalance is suppressed when last attempt was within cooldown window."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = current_time - 30  # 30s ago
        controller.positions_held = []  # No base held → would normally trigger rebalance
        self.mock_market_data_provider.time.return_value = current_time

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNone(result)

    def test_rebalance_cooldown_allows_retry_after_expiry(self):
        """Rebalance is allowed after the cooldown has expired."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = current_time - 61  # 61s ago — expired
        controller.positions_held = []  # No base held → triggers rebalance
        self.mock_market_data_provider.time.return_value = current_time

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNotNone(result)
        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.executor_config.side, TradeType.BUY)

    def test_rebalance_records_timestamp(self):
        """When rebalance IS produced, the attempt timestamp is updated."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []
        self.mock_market_data_provider.time.return_value = current_time

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNotNone(result)
        self.assertEqual(controller._last_rebalance_attempt_timestamp, current_time)

    def test_inflight_buy_suppresses_rebalance(self):
        """If in-flight buy orders fully cover the base shortage, no rebalance is emitted."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []  # No base held
        self.mock_market_data_provider.time.return_value = current_time

        # Add an active buy executor that covers the full shortage
        mock_buy_executor = MagicMock(spec=ExecutorInfo)
        mock_buy_executor.is_active = True
        mock_buy_executor.id = "mock_buy_0"
        mock_buy_executor.type = "position_executor"
        mock_buy_executor.config = MagicMock()
        mock_buy_executor.config.connector_name = "binance"
        mock_buy_executor.config.trading_pair = "ETH-USDT"
        mock_buy_executor.config.side = TradeType.BUY
        mock_buy_executor.config.amount = Decimal("10.0")
        mock_buy_executor.custom_info = {"level_id": "buy_0"}
        controller.executors_info = [mock_buy_executor]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNone(result)

    def test_inflight_buy_reduces_rebalance_amount(self):
        """If in-flight buy partially covers shortage, rebalance is reduced."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []  # No base held
        self.mock_market_data_provider.time.return_value = current_time

        # Active buy executor covers half the shortage
        mock_buy_executor = MagicMock(spec=ExecutorInfo)
        mock_buy_executor.is_active = True
        mock_buy_executor.id = "mock_buy_0"
        mock_buy_executor.type = "position_executor"
        mock_buy_executor.config = MagicMock()
        mock_buy_executor.config.connector_name = "binance"
        mock_buy_executor.config.trading_pair = "ETH-USDT"
        mock_buy_executor.config.side = TradeType.BUY
        mock_buy_executor.config.amount = Decimal("5.0")
        mock_buy_executor.custom_info = {"level_id": "buy_0"}
        controller.executors_info = [mock_buy_executor]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNotNone(result)
        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.executor_config.side, TradeType.BUY)
        self.assertEqual(result.executor_config.amount, Decimal("5.0"))

    def test_inflight_buy_wrong_pair_ignored(self):
        """In-flight buy for a different pair does NOT reduce rebalance demand."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []
        self.mock_market_data_provider.time.return_value = current_time

        # Active buy executor for WRONG pair
        mock_buy_executor = MagicMock(spec=ExecutorInfo)
        mock_buy_executor.is_active = True
        mock_buy_executor.config = MagicMock()
        mock_buy_executor.config.connector_name = "binance"
        mock_buy_executor.config.trading_pair = "BTC-USDT"  # Different pair
        mock_buy_executor.config.side = TradeType.BUY
        mock_buy_executor.config.amount = Decimal("10.0")
        mock_buy_executor.custom_info = {"level_id": "buy_0"}
        controller.executors_info = [mock_buy_executor]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        # Should still produce rebalance for the full 10.0 (wrong pair executor is ignored)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.executor_config.amount, Decimal("10.0"))

    def test_rebalance_cooldown_field_defaults(self):
        """Verify rebalance_cooldown_time defaults to 60 seconds."""
        config = MarketMakingControllerConfigBase(
            id="test",
            controller_name="test_defaults",
            connector_name="binance",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("1000"),
            buy_spreads=[0.01],
            sell_spreads=[0.01],
        )
        self.assertEqual(config.rebalance_cooldown_time, 60)

    @patch("hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerBase.get_executor_config", new_callable=MagicMock)
    async def test_determine_executor_actions_stop_before_create(self, executor_config_mock: MagicMock):
        """All StopExecutorAction instances must appear before all CreateExecutorAction instances."""
        executor_config_mock.return_value = PositionExecutorConfig(
            timestamp=1234, controller_id=self.controller.config.id, connector_name="binance_perpetual",
            trading_pair="ETH-USDT", side=TradeType.BUY, entry_price=Decimal(100), amount=Decimal(10))
        type(self.mock_market_data_provider).get_price_by_type = MagicMock(return_value=Decimal("100"))
        self.mock_market_data_provider.time = MagicMock(return_value=999999)
        await self.controller.update_processed_data()

        # Create a mock executor that is past refresh time (will trigger stop)
        from hummingbot.strategy_v2.models.base import RunnableStatus
        mock_executor = MagicMock(spec=ExecutorInfo)
        mock_executor.is_active = True
        mock_executor.is_trading = False
        mock_executor.timestamp = 0  # Very old — will be refreshed
        mock_executor.close_type = None
        mock_executor.custom_info = {"level_id": "buy_0"}
        mock_executor.id = "old_executor"
        self.controller.executors_info = [mock_executor]

        actions = self.controller.determine_executor_actions()

        # Find indices of stop and create actions
        stop_indices = [i for i, a in enumerate(actions) if isinstance(a, StopExecutorAction)]
        create_indices = [i for i, a in enumerate(actions) if isinstance(a, CreateExecutorAction)]

        if stop_indices and create_indices:
            self.assertTrue(
                max(stop_indices) < min(create_indices),
                f"Stop actions must precede create actions. Got stops at {stop_indices}, creates at {create_indices}"
            )

    # ---- Real ExecutorInfo integration tests (not MagicMock) ----

    def _make_real_executor_info(
        self,
        executor_type: str,
        side: TradeType,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        level_id: str = "buy_0",
        is_active: bool = True,
    ) -> ExecutorInfo:
        """Create a real ExecutorInfo (not MagicMock) for integration-grade tests."""
        from hummingbot.strategy_v2.models.base import RunnableStatus

        if executor_type == "position_executor":
            config = PositionExecutorConfig(
                timestamp=1000000.0,
                controller_id="test",
                connector_name=connector_name,
                trading_pair=trading_pair,
                side=side,
                amount=amount,
                entry_price=Decimal("0.25"),
                level_id=level_id,
            )
            custom_info = {
                "level_id": level_id,
                "side": side,
                "current_position_average_price": Decimal("0.25"),
                "current_retries": 0,
                "max_retries": 10,
                "close_price": Decimal("0"),
                "open_order_last_update": None,
                "order_ids": [],
                "held_position_orders": [],
            }
        else:  # order_executor
            config = OrderExecutorConfig(
                timestamp=1000000.0,
                controller_id="test",
                connector_name=connector_name,
                trading_pair=trading_pair,
                side=side,
                amount=amount,
                price=Decimal("0.25"),
                execution_strategy=ExecutionStrategy.MARKET,
                level_id=level_id,
            )
            custom_info = {
                "level_id": level_id,
                "current_retries": 0,
                "max_retries": 10,
                "order_id": None,
                "order_last_update": None,
                "held_position_orders": [],
            }

        return ExecutorInfo(
            id=f"test-{level_id}-{side.name}",
            timestamp=1000000.0,
            type=executor_type,
            status=RunnableStatus.RUNNING if is_active else RunnableStatus.TERMINATED,
            config=config,
            net_pnl_pct=Decimal("0"),
            net_pnl_quote=Decimal("0"),
            cum_fees_quote=Decimal("0"),
            filled_amount_quote=Decimal("0"),
            is_active=is_active,
            is_trading=False,
            custom_info=custom_info,
        )

    def test_inflight_buy_real_position_executor_info(self):
        """Real PositionExecutorConfig is correctly detected as inflight buy."""
        controller = self._make_spot_controller()
        real_exec = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("10.0"), "buy_0")
        controller.executors_info = [real_exec]

        result = controller.get_inflight_buy_base_amount()
        self.assertEqual(result, Decimal("10.0"))

    def test_inflight_buy_real_order_executor_info(self):
        """Real OrderExecutorConfig is correctly detected as inflight buy."""
        controller = self._make_spot_controller()
        real_exec = self._make_real_executor_info(
            "order_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("7.5"), "position_rebalance")
        controller.executors_info = [real_exec]

        result = controller.get_inflight_buy_base_amount()
        self.assertEqual(result, Decimal("7.5"))

    def test_inflight_buy_mixed_executor_types(self):
        """Mixed executor types: only BUY-side executors are counted."""
        controller = self._make_spot_controller()
        pos_buy = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("5.0"), "buy_0")
        ord_buy = self._make_real_executor_info(
            "order_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("3.0"), "position_rebalance")
        pos_sell = self._make_real_executor_info(
            "position_executor", TradeType.SELL, "binance", "ETH-USDT", Decimal("4.0"), "sell_0")
        controller.executors_info = [pos_buy, ord_buy, pos_sell]

        result = controller.get_inflight_buy_base_amount()
        self.assertEqual(result, Decimal("8.0"))

    def test_inflight_buy_inactive_executor_excluded(self):
        """Inactive (TERMINATED) executor is excluded from inflight buy count."""
        controller = self._make_spot_controller()
        inactive = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("10.0"), "buy_0",
            is_active=False)
        controller.executors_info = [inactive]

        result = controller.get_inflight_buy_base_amount()
        self.assertEqual(result, Decimal("0"))

    def test_check_rebalance_suppressed_by_real_inflight_buy(self):
        """Full integration: real inflight buy covers shortage, no rebalance emitted."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []
        self.mock_market_data_provider.time.return_value = current_time

        # Real PositionExecutor BUY with amount >= required
        real_exec = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("10.0"), "buy_0")
        controller.executors_info = [real_exec]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNone(result)

    def test_check_rebalance_with_partial_inflight_real(self):
        """Partial inflight buy: rebalance emitted for the remaining shortage."""
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []
        self.mock_market_data_provider.time.return_value = current_time

        # Real inflight buy covers only 4.0 of 10.0 required
        real_exec = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("4.0"), "buy_0")
        controller.executors_info = [real_exec]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            result = controller.check_position_rebalance()

        self.assertIsNotNone(result)
        self.assertIsInstance(result, CreateExecutorAction)
        self.assertEqual(result.executor_config.side, TradeType.BUY)
        self.assertEqual(result.executor_config.amount, Decimal("6.0"))

    def test_get_effective_base_inventory_breakdown(self):
        """get_effective_base_inventory returns correct breakdown dict."""
        controller = self._make_spot_controller(
            position_rebalance_threshold_pct=Decimal("0.05"))
        controller.processed_data = {"reference_price": Decimal("100"), "spread_multiplier": Decimal("1")}

        # positions_held: 3.0 base
        mock_position = MagicMock()
        mock_position.connector_name = "binance"
        mock_position.trading_pair = "ETH-USDT"
        mock_position.side = TradeType.BUY
        mock_position.amount = Decimal("3.0")
        controller.positions_held = [mock_position]

        # active BUY executor: 2.0 base inflight
        real_exec = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("2.0"), "buy_0")
        controller.executors_info = [real_exec]

        with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
            inv = controller.get_effective_base_inventory()

        self.assertEqual(inv["required_base"], Decimal("10.0"))
        self.assertEqual(inv["held_base"], Decimal("3.0"))
        self.assertEqual(inv["inflight_buy_base"], Decimal("2.0"))
        self.assertEqual(inv["raw_shortage"], Decimal("7.0"))
        self.assertEqual(inv["adjusted_shortage"], Decimal("5.0"))
        self.assertEqual(inv["threshold"], Decimal("0.50"))
        self.assertTrue(inv["needs_rebalance"])  # 5.0 > 0.50

    def test_rebalance_debug_logging_emitted(self):
        """check_position_rebalance emits structured DEBUG log with inventory components."""
        import logging
        current_time = 1000000.0
        controller = self._make_spot_controller(rebalance_cooldown_time=60)
        controller._last_rebalance_attempt_timestamp = 0.0
        controller.positions_held = []
        controller.executors_info = []
        self.mock_market_data_provider.time.return_value = current_time

        # Use the actual class logger (may be StructLogger) to avoid cross-test pollution
        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with patch('hummingbot.strategy_v2.controllers.market_making_controller_base.MarketMakingControllerConfigBase.get_required_base_amount', return_value=Decimal("10.0")):
                with self.assertLogs(logger, level="DEBUG") as log:
                    controller.check_position_rebalance()

            # Find the rebalance check log line
            rebalance_logs = [m for m in log.output if "Rebalance check for" in m]
            self.assertTrue(len(rebalance_logs) > 0, "Expected 'Rebalance check for' in DEBUG logs")
            msg = rebalance_logs[0]
            self.assertIn("required_base=", msg)
            self.assertIn("held_base=", msg)
            self.assertIn("inflight_buy=", msg)
            self.assertIn("adjusted_diff=", msg)
        finally:
            logger.setLevel(old_level)

    def test_inflight_buy_logging_when_found(self):
        """get_inflight_buy_base_amount emits DEBUG log when executors are found."""
        import logging
        controller = self._make_spot_controller()
        real_exec = self._make_real_executor_info(
            "position_executor", TradeType.BUY, "binance", "ETH-USDT", Decimal("5.0"), "buy_0")
        controller.executors_info = [real_exec]

        # Use the actual class logger (may be StructLogger) to avoid cross-test pollution
        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with self.assertLogs(logger, level="DEBUG") as log:
                controller.get_inflight_buy_base_amount()

            inflight_logs = [m for m in log.output if "Inflight buy detection" in m]
            self.assertTrue(len(inflight_logs) > 0, "Expected 'Inflight buy detection' in DEBUG logs")
            msg = inflight_logs[0]
            self.assertIn(real_exec.id, msg)
            self.assertIn("5.0", msg)
        finally:
            logger.setLevel(old_level)

    # ---- FIX 2: Stale Market Data Detector tests ----

    def _make_controller_with_order_book(self, snapshot_uid=0, last_diff_uid=0, **config_overrides):
        """Helper: create a controller with a mock connector that has a mock order book."""
        config_kwargs = dict(
            id="test",
            controller_name="market_making_test_controller",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("1000"),
            buy_spreads=[0.01],
            sell_spreads=[0.01],
            buy_amounts_pct=[Decimal(50)],
            sell_amounts_pct=[Decimal(50)],
            executor_refresh_time=300,
            cooldown_time=15,
            leverage=20,
            position_mode=PositionMode.HEDGE,
        )
        config_kwargs.update(config_overrides)
        config = MarketMakingControllerConfigBase(**config_kwargs)

        mock_order_book = MagicMock()
        mock_order_book.snapshot_uid = snapshot_uid
        mock_order_book.last_diff_uid = last_diff_uid

        mock_connector = MagicMock()
        mock_connector.get_order_book.return_value = mock_order_book

        self.mock_market_data_provider.connectors = {config.connector_name: mock_connector}

        controller = MarketMakingControllerBase(
            config=config,
            market_data_provider=self.mock_market_data_provider,
            actions_queue=self.mock_actions_queue,
        )
        controller.processed_data = {"reference_price": Decimal("100"), "spread_multiplier": Decimal("1")}
        controller.executors_info = []
        controller.positions_held = []
        return controller, mock_order_book, mock_connector

    def test_no_stale_warning_before_first_market_data(self):
        """Before any market data arrives, freshness check should not warn and state stays 'unknown'."""
        import logging
        # Create controller with snapshot_uid=0 and last_diff_uid=0 (no real data yet)
        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=0, last_diff_uid=0)
        self.assertEqual(controller._stale_state, "unknown")

        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            # assertLogs requires at least one log; emit a dummy DEBUG to satisfy it
            with self.assertLogs(logger, level="DEBUG") as log:
                controller._check_market_data_freshness()
                logger.debug("dummy")

            warning_logs = [m for m in log.output if "WARNING" in m and "STALE" in m]
            self.assertEqual(len(warning_logs), 0, "No WARNING should be emitted before first market data")
            self.assertEqual(controller._stale_state, "unknown")
        finally:
            logger.setLevel(old_level)

    def test_snapshot_resets_stale_timer(self):
        """A non-zero snapshot_uid should transition state to 'healthy'."""
        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=42, last_diff_uid=0)
        self.assertEqual(controller._stale_state, "unknown")

        controller._check_market_data_freshness()

        self.assertEqual(controller._stale_state, "healthy")
        self.assertIsNotNone(controller._last_ob_event_time)
        self.assertEqual(controller._last_ob_snapshot_uid, 42)

    def test_diff_resets_stale_timer(self):
        """A non-zero last_diff_uid should transition state to 'healthy'."""
        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=0, last_diff_uid=99)
        self.assertEqual(controller._stale_state, "unknown")

        controller._check_market_data_freshness()

        self.assertEqual(controller._stale_state, "healthy")
        self.assertIsNotNone(controller._last_ob_event_time)
        self.assertEqual(controller._last_ob_diff_uid, 99)

    def test_stale_fires_after_threshold(self):
        """When data is older than threshold, state transitions to 'stale' with WARNING."""
        import logging
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=1, last_diff_uid=0)
        # Simulate: data was received, then went stale
        controller._last_ob_event_time = _time.time() - 35  # 35s ago, default threshold is 30s
        controller._last_ob_snapshot_uid = 1  # Already seen this UID
        controller._stale_state = "healthy"
        # OB still has the same UID (no change)
        mock_ob.snapshot_uid = 1
        mock_ob.last_diff_uid = 0

        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            with self.assertLogs(logger, level="WARNING") as log:
                controller._check_market_data_freshness()

            self.assertEqual(controller._stale_state, "stale")
            stale_warnings = [m for m in log.output if "STALE MARKET DATA" in m]
            self.assertTrue(len(stale_warnings) > 0, "Expected STALE MARKET DATA warning")
        finally:
            logger.setLevel(old_level)

    def test_stale_log_rate_limited(self):
        """Once in stale state, logging should be rate-limited to 60s intervals."""
        import logging
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=1, last_diff_uid=0)
        # Already in stale state, last log was just now
        controller._stale_state = "stale"
        controller._last_ob_event_time = _time.time() - 35
        controller._last_ob_snapshot_uid = 1
        controller._last_stale_log_time = _time.time()  # Just logged
        controller._stale_transition_time = _time.time() - 5  # Entered stale 5s ago
        mock_ob.snapshot_uid = 1
        mock_ob.last_diff_uid = 0

        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with self.assertLogs(logger, level="DEBUG") as log:
                controller._check_market_data_freshness()
                logger.debug("dummy")

            # Should NOT have any additional WARNING or INFO stale logs (rate limited)
            stale_logs = [m for m in log.output
                          if ("STALE MARKET DATA" in m) and ("dummy" not in m)]
            self.assertEqual(len(stale_logs), 0,
                             f"No stale log should be emitted within 60s rate limit, got: {stale_logs}")
        finally:
            logger.setLevel(old_level)

    def test_stale_recovery_logged(self):
        """Recovery from stale state should log an INFO message with duration."""
        import logging
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=1, last_diff_uid=0)
        # Simulate stale state
        controller._stale_state = "stale"
        controller._stale_transition_time = _time.time() - 10  # Stale for 10s
        controller._last_ob_snapshot_uid = 1
        controller._last_ob_event_time = _time.time() - 40

        # Now simulate fresh data arriving (new snapshot UID)
        mock_ob.snapshot_uid = 2

        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with self.assertLogs(logger, level="INFO") as log:
                controller._check_market_data_freshness()

            self.assertEqual(controller._stale_state, "healthy")
            recovery_logs = [m for m in log.output if "MARKET DATA RECOVERED" in m]
            self.assertTrue(len(recovery_logs) > 0, "Expected MARKET DATA RECOVERED info log")
            self.assertIn("stale duration=", recovery_logs[0])
        finally:
            logger.setLevel(old_level)

    def test_exception_logged_not_swallowed(self):
        """If get_order_book raises, a DEBUG log is emitted and no crash occurs."""
        import logging

        controller, _, mock_connector = self._make_controller_with_order_book(snapshot_uid=1, last_diff_uid=0)
        mock_connector.get_order_book.side_effect = RuntimeError("test error")

        logger = controller.logger()
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with self.assertLogs(logger, level="DEBUG") as log:
                controller._check_market_data_freshness()  # Should not raise

            error_logs = [m for m in log.output if "freshness check error" in m]
            self.assertTrue(len(error_logs) > 0, "Expected DEBUG log for freshness check error")
        finally:
            logger.setLevel(old_level)

    # ---- FIX 3: Stale Detection Safety Actions tests ----

    def _make_mock_executor(self, executor_id, is_active=True, is_trading=False, level_id="buy_0"):
        """Helper: create a mock executor for stale action tests."""
        mock_executor = MagicMock(spec=ExecutorInfo)
        mock_executor.id = executor_id
        mock_executor.is_active = is_active
        mock_executor.is_trading = is_trading
        mock_executor.custom_info = {"level_id": level_id}
        mock_executor.timestamp = 0
        mock_executor.close_type = None
        mock_executor.config = MagicMock()
        mock_executor.config.connector_name = "binance_perpetual"
        mock_executor.config.trading_pair = "ETH-USDT"
        return mock_executor

    def test_warn_only_does_not_stop_executors(self):
        """With stale_data_action='warn_only', executors_to_early_stop returns empty list."""
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(
            snapshot_uid=1, last_diff_uid=0, stale_data_action="warn_only")
        controller._stale_state = "stale"
        controller._last_ob_event_time = _time.time() - 50  # 50s stale

        # Add active executors
        controller.executors_info = [
            self._make_mock_executor("exec_1", is_active=True, is_trading=False),
            self._make_mock_executor("exec_2", is_active=True, is_trading=True),
        ]

        result = controller.executors_to_early_stop()
        self.assertEqual(result, [])

    def test_pause_new_orders_suppresses_creates(self):
        """With stale + 'pause_new_orders', create_actions_proposal returns stop actions only."""
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(
            snapshot_uid=1, last_diff_uid=0, stale_data_action="pause_new_orders")
        controller._stale_state = "stale"
        controller._last_ob_event_time = _time.time() - 50
        controller._last_ob_snapshot_uid = 1
        mock_ob.snapshot_uid = 1
        mock_ob.last_diff_uid = 0

        controller.executors_info = [
            self._make_mock_executor("exec_1", is_active=True, is_trading=False, level_id="buy_0"),
        ]

        # market_data_provider.time() is used by executors_to_refresh inside stop_actions_proposal
        self.mock_market_data_provider.time.return_value = _time.time()

        actions = controller.create_actions_proposal()
        # Should contain only StopExecutorAction (no CreateExecutorAction)
        for action in actions:
            self.assertIsInstance(action, StopExecutorAction)
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(len(create_actions), 0, "No create actions should be emitted when paused")

    def test_cancel_passive_stops_resting(self):
        """With stale + 'cancel_passive_orders', passive (not trading) executors are stopped."""
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(
            snapshot_uid=1, last_diff_uid=0, stale_data_action="cancel_passive_orders")
        controller._stale_state = "stale"
        controller._last_ob_event_time = _time.time() - 50  # Past soft threshold

        passive_exec = self._make_mock_executor("passive_1", is_active=True, is_trading=False)
        trading_exec = self._make_mock_executor("trading_1", is_active=True, is_trading=True)
        controller.executors_info = [passive_exec, trading_exec]

        result = controller.executors_to_early_stop()

        # Should stop passive executor but not trading one
        stopped_ids = [a.executor_id for a in result]
        self.assertIn("passive_1", stopped_ids)
        self.assertNotIn("trading_1", stopped_ids)

    def test_hard_threshold_stops_all(self):
        """When stale > hard threshold (90s), ALL active executors are stopped."""
        import time as _time

        controller, mock_ob, _ = self._make_controller_with_order_book(
            snapshot_uid=1, last_diff_uid=0, stale_data_action="warn_only")
        controller._stale_state = "stale"
        controller._last_ob_event_time = _time.time() - 100  # 100s > 90s hard threshold

        passive_exec = self._make_mock_executor("passive_1", is_active=True, is_trading=False)
        trading_exec = self._make_mock_executor("trading_1", is_active=True, is_trading=True)
        inactive_exec = self._make_mock_executor("inactive_1", is_active=False, is_trading=False)
        controller.executors_info = [passive_exec, trading_exec, inactive_exec]

        result = controller.executors_to_early_stop()

        stopped_ids = [a.executor_id for a in result]
        self.assertIn("passive_1", stopped_ids)
        self.assertIn("trading_1", stopped_ids)
        self.assertNotIn("inactive_1", stopped_ids)
        self.assertEqual(len(result), 2)

    def test_no_action_when_healthy(self):
        """When state is healthy, executors_to_early_stop returns empty list."""
        controller, mock_ob, _ = self._make_controller_with_order_book(snapshot_uid=1, last_diff_uid=0)
        controller._stale_state = "healthy"

        controller.executors_info = [
            self._make_mock_executor("exec_1", is_active=True, is_trading=False),
            self._make_mock_executor("exec_2", is_active=True, is_trading=True),
        ]

        result = controller.executors_to_early_stop()
        self.assertEqual(result, [])

    def test_default_config_is_cancel_passive(self):
        """Default stale_data_action should be 'cancel_passive_orders'."""
        config = MarketMakingControllerConfigBase(
            id="test_defaults",
            controller_name="test_defaults",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("1000"),
            buy_spreads=[0.01],
            sell_spreads=[0.01],
        )
        self.assertEqual(config.stale_data_action, "cancel_passive_orders")
        self.assertEqual(config.max_market_data_stale_seconds, 30)
        self.assertEqual(config.hard_market_data_stale_seconds, 90)
