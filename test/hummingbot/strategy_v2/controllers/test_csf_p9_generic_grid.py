"""
CSF-V1 Phase 9 tests — generic controllers: grid family.

Covers:
- GEN-9: pydantic geometry validators on GridExecutorConfig and the grid controller
  configs (grid_strike, multi_grid_strike per-grid, quantum) — inverted range, zero
  start, wrong-side limit and pct-sum > 1 are rejected at config time (these configs
  bypass the orchestrator budget preflight, so validators are the only gate).
- GEN-6: multi_grid_strike grid respawns immediately after its executor terminates
  even while the corpse lingers in executors_info (archival lag); the mapping is
  pruned; the disabled-grid stop path only stops active executors.
- GEN-8: editing a still-enabled grid's parameters stops its active executor so the
  create path re-issues with the new parameters.
- GEN-10: grid_strike re-entry cooldown after termination and consecutive-stop-out
  breaker (halts creation with a rate-limited warning).
- GEN-11: quantum_grid_allocator falls back to config.grid_range when ta.bbands
  returns None or the width is not finite/positive; runtime GridExecutorConfig
  construction with degenerate geometry is skipped fail-closed instead of raising.
- GEN-17: lp_rebalancer captures closed-position amounts on EVERY termination of the
  tracked executor (not just rebalance-initiated ones) so replacements are clamped
  to what the closed position actually returned.
"""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
from pydantic import ValidationError

from controllers.generic.grid_strike import GridStrike, GridStrikeConfig
from controllers.generic.lp_rebalancer.lp_rebalancer import LPRebalancer, LPRebalancerConfig
from controllers.generic.multi_grid_strike import GridConfig, MultiGridStrike, MultiGridStrikeConfig
from controllers.generic.quantum_grid_allocator import QGAConfig, QuantumGridAllocator
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.lp_executor.data_types import LPExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def valid_grid_executor_kwargs(**overrides):
    kwargs = dict(
        timestamp=1000.0,
        connector_name="kraken",
        trading_pair="SOL-USDT",
        side=TradeType.BUY,
        start_price=Decimal("100"),
        end_price=Decimal("120"),
        limit_price=Decimal("90"),
        total_amount_quote=Decimal("100"),
        triple_barrier_config=TripleBarrierConfig(take_profit=Decimal("0.001")),
    )
    kwargs.update(overrides)
    return kwargs


def make_grid_executor_info(config: GridExecutorConfig, executor_id: str = "e1",
                            is_active: bool = True, close_type=None, close_timestamp=None):
    return ExecutorInfo(
        id=executor_id,
        timestamp=config.timestamp,
        type="grid_executor",
        status=RunnableStatus.RUNNING if is_active else RunnableStatus.TERMINATED,
        config=config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=Decimal("0"),
        is_active=is_active,
        is_trading=is_active,
        custom_info={},
        close_type=close_type,
        close_timestamp=close_timestamp,
    )


class TestGridExecutorConfigValidators(IsolatedAsyncioWrapperTestCase):
    """GEN-9 — validators on GridExecutorConfig itself."""

    def test_valid_buy_geometry_accepted(self):
        config = GridExecutorConfig(**valid_grid_executor_kwargs())
        self.assertEqual(Decimal("100"), config.start_price)

    def test_valid_sell_geometry_accepted(self):
        config = GridExecutorConfig(**valid_grid_executor_kwargs(
            side=TradeType.SELL, limit_price=Decimal("130")))
        self.assertEqual(Decimal("130"), config.limit_price)

    def test_zero_start_price_rejected(self):
        # Would-have-caught: start=0 previously reached level construction and
        # raised ZeroDivisionError inside the executor.
        with self.assertRaises(ValidationError):
            GridExecutorConfig(**valid_grid_executor_kwargs(
                start_price=Decimal("0"), limit_price=Decimal("-1")))

    def test_inverted_range_rejected(self):
        with self.assertRaises(ValidationError):
            GridExecutorConfig(**valid_grid_executor_kwargs(
                start_price=Decimal("120"), end_price=Decimal("100")))

    def test_equal_start_end_rejected(self):
        with self.assertRaises(ValidationError):
            GridExecutorConfig(**valid_grid_executor_kwargs(
                start_price=Decimal("100"), end_price=Decimal("100")))

    def test_buy_limit_above_start_rejected(self):
        # Wrong-side limit → instant limit-breach stop on every creation
        with self.assertRaises(ValidationError):
            GridExecutorConfig(**valid_grid_executor_kwargs(limit_price=Decimal("110")))

    def test_sell_limit_below_end_rejected(self):
        with self.assertRaises(ValidationError):
            GridExecutorConfig(**valid_grid_executor_kwargs(
                side=TradeType.SELL, limit_price=Decimal("90")))

    def test_nan_price_rejected(self):
        with self.assertRaises(ValidationError):
            GridExecutorConfig(**valid_grid_executor_kwargs(start_price=Decimal("NaN")))


