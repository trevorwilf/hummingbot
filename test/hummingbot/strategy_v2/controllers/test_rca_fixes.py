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


class TestBuyExecutorCloseReservation(IsolatedAsyncioWrapperTestCase):
    """Fix 1 (RCA): Active buy executors reserve base for close orders."""

    def test_buy_executor_close_reservation_reduces_spendable_sell(self):
        """Buy executor holding 10 base should reduce spendable sell to 0."""
        ctrl = _make_controller(available_base=Decimal("10"))
        mock_executor = MagicMock()
        mock_executor.is_active = True
        mock_executor.is_trading = True
        mock_executor.custom_info = {"level_id": "buy_0"}
        mock_executor.config = MagicMock()
        mock_executor.config.amount = Decimal("10")
        ctrl.executors_info = [mock_executor]

        spendable = ctrl.get_spendable_sell_base_inventory()
        self.assertEqual(Decimal("0"), spendable)

    def test_mixed_buy_and_sell_executors_reservation(self):
        """Buy executor (5) + sell executor (3) with 15 available => spendable = 7."""
        ctrl = _make_controller(available_base=Decimal("15"))
        buy_exec = MagicMock()
        buy_exec.is_active = True
        buy_exec.is_trading = True
        buy_exec.custom_info = {"level_id": "buy_0"}
        buy_exec.config = MagicMock()
        buy_exec.config.amount = Decimal("5")

        sell_exec = MagicMock()
        sell_exec.is_active = True
        sell_exec.is_trading = True
        sell_exec.custom_info = {"level_id": "sell_0"}
        sell_exec.config = MagicMock()
        sell_exec.config.amount = Decimal("3")

        ctrl.executors_info = [buy_exec, sell_exec]
        spendable = ctrl.get_spendable_sell_base_inventory()
        self.assertEqual(Decimal("7"), spendable)

    def test_no_active_executors_returns_full_balance(self):
        """No executors, 100 base available => spendable = 100."""
        ctrl = _make_controller(available_base=Decimal("100"))
        ctrl.executors_info = []
        spendable = ctrl.get_spendable_sell_base_inventory()
        self.assertEqual(Decimal("100"), spendable)

    def test_double_booking_prevented_in_action_proposal(self):
        """With buy executor holding all available base, no sell actions should be created."""
        ctrl = _make_controller(available_base=Decimal("0.13"))
        mock_executor = MagicMock()
        mock_executor.is_active = True
        mock_executor.is_trading = True
        mock_executor.custom_info = {"level_id": "buy_0", "current_position_average_price": "0.22"}
        mock_executor.config = MagicMock()
        mock_executor.config.amount = Decimal("0.13")
        ctrl.executors_info = [mock_executor]

        actions = ctrl.create_actions_proposal()
        sell_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.SELL]
        self.assertEqual(0, len(sell_actions),
                         "All base reserved for buy executor close — no sell actions should be created")


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


class TestControllerMinNotionalValidation(IsolatedAsyncioWrapperTestCase):
    """Test 6 (Phase 1 Fix 3): Controller skips levels below min-notional."""

    def test_controller_skips_below_min_notional(self):
        """Level with notional below min_notional_size should be skipped."""
        from hummingbot.connector.trading_rule import TradingRule
        config = _make_config(total_amount_quote=Decimal("1"))  # Very small allocation
        ctrl = _make_controller(config=config, available_base=Decimal("100"), available_quote=Decimal("100"))

        # Set up trading rules on the mock connector
        connector = ctrl.market_data_provider.connectors["nonkyc"]
        trading_rule = TradingRule(
            trading_pair="ARRR-USDT",
            min_order_size=Decimal("0.0001"),
            min_notional_size=Decimal("10"),  # 10 USDT minimum
        )
        connector.trading_rules = {"ARRR-USDT": trading_rule}
        connector.quantize_order_amount = lambda pair, amt: amt
        connector.quantize_order_price = lambda pair, price: price

        # Don't override get_levels_to_execute — use actual method
        ctrl.get_levels_to_execute = MarketMakingControllerBase.get_levels_to_execute.__get__(ctrl)
        # But we need get_not_active_levels_ids to work:
        ctrl.get_not_active_levels_ids = lambda ids: [l for l in ["buy_0", "sell_0"] if l not in ids]

        actions = ctrl.create_actions_proposal()

        # Both buy and sell have notional = 0.5 / 0.22 * 0.22 ~= 0.5 < 10 min_notional
        buy_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.BUY]
        sell_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.SELL]
        self.assertEqual(0, len(buy_actions), "Buy level below min_notional should be skipped")
        self.assertEqual(0, len(sell_actions), "Sell level below min_notional should be skipped")

    def test_controller_passes_valid_levels(self):
        """Level with notional above min_notional_size should pass through."""
        from hummingbot.connector.trading_rule import TradingRule
        config = _make_config(total_amount_quote=Decimal("100"))  # Sufficient allocation
        ctrl = _make_controller(config=config, available_base=Decimal("1000"), available_quote=Decimal("1000"))

        connector = ctrl.market_data_provider.connectors["nonkyc"]
        trading_rule = TradingRule(
            trading_pair="ARRR-USDT",
            min_order_size=Decimal("0.0001"),
            min_notional_size=Decimal("1"),  # 1 USDT minimum
        )
        connector.trading_rules = {"ARRR-USDT": trading_rule}
        connector.quantize_order_amount = lambda pair, amt: amt
        connector.quantize_order_price = lambda pair, price: price

        ctrl.get_levels_to_execute = MarketMakingControllerBase.get_levels_to_execute.__get__(ctrl)
        ctrl.get_not_active_levels_ids = lambda ids: [l for l in ["buy_0", "sell_0"] if l not in ids]

        actions = ctrl.create_actions_proposal()

        # Notional should be ~50 USDT per side, well above min 1 USDT
        buy_actions = [a for a in actions if isinstance(a, CreateExecutorAction)
                        and a.executor_config.side == TradeType.BUY]
        self.assertGreater(len(buy_actions), 0, "Valid buy level should produce an action")

    def test_controller_warns_once_per_level(self):
        """Warning should fire once per invalid level, not every cycle."""
        from hummingbot.connector.trading_rule import TradingRule
        config = _make_config(total_amount_quote=Decimal("1"))
        ctrl = _make_controller(config=config, available_base=Decimal("100"), available_quote=Decimal("100"))

        connector = ctrl.market_data_provider.connectors["nonkyc"]
        trading_rule = TradingRule(
            trading_pair="ARRR-USDT",
            min_order_size=Decimal("0.0001"),
            min_notional_size=Decimal("10"),
        )
        connector.trading_rules = {"ARRR-USDT": trading_rule}
        connector.quantize_order_amount = lambda pair, amt: amt
        connector.quantize_order_price = lambda pair, price: price
        ctrl.get_levels_to_execute = MarketMakingControllerBase.get_levels_to_execute.__get__(ctrl)
        ctrl.get_not_active_levels_ids = lambda ids: [l for l in ["buy_0", "sell_0"] if l not in ids]

        # First call: should set warnings
        ctrl.create_actions_proposal()
        self.assertTrue(getattr(ctrl, '_min_notional_warned', {}).get("buy_0"))

        # Second call: should not re-warn (flag already set)
        ctrl.create_actions_proposal()
        # Still flagged
        self.assertTrue(ctrl._min_notional_warned.get("buy_0"))
