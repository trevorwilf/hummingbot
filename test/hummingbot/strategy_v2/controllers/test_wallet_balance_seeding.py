"""
Comprehensive tests for the use_wallet_balance feature.

Tests cover:
1. Config: use_wallet_balance flag defaults, serialisation, is_updatable=False
2. Controller: compute_wallet_seed_amount() logic
3. Orchestrator: add_wallet_position() creates correct PositionHold
4. Strategy: _seed_wallet_balances() integration — one-time seeding, retry, skip logic
"""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from hummingbot.core.data_type.common import MarketDict, PositionMode, PriceType, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.data_types import PositionSummary


# ---------------------------------------------------------------------------
# Helper: build a spot MarketMakingControllerConfigBase with sensible defaults
# ---------------------------------------------------------------------------
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
        skip_rebalance=False,
        use_wallet_balance=False,
    )
    defaults.update(overrides)
    return MarketMakingControllerConfigBase(**defaults)


def _make_controller(config=None, **config_overrides):
    if config is None:
        config = _make_config(**config_overrides)
    mdp = MagicMock(spec=MarketDataProvider)
    q = AsyncMock(spec=asyncio.Queue)
    ctrl = MarketMakingControllerBase(config=config, market_data_provider=mdp, actions_queue=q)
    ctrl.processed_data = {"reference_price": Decimal("5"), "spread_multiplier": Decimal("1")}
    ctrl.executors_info = []
    ctrl.positions_held = []
    return ctrl


# ==========================================================================
# 1. CONFIG TESTS
# ==========================================================================
class TestUseWalletBalanceConfig(IsolatedAsyncioWrapperTestCase):

    def test_default_is_false(self):
        config = _make_config()
        self.assertFalse(config.use_wallet_balance)

    def test_can_be_set_true(self):
        config = _make_config(use_wallet_balance=True)
        self.assertTrue(config.use_wallet_balance)

    def test_is_not_updatable(self):
        """use_wallet_balance should not be hot-updatable (is_updatable=False)."""
        field_info = MarketMakingControllerConfigBase.model_fields["use_wallet_balance"]
        extra = field_info.json_schema_extra or {}
        self.assertFalse(extra.get("is_updatable", True))

    def test_serialises_in_model_dump(self):
        config = _make_config(use_wallet_balance=True)
        dumped = config.model_dump()
        self.assertIn("use_wallet_balance", dumped)
        self.assertTrue(dumped["use_wallet_balance"])

    def test_existing_fields_unaffected(self):
        """Adding use_wallet_balance doesn't break existing config fields."""
        config = _make_config(
            use_wallet_balance=True,
            stop_loss=Decimal("0.05"),
            take_profit=Decimal("0.03"),
        )
        self.assertEqual(config.stop_loss, Decimal("0.05"))
        self.assertEqual(config.take_profit, Decimal("0.03"))
        self.assertEqual(config.connector_name, "nonkyc")