class TestGridStrikeConfigValidators(IsolatedAsyncioWrapperTestCase):
    """GEN-9 — validators on GridStrikeConfig."""

    def _kwargs(self, **overrides):
        kwargs = dict(
            id="test-grid-strike",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            side=TradeType.BUY,
            start_price=Decimal("100"),
            end_price=Decimal("120"),
            limit_price=Decimal("90"),
            total_amount_quote=Decimal("100"),
        )
        kwargs.update(overrides)
        return kwargs

    def test_valid_config_accepted(self):
        config = GridStrikeConfig(**self._kwargs())
        self.assertEqual(Decimal("90"), config.limit_price)

    def test_inverted_range_rejected(self):
        with self.assertRaises(ValidationError):
            GridStrikeConfig(**self._kwargs(start_price=Decimal("120"), end_price=Decimal("100")))

    def test_zero_start_rejected(self):
        with self.assertRaises(ValidationError):
            GridStrikeConfig(**self._kwargs(start_price=Decimal("0"), limit_price=Decimal("-1")))

    def test_buy_wrong_side_limit_rejected(self):
        with self.assertRaises(ValidationError):
            GridStrikeConfig(**self._kwargs(limit_price=Decimal("105")))

    def test_sell_wrong_side_limit_rejected(self):
        with self.assertRaises(ValidationError):
            GridStrikeConfig(**self._kwargs(side=TradeType.SELL, limit_price=Decimal("110")))


class TestMultiGridConfigValidators(IsolatedAsyncioWrapperTestCase):
    """GEN-9 — validators on GridConfig (per grid) and MultiGridStrikeConfig (pct sum)."""

    def _grid_kwargs(self, **overrides):
        kwargs = dict(
            grid_id="g1",
            start_price=Decimal("100"),
            end_price=Decimal("120"),
            limit_price=Decimal("90"),
            side=TradeType.BUY,
            amount_quote_pct=Decimal("0.5"),
        )
        kwargs.update(overrides)
        return kwargs

    def test_valid_grid_accepted(self):
        grid = GridConfig(**self._grid_kwargs())
        self.assertEqual("g1", grid.grid_id)

    def test_inverted_range_rejected(self):
        with self.assertRaises(ValidationError):
            GridConfig(**self._grid_kwargs(start_price=Decimal("120"), end_price=Decimal("100")))

    def test_wrong_side_limit_rejected(self):
        with self.assertRaises(ValidationError):
            GridConfig(**self._grid_kwargs(limit_price=Decimal("110")))

    def test_zero_amount_pct_rejected(self):
        with self.assertRaises(ValidationError):
            GridConfig(**self._grid_kwargs(amount_quote_pct=Decimal("0")))

    def test_enabled_pct_sum_above_one_rejected(self):
        with self.assertRaises(ValidationError):
            MultiGridStrikeConfig(
                id="test-multi-grid",
                connector_name="kraken",
                trading_pair="SOL-USDT",
                grids=[
                    GridConfig(**self._grid_kwargs(grid_id="g1", amount_quote_pct=Decimal("0.6"))),
                    GridConfig(**self._grid_kwargs(grid_id="g2", amount_quote_pct=Decimal("0.6"))),
                ],
            )

    def test_disabled_grids_not_counted_in_pct_sum(self):
        config = MultiGridStrikeConfig(
            id="test-multi-grid",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            grids=[
                GridConfig(**self._grid_kwargs(grid_id="g1", amount_quote_pct=Decimal("0.6"))),
                GridConfig(**self._grid_kwargs(grid_id="g2", amount_quote_pct=Decimal("0.6"), enabled=False)),
            ],
        )
        self.assertEqual(2, len(config.grids))


