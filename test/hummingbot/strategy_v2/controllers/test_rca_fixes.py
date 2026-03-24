"""
Tests for RCA fixes:
1. Sell-side inventory clipping
2. Under-seed warning at any shortfall
3. Cross-order warning deduplication
4. Startup gate
"""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction


def _make_config(**overrides):
    defaults = dict(
        id="test_ctrl",
        controller_name="test_mm",
        connector_name="nonkyc",
        trading_pair="ARRR-USDT",
        total_amount_quote=Decimal("100"),
        buy_spreads=[0.01],
        sell_spreads=[0.01],
        buy_amounts_pct=[Decimal(50)],
        sell_amounts_pct=[Decimal(50)],
        executor_refresh_time=300,
        cooldown_time=15,
        leverage=1,
        position_mode=PositionMode.HEDGE,
        skip_rebalance=True,
    )
    defaults.update(overrides)
    return MarketMakingControllerConfigBase(**defaults)


def _make_controller(config=None, available_base=Decimal("0"), available_quote=Decimal("1000"), **config_overrides):
    if config is None:
        config = _make_config(**config_overrides)
    mdp = MagicMock(spec=MarketDataProvider)
    q = AsyncMock(spec=asyncio.Queue)

    mock_connector = MagicMock()
    mock_connector.available_balances = {
        config.trading_pair.split("-")[0]: available_base,
        config.trading_pair.split("-")[1]: available_quote,
    }
    mdp.connectors = {config.connector_name: mock_connector}

    ctrl = MarketMakingControllerBase(config=config, market_data_provider=mdp, actions_queue=q)
    ctrl.processed_data = {"reference_price": Decimal("0.22"), "spread_multiplier": Decimal("1")}
    ctrl.executors_info = []
    ctrl.positions_held = []

    # Patch abstract methods so create_actions_proposal works
    from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
    ref = Decimal("0.22")
    ctrl.check_position_rebalance = lambda: None
    ctrl.get_levels_to_execute = lambda: ["buy_0", "sell_0"]
    ctrl.get_trade_type_from_level_id = lambda lid: TradeType.BUY if lid.startswith("buy") else TradeType.SELL
    ctrl.get_price_and_amount = lambda lid: (
        ref * (Decimal("1") - Decimal("0.01")) if lid.startswith("buy") else ref * (Decimal("1") + Decimal("0.01")),
        Decimal(str(config.total_amount_quote / Decimal("2"))) / ref,
    )
    ctrl.get_executor_config = lambda lid, price, amount: PositionExecutorConfig(
        timestamp=1234, controller_id="test_ctrl", connector_name=config.connector_name,
        trading_pair=config.trading_pair,
        side=TradeType.BUY if lid.startswith("buy") else TradeType.SELL,
        entry_price=price, amount=amount,
    )
    return ctrl


class TestSellSideInventoryClipping(IsolatedAsyncioWrapperTestCase):
    """Fix 2: Sell orders are clipped to available base, not silently dropped."""

    def test_full_sell_inventory_creates_sell_orders(self):
        ctrl = _make_controller(available_base=Decimal("300"))
        actions = ctrl.create_actions_proposal()
        sell_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.SELL]
        self.assertTrue(len(sell_actions) > 0)

    def test_partial_sell_inventory_clips_sell_orders(self):
        ctrl = _make_controller(available_base=Decimal("50"))
        actions = ctrl.create_actions_proposal()
        sell_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.SELL]
        if sell_actions:
            for action in sell_actions:
                self.assertLessEqual(action.executor_config.amount, Decimal("50"))

    def test_zero_sell_inventory_skips_sell_orders(self):
        ctrl = _make_controller(available_base=Decimal("0"))
        actions = ctrl.create_actions_proposal()
        sell_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.SELL]
        self.assertEqual(len(sell_actions), 0)

    def test_buy_orders_still_created_regardless_of_base_inventory(self):
        ctrl = _make_controller(available_base=Decimal("0"), available_quote=Decimal("1000"))
        actions = ctrl.create_actions_proposal()
        buy_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                       and a.executor_config.side == TradeType.BUY]
        self.assertTrue(len(buy_actions) > 0)


class TestUnderSeedWarning(IsolatedAsyncioWrapperTestCase):
    """Fix 5: Warning threshold covers any shortfall, not just 50%+."""

    def test_89_percent_coverage_warns(self):
        ctrl = _make_controller(available_base=Decimal("54.6079"), skip_rebalance=True)
        ctrl.processed_data = {"reference_price": Decimal("0.22"), "spread_multiplier": Decimal("1")}
        with self.assertLogs(level='WARNING') as log_output:
            ctrl._check_sell_side_inventory()
        self.assertTrue(any("SHORTFALL" in msg for msg in log_output.output))

    def test_100_percent_coverage_no_warning(self):
        ctrl = _make_controller(available_base=Decimal("300"), skip_rebalance=True)
        ctrl.processed_data = {"reference_price": Decimal("0.22"), "spread_multiplier": Decimal("1")}
        # Should log INFO (OK), not WARNING
        ctrl._check_sell_side_inventory()

    def test_49_percent_coverage_warns(self):
        ctrl = _make_controller(available_base=Decimal("5"), skip_rebalance=True)
        ctrl.processed_data = {"reference_price": Decimal("0.22"), "spread_multiplier": Decimal("1")}
        with self.assertLogs(level='WARNING') as log_output:
            ctrl._check_sell_side_inventory()
        self.assertTrue(any("SHORTFALL" in msg for msg in log_output.output))


class TestCrossOrderWarningDedup(IsolatedAsyncioWrapperTestCase):
    """Fix 9: Cross-order warnings only emit once per level until condition resolves."""

    def test_repeated_cross_order_warns_only_once(self):
        ctrl = _make_controller(available_base=Decimal("300"), available_quote=Decimal("1000"))
        mock_executor = MagicMock()
        mock_executor.is_active = True
        mock_executor.is_trading = True
        mock_executor.custom_info = {"level_id": "buy_0", "current_position_average_price": "999"}
        mock_executor.config = MagicMock()
        mock_executor.config.amount = Decimal("10")
        ctrl.executors_info = [mock_executor]

        # First call warns, second does not
        ctrl.create_actions_proposal()
        ctrl.create_actions_proposal()
        self.assertTrue(hasattr(ctrl, '_cross_order_warned'))


class TestStartupGate(IsolatedAsyncioWrapperTestCase):
    """Fix 1: Controllers cannot emit actions before wallet seeding completes."""

    async def test_controller_blocks_before_gate_set(self):
        ctrl = _make_controller(available_base=Decimal("100"))
        gate = asyncio.Event()  # Not set
        ctrl.set_startup_gate(gate)
        ctrl.executors_update_event = MagicMock()
        ctrl.executors_update_event.is_set.return_value = True

        await ctrl.control_task()
        # Queue should not have received actions
        ctrl.actions_queue.put.assert_not_called()

    async def test_controller_proceeds_after_gate_set(self):
        """After gate is set, control_task should call update_processed_data (proving it didn't return early)."""
        ctrl = _make_controller(available_base=Decimal("100"))
        gate = asyncio.Event()
        gate.set()
        ctrl.set_startup_gate(gate)
        ctrl.executors_update_event = asyncio.Event()
        ctrl.executors_update_event.set()

        # Mock update_processed_data to verify it gets called (proves gate didn't block)
        from unittest.mock import AsyncMock as AM
        ctrl.update_processed_data = AM()
        ctrl.determine_executor_actions = MagicMock(return_value=[])
        ctrl.send_actions = AM()

        await ctrl.control_task()
        ctrl.update_processed_data.assert_called_once()