# ==========================================================================
# 2. CONTROLLER: compute_wallet_seed_amount() TESTS
# ==========================================================================
class TestComputeWalletSeedAmount(IsolatedAsyncioWrapperTestCase):

    def test_returns_zero_when_flag_false(self):
        ctrl = _make_controller(use_wallet_balance=False)
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(result, Decimal("0"))

    def test_returns_min_available_required(self):
        """When wallet has less than required, seeds the entire wallet balance."""
        ctrl = _make_controller(
            use_wallet_balance=True,
            total_amount_quote=Decimal("100"),
            trading_pair="ARRR-USDT",
        )
        # Sell side needs 50 quote / 5 price = 10 ARRR
        # Wallet has 7.5 ARRR
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("7.5")
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(result, Decimal("7.5"))

    def test_caps_at_required_when_wallet_has_excess(self):
        """When wallet has more than required, only seeds required amount."""
        ctrl = _make_controller(
            use_wallet_balance=True,
            total_amount_quote=Decimal("100"),
            trading_pair="ARRR-USDT",
        )
        # Sell side needs 50 quote / 5 price = 10 ARRR
        # Wallet has 20.1573 ARRR
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("20.1573")
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        required = ctrl.config.get_required_base_amount(Decimal("5"))
        self.assertEqual(result, required)
        self.assertLessEqual(result, Decimal("20.1573"))

    def test_returns_zero_when_zero_balance(self):
        ctrl = _make_controller(use_wallet_balance=True)
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("0")
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(result, Decimal("0"))

    def test_returns_zero_when_negative_balance(self):
        ctrl = _make_controller(use_wallet_balance=True)
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("-1")
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(result, Decimal("0"))

    def test_returns_zero_when_balance_none(self):
        ctrl = _make_controller(use_wallet_balance=True)
        ctrl.market_data_provider.get_available_balance.return_value = None
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(result, Decimal("0"))

    def test_returns_zero_on_balance_exception(self):
        """If the exchange raises, gracefully returns 0."""
        ctrl = _make_controller(use_wallet_balance=True)
        ctrl.market_data_provider.get_available_balance.side_effect = Exception("Connection error")
        result = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(result, Decimal("0"))

    def test_extracts_correct_base_asset(self):
        """Verify it queries the base asset (left side of the pair)."""
        ctrl = _make_controller(use_wallet_balance=True, trading_pair="BTC-USDT")
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("1")
        ctrl.compute_wallet_seed_amount(Decimal("50000"))
        ctrl.market_data_provider.get_available_balance.assert_called_once_with("nonkyc", "BTC")

    def test_different_price_affects_required(self):
        """Higher price means fewer base units required for the same quote budget."""
        ctrl = _make_controller(
            use_wallet_balance=True,
            total_amount_quote=Decimal("100"),
            trading_pair="ARRR-USDT",
        )
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("1000")

        result_low_price = ctrl.compute_wallet_seed_amount(Decimal("1"))
        result_high_price = ctrl.compute_wallet_seed_amount(Decimal("10"))
        # At lower price, more base is required
        self.assertGreater(result_low_price, result_high_price)

    def test_wallet_balance_seeded_flag_initialised(self):
        """Controller starts with _wallet_balance_seeded=False."""
        ctrl = _make_controller()
        self.assertFalse(ctrl._wallet_balance_seeded)


# ==========================================================================
# 3. ORCHESTRATOR: add_wallet_position() TESTS
# ==========================================================================
class TestAddWalletPosition(IsolatedAsyncioWrapperTestCase):

    def _make_orchestrator(self):
        """Create a minimal mock ExecutorOrchestrator."""
        from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
        strategy = MagicMock()
        strategy.controllers = {}
        strategy.markets = {"nonkyc": {"ARRR-USDT"}}
        strategy.connectors = {}
        # Avoid DB calls in __init__
        with patch.object(ExecutorOrchestrator, '_initialize_cached_performance'):
            orch = ExecutorOrchestrator(strategy=strategy)
        orch.positions_held = {}
        orch.active_executors = {}
        orch.cached_performance = {}
        return orch

    def test_creates_buy_position_hold(self):
        orch = self._make_orchestrator()
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            side=TradeType.BUY,
            amount=Decimal("20.1573"),
        )
        positions = orch.positions_held.get("ctrl1", [])
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.connector_name, "nonkyc")
        self.assertEqual(pos.trading_pair, "ARRR-USDT")
        self.assertEqual(pos.side, TradeType.BUY)
        self.assertEqual(pos.buy_amount_base, Decimal("20.1573"))
        self.assertTrue(pos.buy_amount_quote.is_nan())  # Lazy-calculated
        self.assertEqual(pos.sell_amount_base, Decimal("0"))
        self.assertEqual(pos.sell_amount_quote, Decimal("0"))
        self.assertEqual(pos.volume_traded_quote, Decimal("0"))
        self.assertEqual(pos.cum_fees_quote, Decimal("0"))

    def test_creates_sell_position_hold(self):
        orch = self._make_orchestrator()
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            side=TradeType.SELL,
            amount=Decimal("5.0"),
        )
        positions = orch.positions_held.get("ctrl1", [])
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.sell_amount_base, Decimal("5.0"))
        self.assertTrue(pos.sell_amount_quote.is_nan())
        self.assertEqual(pos.buy_amount_base, Decimal("0"))

    def test_zero_amount_is_no_op(self):
        orch = self._make_orchestrator()
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            side=TradeType.BUY,
            amount=Decimal("0"),
        )
        self.assertEqual(len(orch.positions_held.get("ctrl1", [])), 0)

    def test_negative_amount_is_no_op(self):
        orch = self._make_orchestrator()
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            side=TradeType.BUY,
            amount=Decimal("-5"),
        )
        self.assertEqual(len(orch.positions_held.get("ctrl1", [])), 0)

    def test_initialises_positions_list_if_missing(self):
        orch = self._make_orchestrator()
        # positions_held has no key for ctrl1
        self.assertNotIn("ctrl1", orch.positions_held)
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            side=TradeType.BUY,
            amount=Decimal("10"),
        )
        self.assertIn("ctrl1", orch.positions_held)
        self.assertEqual(len(orch.positions_held["ctrl1"]), 1)

    def test_appends_to_existing_positions(self):
        orch = self._make_orchestrator()
        orch.positions_held["ctrl1"] = []
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            side=TradeType.BUY,
            amount=Decimal("5"),
        )
        orch.add_wallet_position(
            controller_id="ctrl1",
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
            side=TradeType.BUY,
            amount=Decimal("0.5"),
        )
        self.assertEqual(len(orch.positions_held["ctrl1"]), 2)