class TestQGAConfigValidators(IsolatedAsyncioWrapperTestCase):
    """GEN-9 — validators on the quantum allocator's geometry-driving parameters."""

    def test_defaults_accepted(self):
        config = QGAConfig(id="test-qga")
        self.assertEqual(Decimal("0.002"), config.grid_range)

    def test_zero_grid_range_rejected(self):
        with self.assertRaises(ValidationError):
            QGAConfig(id="test-qga", grid_range=Decimal("0"))

    def test_nan_grid_range_rejected(self):
        with self.assertRaises(ValidationError):
            QGAConfig(id="test-qga", grid_range=Decimal("NaN"))

    def test_tp_sl_ratio_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            QGAConfig(id="test-qga", tp_sl_ratio=Decimal("1"))
        with self.assertRaises(ValidationError):
            QGAConfig(id="test-qga", tp_sl_ratio=Decimal("0"))

    def test_zero_limit_price_spread_rejected(self):
        with self.assertRaises(ValidationError):
            QGAConfig(id="test-qga", limit_price_spread=Decimal("0"))


class MultiGridStrikeTestBase(IsolatedAsyncioWrapperTestCase):
    def _make_controller(self, grids):
        config = MultiGridStrikeConfig(
            id="test-multi-grid",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            total_amount_quote=Decimal("1000"),
            grids=grids,
        )
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=1000.0)
        market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("110"))
        controller = MultiGridStrike(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def _grid(self, grid_id="g1", **overrides):
        kwargs = dict(
            grid_id=grid_id,
            start_price=Decimal("100"),
            end_price=Decimal("120"),
            limit_price=Decimal("90"),
            side=TradeType.BUY,
            amount_quote_pct=Decimal("0.5"),
        )
        kwargs.update(overrides)
        return GridConfig(**kwargs)

    def _executor_for_grid(self, controller, grid, executor_id="e1", is_active=True,
                           close_type=None, close_timestamp=None):
        executor_config = GridExecutorConfig(**valid_grid_executor_kwargs(
            start_price=grid.start_price, end_price=grid.end_price,
            limit_price=grid.limit_price, side=grid.side, level_id=grid.grid_id))
        return make_grid_executor_info(executor_config, executor_id=executor_id,
                                       is_active=is_active, close_type=close_type,
                                       close_timestamp=close_timestamp)


class TestMultiGridRespawnAndStops(MultiGridStrikeTestBase):
    """GEN-6."""

    def test_grid_respawns_while_terminated_executor_still_in_buffer(self):
        # Would-have-caught: the terminated executor lingers in executors_info until
        # global archival; old get_executor_by_grid_id returned the corpse and the
        # grid never respawned.
        grid = self._grid()
        controller = self._make_controller([grid])
        corpse = self._executor_for_grid(controller, grid, is_active=False,
                                         close_type=CloseType.TAKE_PROFIT, close_timestamp=900.0)
        controller.executors_info = [corpse]
        controller._grid_executor_mapping = {"g1": corpse.id}

        actions = controller.determine_executor_actions()

        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(create_actions))
        self.assertEqual(Decimal("100"), create_actions[0].executor_config.start_price)

    async def test_mapping_pruned_for_done_executor(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        corpse = self._executor_for_grid(controller, grid, is_active=False,
                                         close_type=CloseType.TAKE_PROFIT, close_timestamp=900.0)
        controller.executors_info = [corpse]
        controller._grid_executor_mapping = {"g1": corpse.id}

        await controller.update_processed_data()

        self.assertNotIn("g1", controller._grid_executor_mapping)

    def test_disabled_grid_does_not_stop_terminated_executor(self):
        # Regression: old code sent StopExecutorAction to the corpse.
        grid = self._grid()
        controller = self._make_controller([grid])
        corpse = self._executor_for_grid(controller, grid, is_active=False,
                                         close_type=CloseType.TAKE_PROFIT, close_timestamp=900.0)
        controller.executors_info = [corpse]
        controller._grid_executor_mapping = {"g1": corpse.id}
        grid.enabled = False  # hot-disable (assignment does not re-validate)

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(0, len(stop_actions))
        self.assertNotIn("g1", controller._grid_executor_mapping)

    def test_disabled_grid_stops_active_executor(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, is_active=True)
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": active.id}
        grid.enabled = False

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual(active.id, stop_actions[0].executor_id)


class TestMultiGridParamEdit(MultiGridStrikeTestBase):
    """GEN-8."""

    def test_unchanged_params_do_not_stop_executor(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, is_active=True)
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": active.id}

        actions = controller.determine_executor_actions()

        self.assertEqual([], actions)

    async def test_param_edit_stops_then_recreates_with_new_params(self):
        # Would-have-caught: old code detected the config change but only handled
        # removed/disabled grids — the edit was never applied.
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, is_active=True)
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": active.id}

        # Hot-edit the still-enabled grid's start price
        grid.start_price = Decimal("95")

        actions = controller.determine_executor_actions()
        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual(active.id, stop_actions[0].executor_id)
        # No create while the old executor is still winding down
        self.assertEqual(0, len(create_actions))

        # Executor terminates; the mapping is pruned on the next processed-data pass
        terminated = self._executor_for_grid(controller, grid, is_active=False,
                                             close_type=CloseType.EARLY_STOP, close_timestamp=1000.0)
        controller.executors_info = [terminated]
        await controller.update_processed_data()

        actions = controller.determine_executor_actions()
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(create_actions))
        self.assertEqual(Decimal("95"), create_actions[0].executor_config.start_price)

    def test_param_edit_without_active_executor_records_hash_without_stop(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        controller.executors_info = []
        grid.start_price = Decimal("95")

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(0, len(stop_actions))
        # The create path may fire immediately since no executor exists
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(create_actions))
        self.assertEqual(Decimal("95"), create_actions[0].executor_config.start_price)


class TestGridStrikeReentryThrottling(IsolatedAsyncioWrapperTestCase):
    """GEN-10."""

    def setUp(self):
        self.config = GridStrikeConfig(
            id="test-grid-strike",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            side=TradeType.BUY,
            start_price=Decimal("100"),
            end_price=Decimal("120"),
            limit_price=Decimal("90"),
            total_amount_quote=Decimal("100"),
            reentry_cooldown_seconds=60,
            max_consecutive_stopouts=3,
        )
        self.current_time = 1000.0
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(side_effect=lambda: self.current_time)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("110"))
        self.controller = GridStrike(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    def _terminated_executor(self, executor_id, close_type, close_timestamp):
        executor_config = GridExecutorConfig(**valid_grid_executor_kwargs())
        return make_grid_executor_info(executor_config, executor_id=executor_id,
                                       is_active=False, close_type=close_type,
                                       close_timestamp=close_timestamp)

    def test_creates_when_no_prior_termination(self):
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)

    def test_cooldown_blocks_immediate_reentry(self):
        # Would-have-caught: old code recreated the grid on the very next tick after
        # a termination (stop-out/re-enter loop around the limit price).
        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.STOP_LOSS, 1000.0)]
        self.current_time = 1030.0  # 30s after termination, cooldown is 60s

        actions = self.controller.determine_executor_actions()

        self.assertEqual([], actions)

    def test_reentry_allowed_after_cooldown(self):
        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.STOP_LOSS, 1000.0)]
        self.current_time = 1061.0

        actions = self.controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)

    def test_breaker_halts_after_consecutive_stopouts(self):
        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.STOP_LOSS, 100.0),
            self._terminated_executor("e2", CloseType.STOP_LOSS, 200.0),
            self._terminated_executor("e3", CloseType.STOP_LOSS, 300.0),
        ]
        self.current_time = 10_000.0  # far past the cooldown

        with patch.object(GridStrike, "logger") as logger_mock:
            actions = self.controller.determine_executor_actions()

        self.assertEqual([], actions)
        self.assertTrue(logger_mock.return_value.warning.called)
        warning_msg = logger_mock.return_value.warning.call_args[0][0]
        self.assertIn("consecutive stop-outs", warning_msg)

    def test_breaker_warning_is_rate_limited(self):
        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.STOP_LOSS, 100.0),
            self._terminated_executor("e2", CloseType.STOP_LOSS, 200.0),
            self._terminated_executor("e3", CloseType.STOP_LOSS, 300.0),
        ]
        self.current_time = 10_000.0
        with patch.object(GridStrike, "logger") as logger_mock:
            self.controller.determine_executor_actions()
            self.current_time = 10_010.0  # 10s later, inside the 30s window
            self.controller.determine_executor_actions()
        self.assertEqual(1, logger_mock.return_value.warning.call_count)

    def test_take_profit_resets_breaker(self):
        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.STOP_LOSS, 100.0),
            self._terminated_executor("e2", CloseType.STOP_LOSS, 200.0),
            self._terminated_executor("e3", CloseType.TAKE_PROFIT, 300.0),
        ]
        self.current_time = 10_000.0

        actions = self.controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)

    def test_terminations_processed_once(self):
        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.STOP_LOSS, 1000.0)]
        self.current_time = 1061.0
        self.controller.determine_executor_actions()
        self.controller.determine_executor_actions()
        # Same corpse seen on two ticks counts once
        self.assertEqual(1, self.controller._consecutive_stopouts)