# ==========================================================================
# 4. STRATEGY: _seed_wallet_balances() INTEGRATION TESTS
# ==========================================================================
class TestSeedWalletBalances(IsolatedAsyncioWrapperTestCase):

    def _make_strategy(self, controllers_dict=None):
        """Build a minimal mock of StrategyV2Base with just enough for _seed_wallet_balances."""
        from hummingbot.strategy.strategy_v2_base import StrategyV2Base
        strategy = MagicMock(spec=StrategyV2Base)
        strategy.controllers = controllers_dict or {}
        strategy.market_data_provider = MagicMock(spec=MarketDataProvider)
        strategy.executor_orchestrator = MagicMock()
        strategy._wallet_balances_seeded = False
        strategy.logger = MagicMock(return_value=MagicMock())
        # Bind the real method to our mock
        strategy._seed_wallet_balances = StrategyV2Base._seed_wallet_balances.__get__(strategy)
        return strategy

    def test_seeds_once_then_marks_done(self):
        ctrl = _make_controller(use_wallet_balance=True, connector_name="nonkyc", trading_pair="ARRR-USDT")
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("20")

        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("5")

        strategy._seed_wallet_balances()

        self.assertTrue(strategy._wallet_balances_seeded)
        self.assertTrue(ctrl._wallet_balance_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_called_once()

    def test_skips_when_flag_false(self):
        ctrl = _make_controller(use_wallet_balance=False)
        strategy = self._make_strategy({"ctrl1": ctrl})

        strategy._seed_wallet_balances()

        self.assertTrue(strategy._wallet_balances_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_skips_perpetual_connector(self):
        ctrl = _make_controller(
            use_wallet_balance=True,
            connector_name="nonkyc_perpetual",
            trading_pair="ARRR-USDT",
        )
        strategy = self._make_strategy({"ctrl1": ctrl})

        strategy._seed_wallet_balances()

        self.assertTrue(strategy._wallet_balances_seeded)
        self.assertTrue(ctrl._wallet_balance_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_retries_when_no_price_available(self):
        ctrl = _make_controller(use_wallet_balance=True, connector_name="nonkyc", trading_pair="ARRR-USDT")
        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("NaN")

        strategy._seed_wallet_balances()

        # Should NOT be marked done — will retry next tick
        self.assertFalse(strategy._wallet_balances_seeded)
        self.assertFalse(ctrl._wallet_balance_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_retries_when_price_is_zero(self):
        ctrl = _make_controller(use_wallet_balance=True, connector_name="nonkyc", trading_pair="ARRR-USDT")
        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("0")

        strategy._seed_wallet_balances()

        self.assertFalse(strategy._wallet_balances_seeded)

    def test_does_not_retry_on_exception(self):
        ctrl = _make_controller(use_wallet_balance=True, connector_name="nonkyc", trading_pair="ARRR-USDT")
        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.side_effect = Exception("boom")

        strategy._seed_wallet_balances()

        # Marked done even on exception (don't retry forever)
        self.assertTrue(ctrl._wallet_balance_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_zero_seed_amount_no_position_created(self):
        ctrl = _make_controller(use_wallet_balance=True, connector_name="nonkyc", trading_pair="ARRR-USDT")
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("0")
        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("5")

        strategy._seed_wallet_balances()

        self.assertTrue(strategy._wallet_balances_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_already_seeded_controller_is_skipped(self):
        ctrl = _make_controller(use_wallet_balance=True, connector_name="nonkyc", trading_pair="ARRR-USDT")
        ctrl._wallet_balance_seeded = True  # Already done

        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("5")

        strategy._seed_wallet_balances()

        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_non_market_making_controller_skipped(self):
        """Only MarketMakingControllerConfigBase controllers are considered."""
        from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
        generic_ctrl = MagicMock(spec=ControllerBase)
        generic_ctrl.config = MagicMock(spec=ControllerConfigBase)
        generic_ctrl.config.use_wallet_balance = True  # Would be True if it existed

        strategy = self._make_strategy({"ctrl1": generic_ctrl})

        strategy._seed_wallet_balances()

        self.assertTrue(strategy._wallet_balances_seeded)
        strategy.executor_orchestrator.add_wallet_position.assert_not_called()

    def test_multiple_controllers_mixed(self):
        """Two controllers: one with flag on, one off. Only the enabled one gets seeded."""
        ctrl_on = _make_controller(
            id="ctrl_on",
            use_wallet_balance=True,
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
        )
        ctrl_on.market_data_provider.get_available_balance.return_value = Decimal("15")

        ctrl_off = _make_controller(
            id="ctrl_off",
            use_wallet_balance=False,
            connector_name="nonkyc",
            trading_pair="BTC-USDT",
        )

        strategy = self._make_strategy({"ctrl_on": ctrl_on, "ctrl_off": ctrl_off})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("5")

        strategy._seed_wallet_balances()

        self.assertTrue(strategy._wallet_balances_seeded)
        # Only ctrl_on should have been seeded
        strategy.executor_orchestrator.add_wallet_position.assert_called_once()
        call_kwargs = strategy.executor_orchestrator.add_wallet_position.call_args
        self.assertEqual(call_kwargs[1]["controller_id"], "ctrl_on")
        self.assertEqual(call_kwargs[1]["side"], TradeType.BUY)

    def test_add_wallet_position_called_with_correct_args(self):
        """Verify the exact arguments passed to add_wallet_position."""
        ctrl = _make_controller(
            use_wallet_balance=True,
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            total_amount_quote=Decimal("100"),
        )
        # Wallet has 20.1573 ARRR, sell side requires ~10 ARRR at price 5
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("20.1573")

        strategy = self._make_strategy({"ctrl1": ctrl})
        strategy.market_data_provider.get_price_by_type.return_value = Decimal("5")

        strategy._seed_wallet_balances()

        strategy.executor_orchestrator.add_wallet_position.assert_called_once()
        call_kwargs = strategy.executor_orchestrator.add_wallet_position.call_args[1]
        self.assertEqual(call_kwargs["controller_id"], "ctrl1")
        self.assertEqual(call_kwargs["connector_name"], "nonkyc")
        self.assertEqual(call_kwargs["trading_pair"], "ARRR-USDT")
        self.assertEqual(call_kwargs["side"], TradeType.BUY)
        # Seed should be min(20.1573, required). Required = sell_side_quote / price
        expected_required = ctrl.config.get_required_base_amount(Decimal("5"))
        self.assertEqual(call_kwargs["amount"], expected_required)


# ==========================================================================
# 5. END-TO-END SCENARIO TESTS
# ==========================================================================
class TestWalletSeedEndToEnd(IsolatedAsyncioWrapperTestCase):
    """Scenario-based tests matching the user's example."""

    def test_user_scenario_arrr_less_than_needed(self):
        """
        User scenario: total_amount_quote=100, trading ARRR at price 5.
        Sell side gets 50 quote -> needs 10 ARRR.
        User only has 7.5 ARRR. Controller should seed 7.5.
        """
        ctrl = _make_controller(
            use_wallet_balance=True,
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            total_amount_quote=Decimal("100"),
        )
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("7.5")
        seed = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(seed, Decimal("7.5"))

    def test_user_scenario_arrr_more_than_needed(self):
        """
        User scenario: total_amount_quote=100, trading ARRR at price 5.
        Sell side gets 50 quote -> needs 10 ARRR.
        User has 20.1573 ARRR. Controller should seed exactly 10.
        """
        ctrl = _make_controller(
            use_wallet_balance=True,
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            total_amount_quote=Decimal("100"),
        )
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("20.1573")
        seed = ctrl.compute_wallet_seed_amount(Decimal("5"))
        required = ctrl.config.get_required_base_amount(Decimal("5"))
        self.assertEqual(seed, required)

    def test_user_scenario_arrr_exactly_enough(self):
        """User has exactly the amount needed."""
        ctrl = _make_controller(
            use_wallet_balance=True,
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            total_amount_quote=Decimal("100"),
        )
        required = ctrl.config.get_required_base_amount(Decimal("5"))
        ctrl.market_data_provider.get_available_balance.return_value = required
        seed = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(seed, required)

    def test_user_scenario_no_arrr_at_all(self):
        """User has 0 ARRR. Controller seeds nothing."""
        ctrl = _make_controller(
            use_wallet_balance=True,
            connector_name="nonkyc",
            trading_pair="ARRR-USDT",
            total_amount_quote=Decimal("100"),
        )
        ctrl.market_data_provider.get_available_balance.return_value = Decimal("0")
        seed = ctrl.compute_wallet_seed_amount(Decimal("5"))
        self.assertEqual(seed, Decimal("0"))


# ==========================================================================
# 6. RACE CONDITION: on_tick refreshes controller view after seeding
# ==========================================================================
class TestOnTickRefreshAfterSeed(IsolatedAsyncioWrapperTestCase):
    """
    Verify that on_tick() calls update_executors_info() AFTER _seed_wallet_balances()
    so controllers see the newly-seeded positions before determine_executor_actions().
    Without this, check_position_rebalance() would see empty positions and fire
    a spurious buy order on the very first tick.
    """

    def _make_strategy_with_on_tick(self):
        """Build a mock strategy that tracks call order."""
        from hummingbot.strategy.strategy_v2_base import StrategyV2Base

        strategy = MagicMock(spec=StrategyV2Base)
        strategy.controllers = {"ctrl1": MagicMock()}
        strategy._is_stop_triggered = False
        strategy._wallet_balances_seeded = False

        # Track call order
        call_log = []
        strategy.update_executors_info = MagicMock(side_effect=lambda: call_log.append("update_executors_info"))
        strategy.update_controllers_configs = MagicMock(side_effect=lambda: call_log.append("update_controllers_configs"))
        strategy._seed_wallet_balances = MagicMock(side_effect=lambda: call_log.append("_seed_wallet_balances"))
        strategy.determine_executor_actions = MagicMock(
            side_effect=lambda: (call_log.append("determine_executor_actions"), [])[1]
        )
        strategy.executor_orchestrator = MagicMock()

        # market_data_provider.ready = True
        strategy.market_data_provider = MagicMock()
        type(strategy.market_data_provider).ready = PropertyMock(return_value=True)

        # Bind the real on_tick method
        strategy.on_tick = StrategyV2Base.on_tick.__get__(strategy)

        return strategy, call_log

    def test_update_executors_info_called_after_seed(self):
        """update_executors_info runs both BEFORE and AFTER _seed_wallet_balances."""
        strategy, call_log = self._make_strategy_with_on_tick()

        strategy.on_tick()

        # Expected order: update_executors_info, update_controllers_configs,
        #                 _seed_wallet_balances, update_executors_info (refresh),
        #                 determine_executor_actions
        self.assertEqual(call_log[0], "update_executors_info")
        seed_idx = call_log.index("_seed_wallet_balances")
        # There must be an update_executors_info AFTER the seed
        post_seed_updates = [
            i for i, c in enumerate(call_log)
            if c == "update_executors_info" and i > seed_idx
        ]
        self.assertTrue(
            len(post_seed_updates) > 0,
            f"No update_executors_info after _seed_wallet_balances. Call log: {call_log}"
        )
        # determine_executor_actions must come AFTER the post-seed refresh
        determine_idx = call_log.index("determine_executor_actions")
        self.assertGreater(
            determine_idx, post_seed_updates[0],
            f"determine_executor_actions ran before post-seed refresh. Call log: {call_log}"
        )

    def test_no_double_refresh_when_already_seeded(self):
        """Once _wallet_balances_seeded=True, only the normal single update runs."""
        strategy, call_log = self._make_strategy_with_on_tick()
        strategy._wallet_balances_seeded = True

        strategy.on_tick()

        # Should only have one update_executors_info (the normal one at the top)
        update_count = call_log.count("update_executors_info")
        self.assertEqual(update_count, 1, f"Expected 1 update, got {update_count}. Call log: {call_log}")
        # _seed_wallet_balances should NOT be called
        self.assertNotIn("_seed_wallet_balances", call_log)