class TestQuantumGridAllocatorFallbacks(IsolatedAsyncioWrapperTestCase):
    """GEN-11 + the fail-closed runtime geometry wrap."""

    def setUp(self):
        self.config = QGAConfig(
            id="test-qga",
            connector_name="kraken",
            portfolio_allocation={"SOL": Decimal("0.5")},
            grid_range=Decimal("0.002"),
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.controller = QuantumGridAllocator(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    async def test_short_candles_fall_back_to_static_range(self):
        # Would-have-caught: 1 <= rows < bb_length makes ta.bbands return None and
        # the old code raised TypeError on subscription, every tick.
        candles = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
        self.market_data_provider.get_candles_df = MagicMock(return_value=candles)

        await self.controller.update_processed_data()

        self.assertEqual(self.config.grid_range,
                         self.controller.processed_data["SOL-USDT"]["bb_width"])

    async def test_nan_bb_width_falls_back_to_static_range(self):
        candles = pd.DataFrame({"close": [100.0] * 150})
        self.market_data_provider.get_candles_df = MagicMock(return_value=candles)
        nan_bb = pd.DataFrame({
            f"BBB_{self.config.bb_length}_{self.config.bb_std_dev}": [float("nan")]})
        with patch("controllers.generic.quantum_grid_allocator.ta.bbands", return_value=nan_bb):
            await self.controller.update_processed_data()

        self.assertEqual(self.config.grid_range,
                         self.controller.processed_data["SOL-USDT"]["bb_width"])

    async def test_zero_bb_width_falls_back_to_static_range(self):
        # An exactly-zero bandwidth is not a usable grid range
        candles = pd.DataFrame({"close": [100.0] * 150})
        self.market_data_provider.get_candles_df = MagicMock(return_value=candles)
        zero_bb = pd.DataFrame({
            f"BBB_{self.config.bb_length}_{self.config.bb_std_dev}": [0.0]})
        with patch("controllers.generic.quantum_grid_allocator.ta.bbands", return_value=zero_bb):
            await self.controller.update_processed_data()

        self.assertEqual(self.config.grid_range,
                         self.controller.processed_data["SOL-USDT"]["bb_width"])

    async def test_real_bbands_column_naming_resolved_by_prefix(self):
        # pandas_ta 0.4.x names the column BBB_{len}_{std}_{std}; the hard-coded
        # BBB_{len}_{std} lookup KeyErrored whenever bbands returned a frame.
        # Constant closes yield a tiny-but-positive float-epsilon bandwidth, so the
        # dynamic value (not the fallback) must be used — proving the column was found.
        candles = pd.DataFrame({"close": [100.0] * 150})
        self.market_data_provider.get_candles_df = MagicMock(return_value=candles)

        await self.controller.update_processed_data()

        bb_width = self.controller.processed_data["SOL-USDT"]["bb_width"]
        self.assertNotEqual(self.config.grid_range, bb_width)
        self.assertTrue(bb_width.is_finite())
        self.assertGreater(bb_width, 0)

    async def test_valid_bb_width_used(self):
        candles = pd.DataFrame({"close": [100.0] * 150})
        self.market_data_provider.get_candles_df = MagicMock(return_value=candles)
        valid_bb = pd.DataFrame({
            f"BBB_{self.config.bb_length}_{self.config.bb_std_dev}": [4.0]})
        with patch("controllers.generic.quantum_grid_allocator.ta.bbands", return_value=valid_bb):
            await self.controller.update_processed_data()

        self.assertEqual(Decimal("0.04"),
                         self.controller.processed_data["SOL-USDT"]["bb_width"])

    def test_degenerate_geometry_skips_creation_without_raising(self):
        # NaN mid price → NaN start/end/limit → GridExecutorConfig validators reject;
        # the controller must skip the grid fail-closed instead of raising per tick.
        trading_rules = MagicMock()
        trading_rules.min_notional_size = Decimal("5")
        self.market_data_provider.get_trading_rules = MagicMock(return_value=trading_rules)

        with patch.object(QuantumGridAllocator, "logger") as logger_mock:
            action = self.controller.create_grid_executor(
                trading_pair="SOL-USDT",
                side=TradeType.BUY,
                start_price=Decimal("NaN"),
                end_price=Decimal("NaN"),
                grid_value=Decimal("100"),
            )

        self.assertIsNone(action)
        self.assertTrue(logger_mock.return_value.warning.called)

    def test_valid_geometry_still_creates(self):
        trading_rules = MagicMock()
        trading_rules.min_notional_size = Decimal("5")
        self.market_data_provider.get_trading_rules = MagicMock(return_value=trading_rules)

        action = self.controller.create_grid_executor(
            trading_pair="SOL-USDT",
            side=TradeType.BUY,
            start_price=Decimal("100"),
            end_price=Decimal("102"),
            grid_value=Decimal("100"),
        )

        self.assertIsNotNone(action)
        self.assertEqual(Decimal("100"), action.executor_config.start_price)
        # BUY limit sits below start by limit_price_spread
        self.assertLess(action.executor_config.limit_price, Decimal("100"))


class TestLPRebalancerClosedAmountCapture(IsolatedAsyncioWrapperTestCase):
    """GEN-17."""

    def _make_controller(self):
        config = LPRebalancerConfig(
            id="test-lp-rebalancer",
            connector_name="meteora/clmm",
            trading_pair="SOL-USDC",
            pool_address="pool123",
            total_amount_quote=Decimal("50"),
            side=1,  # BUY
        )
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=1000.0)
        market_data_provider.get_balance = MagicMock(return_value=Decimal("100"))
        controller = LPRebalancer(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        controller._pool_price = Decimal("150")
        # CDX-006: creation now requires a fresh price timestamp (mdp.time() == 1000.0)
        controller._pool_price_timestamp = 1000.0
        return controller

    def _terminated_lp_executor(self, executor_id="lp1", custom_info=None):
        executor_config = LPExecutorConfig(
            timestamp=1000.0,
            connector_name="meteora/clmm",
            trading_pair="SOL-USDC",
            pool_address="pool123",
            lower_price=Decimal("140"),
            upper_price=Decimal("160"),
            quote_amount=Decimal("50"),
            side=1,
        )
        return ExecutorInfo(
            id=executor_id,
            timestamp=1000.0,
            type="lp_executor",
            status=RunnableStatus.TERMINATED,
            config=executor_config,
            net_pnl_pct=Decimal("0"),
            net_pnl_quote=Decimal("0"),
            cum_fees_quote=Decimal("0"),
            filled_amount_quote=Decimal("0"),
            is_active=False,
            is_trading=False,
            custom_info=custom_info or {},
            close_type=CloseType.FAILED,
            close_timestamp=1000.0,
        )

    def test_failure_termination_clamps_replacement_size(self):
        # Would-have-caught: old code captured amounts only when _pending_rebalance
        # was set, so a failure-terminated executor recreated at the full configured
        # size even though the closed position returned less.
        controller = self._make_controller()
        controller._current_executor_id = "lp1"
        controller._pending_rebalance = False
        controller.executors_info = [self._terminated_lp_executor(custom_info={
            "base_amount": "0", "quote_amount": "10", "base_fee": "0", "quote_fee": "0.2"})]

        actions = controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        executor_config = actions[0].executor_config
        self.assertEqual(Decimal("10.2"), executor_config.quote_amount)

    def test_zero_amount_termination_keeps_configured_size(self):
        # An executor that never opened a position reports all-zero amounts — the
        # replacement must use the configured total, not clamp to zero.
        controller = self._make_controller()
        controller._current_executor_id = "lp1"
        controller._pending_rebalance = False
        controller.executors_info = [self._terminated_lp_executor(custom_info={
            "base_amount": "0", "quote_amount": "0", "base_fee": "0", "quote_fee": "0"})]

        actions = controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        executor_config = actions[0].executor_config
        self.assertEqual(Decimal("50"), executor_config.quote_amount)

    def test_rebalance_termination_still_clamps(self):
        # Regression: the original rebalance-initiated capture keeps working.
        controller = self._make_controller()
        controller._current_executor_id = "lp1"
        controller._pending_rebalance = True
        controller._pending_rebalance_side = 1
        controller.executors_info = [self._terminated_lp_executor(custom_info={
            "base_amount": "0", "quote_amount": "30", "base_fee": "0", "quote_fee": "1"})]

        actions = controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        executor_config = actions[0].executor_config
        self.assertEqual(Decimal("31"), executor_config.quote_amount)
