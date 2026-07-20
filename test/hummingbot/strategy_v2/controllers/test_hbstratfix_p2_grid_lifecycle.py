"""
hbstrat_fix Phase 2 tests — multi_grid_strike.py + grid_strike.py grid lifecycle.

Covers:
- CDX-003/CLA-015: duplicate grid_id rejected at config time (including disabled
  entries); runtime ownership reconciliation stops duplicate executors for one
  grid_id and unowned executors whose level_id maps to no enabled grid, so no
  executor is left trading unowned.
- CLA-M01: an empty-book NaN mid returns [] BEFORE any change-detection state
  (_last_config_hash / _grid_param_hashes / mapping) is consumed — a pending
  removed-grid stop or param edit is still applied on the next valid tick.
- CDX-M01: shared creation-only `is_updatable` fields (total_amount_quote,
  keep_position, ...) are part of the re-issue signature — an edit stops the live
  executors and the recreate carries the new values.
- CLA-002: grid_strike hashes its updatable creation-only fields (limit_price is
  the risk stop) and stops/reissues the live grid on change; recovery with an
  unknown creating signature adopts instead of blind-stopping; NaN mid is
  fail-closed.
"""
import asyncio
from decimal import Decimal
from test.hummingbot.strategy_v2.controllers.test_csf_p9_generic_grid import (
    MultiGridStrikeTestBase,
    make_grid_executor_info,
    valid_grid_executor_kwargs,
)
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from controllers.generic.grid_strike import GridStrike, GridStrikeConfig
from controllers.generic.multi_grid_strike import GridConfig, MultiGridStrikeConfig
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType


class TestMultiGridDuplicateGridIdValidator(IsolatedAsyncioWrapperTestCase):
    """CDX-003/CLA-015 — config-time uniqueness gate."""

    def _grid(self, grid_id, enabled=True, pct="0.3"):
        return GridConfig(
            grid_id=grid_id,
            start_price=Decimal("100"),
            end_price=Decimal("120"),
            limit_price=Decimal("90"),
            side=TradeType.BUY,
            amount_quote_pct=Decimal(pct),
            enabled=enabled,
        )

    def _config(self, grids):
        return MultiGridStrikeConfig(
            id="test-multi-grid",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            grids=grids,
        )

    def test_duplicate_enabled_grid_id_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self._config([self._grid("g1"), self._grid("g1")])
        self.assertIn("Duplicate grid_id 'g1'", str(ctx.exception))

    def test_duplicate_across_disabled_entry_rejected(self):
        # A disabled duplicate re-enabled later would collide at runtime, so the
        # validator must cover ALL entries, not just enabled ones.
        with self.assertRaises(ValidationError) as ctx:
            self._config([self._grid("g1"), self._grid("g1", enabled=False)])
        self.assertIn("Duplicate grid_id 'g1'", str(ctx.exception))

    def test_unique_grid_ids_accepted(self):
        config = self._config([self._grid("g1"), self._grid("g2", enabled=False)])
        self.assertEqual(["g1", "g2"], [g.grid_id for g in config.grids])


class TestMultiGridOwnershipReconciliation(MultiGridStrikeTestBase):
    """CDX-003 — runtime grid_id -> executors reconciliation."""

    def test_duplicate_executors_for_one_grid_stops_the_newer_one(self):
        # Transient duplicate creates (first-tick race) leave two live executors
        # for one grid; the older one is kept deterministically, the excess stopped.
        grid = self._grid()
        controller = self._make_controller([grid])
        older = self._executor_for_grid(controller, grid, executor_id="e1")
        newer = self._executor_for_grid(controller, grid, executor_id="e2")
        newer.timestamp = older.timestamp + 50
        controller.executors_info = [older, newer]
        controller._grid_executor_mapping = {}

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e2", stop_actions[0].executor_id)
        self.assertEqual(0, len(create_actions))
        self.assertEqual("e1", controller._grid_executor_mapping["g1"])

    def test_mapped_executor_is_kept_over_older_unmapped_one(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        older = self._executor_for_grid(controller, grid, executor_id="e1")
        newer = self._executor_for_grid(controller, grid, executor_id="e2")
        newer.timestamp = older.timestamp + 50
        controller.executors_info = [older, newer]
        controller._grid_executor_mapping = {"g1": "e2"}

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e1", stop_actions[0].executor_id)
        self.assertEqual("e2", controller._grid_executor_mapping["g1"])

    def test_unowned_executor_with_unconfigured_level_id_is_stopped(self):
        # An executor whose level_id maps to no enabled grid (removed grid whose
        # stop pass was missed, collapsed mapping, restart with stale state) must
        # not keep trading unowned.
        grid = self._grid()
        controller = self._make_controller([grid])
        owned = self._executor_for_grid(controller, grid, executor_id="e1")
        ghost_config = GridExecutorConfig(**valid_grid_executor_kwargs(level_id="ghost"))
        ghost = make_grid_executor_info(ghost_config, executor_id="e-ghost")
        controller.executors_info = [owned, ghost]
        controller._grid_executor_mapping = {"g1": "e1"}

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e-ghost", stop_actions[0].executor_id)

    def test_disabled_grid_emits_single_stop_not_duplicates(self):
        # The removed-grid pass and the reconciliation pass both see the disabled
        # grid's executor — exactly ONE stop must be emitted for it.
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, executor_id="e1")
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": "e1"}
        grid.enabled = False

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e1", stop_actions[0].executor_id)

    def test_disabled_grid_executor_stopped_even_without_pending_hash_change(self):
        # Hash already consumed (mapping was empty when the change was detected):
        # reconciliation must still stop the disabled grid's live executor.
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, executor_id="e1")
        grid.enabled = False
        controller._last_config_hash = controller._get_config_hash()  # change already consumed
        controller.executors_info = [active]
        controller._grid_executor_mapping = {}

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e1", stop_actions[0].executor_id)


class TestMultiGridNaNMidTransactionalState(MultiGridStrikeTestBase):
    """CLA-M01 — NaN mid fail-closed + transactional change-detection state."""

    def test_nan_mid_returns_empty_and_preserves_state(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        controller.executors_info = []
        hash_before = controller._last_config_hash
        param_hashes_before = dict(controller._grid_param_hashes)
        controller.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))

        actions = controller.determine_executor_actions()

        self.assertEqual([], actions)
        self.assertEqual(hash_before, controller._last_config_hash)
        self.assertEqual(param_hashes_before, controller._grid_param_hashes)

    def test_pending_grid_removal_survives_nan_tick(self):
        # Would-have-caught: the old code consumed _last_config_hash before the
        # NaN raise, so the removed grid's executor was never stopped and traded
        # unowned indefinitely.
        g1, g2 = self._grid("g1"), self._grid("g2")
        controller = self._make_controller([g1, g2])
        e1 = self._executor_for_grid(controller, g1, executor_id="e1")
        e2 = self._executor_for_grid(controller, g2, executor_id="e2")
        controller.executors_info = [e1, e2]
        controller._grid_executor_mapping = {"g1": "e1", "g2": "e2"}
        g2.enabled = False  # pending config change
        controller.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))

        self.assertEqual([], controller.determine_executor_actions())
        # The pending change must NOT have been consumed by the bad tick
        self.assertIn("g2", controller._grid_executor_mapping)

        controller.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("110"))
        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e2", stop_actions[0].executor_id)
        self.assertNotIn("g2", controller._grid_executor_mapping)

    def test_pending_param_edit_survives_nan_tick(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, executor_id="e1")
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": "e1"}
        grid.start_price = Decimal("95")  # pending param edit
        controller.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))

        self.assertEqual([], controller.determine_executor_actions())

        controller.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("110"))
        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e1", stop_actions[0].executor_id)

    def test_is_inside_bounds_nan_price_is_out_of_bounds(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        self.assertFalse(controller.is_inside_bounds(Decimal("NaN"), grid))
        self.assertFalse(controller.is_inside_bounds(None, grid))
        self.assertTrue(controller.is_inside_bounds(Decimal("110"), grid))


class TestMultiGridSharedFieldHotUpdate(MultiGridStrikeTestBase):
    """CDX-M01 — shared creation-only is_updatable fields reach the live executor."""

    async def test_total_amount_quote_edit_stops_then_recreates_with_new_amount(self):
        # Would-have-caught: total_amount_quote was excluded from every change
        # signature, so the edit reached self.config but never a live executor.
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, executor_id="e1")
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": "e1"}

        controller.config.total_amount_quote = Decimal("2000")

        actions = controller.determine_executor_actions()
        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e1", stop_actions[0].executor_id)
        self.assertEqual(0, len(create_actions))

        terminated = self._executor_for_grid(controller, grid, executor_id="e1",
                                             is_active=False, close_type=CloseType.EARLY_STOP,
                                             close_timestamp=1000.0)
        controller.executors_info = [terminated]
        await controller.update_processed_data()

        actions = controller.determine_executor_actions()
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(create_actions))
        # 2000 * 0.5 pct — derived from the edited config, not the old 1000 * 0.5
        self.assertEqual(Decimal("1000"), create_actions[0].executor_config.total_amount_quote)

    def test_keep_position_edit_stops_live_executor(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        active = self._executor_for_grid(controller, grid, executor_id="e1")
        controller.executors_info = [active]
        controller._grid_executor_mapping = {"g1": "e1"}

        controller.config.keep_position = True

        actions = controller.determine_executor_actions()
        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(1, len(stop_actions))
        self.assertEqual("e1", stop_actions[0].executor_id)

    def test_shared_field_edit_without_live_executor_creates_with_new_value(self):
        grid = self._grid()
        controller = self._make_controller([grid])
        controller.executors_info = []
        controller.config.total_amount_quote = Decimal("2000")

        actions = controller.determine_executor_actions()

        stop_actions = [a for a in actions if isinstance(a, StopExecutorAction)]
        create_actions = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(0, len(stop_actions))
        self.assertEqual(1, len(create_actions))
        self.assertEqual(Decimal("1000"), create_actions[0].executor_config.total_amount_quote)


class TestGridStrikeUpdatableFieldsApplied(IsolatedAsyncioWrapperTestCase):
    """CLA-002 — grid_strike stop/reissue on updatable creation-only field edits."""

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

    def _active_executor(self, executor_id="e1"):
        executor_config = GridExecutorConfig(**valid_grid_executor_kwargs())
        return make_grid_executor_info(executor_config, executor_id=executor_id)

    def _terminated_executor(self, executor_id, close_type, close_timestamp):
        executor_config = GridExecutorConfig(**valid_grid_executor_kwargs())
        return make_grid_executor_info(executor_config, executor_id=executor_id,
                                       is_active=False, close_type=close_type,
                                       close_timestamp=close_timestamp)

    def _prime_signature_with_create(self):
        """Issue the initial create so the controller records the signature it
        created the live executor with."""
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)
        self.controller.executors_info = [self._active_executor()]

    def test_unchanged_config_does_not_stop_live_executor(self):
        self._prime_signature_with_create()
        self.assertEqual([], self.controller.determine_executor_actions())

    def test_limit_price_edit_stops_live_executor(self):
        # Would-have-caught: limit_price (the risk stop) carried is_updatable but
        # only fed GridExecutorConfig at creation — a tightened stop never applied.
        self._prime_signature_with_create()
        self.config.limit_price = Decimal("85")

        actions = self.controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], StopExecutorAction)
        self.assertEqual("e1", actions[0].executor_id)

    def test_total_amount_quote_edit_stops_live_executor(self):
        self._prime_signature_with_create()
        self.config.total_amount_quote = Decimal("200")

        actions = self.controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], StopExecutorAction)

    def test_stop_not_resent_while_winding_down(self):
        self._prime_signature_with_create()
        self.config.limit_price = Decimal("85")
        first = self.controller.determine_executor_actions()
        self.assertEqual(1, len(first))

        # Executor still winding down on the next tick: no duplicate stop
        self.assertEqual([], self.controller.determine_executor_actions())

    def test_reissue_after_cooldown_carries_new_limit_price(self):
        self._prime_signature_with_create()
        self.config.limit_price = Decimal("85")
        self.controller.determine_executor_actions()  # stop issued

        self.controller.executors_info = [
            self._terminated_executor("e1", CloseType.EARLY_STOP, 1000.0)]
        self.current_time = 1070.0  # beyond the 60s re-entry cooldown

        actions = self.controller.determine_executor_actions()

        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)
        self.assertEqual(Decimal("85"), actions[0].executor_config.limit_price)
        # A config-edit stop (EARLY_STOP) must not count toward the stop-out breaker
        self.assertEqual(0, self.controller._consecutive_stopouts)

    def test_recovered_executor_with_unknown_signature_is_adopted_not_stopped(self):
        # Restart with a live executor: the creating signature is unknowable, so
        # the controller adopts the current config instead of blind-stopping.
        self.controller.executors_info = [self._active_executor()]
        self.assertIsNone(self.controller._active_config_signature)

        self.assertEqual([], self.controller.determine_executor_actions())
        self.assertIsNotNone(self.controller._active_config_signature)

        # ... but a subsequent edit IS detected against the adopted signature
        self.config.limit_price = Decimal("85")
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], StopExecutorAction)

    def test_nan_mid_price_skips_creation_without_raising(self):
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))
        self.assertEqual([], self.controller.determine_executor_actions())

    def test_is_inside_bounds_nan_is_out_of_bounds(self):
        self.assertFalse(self.controller.is_inside_bounds(Decimal("NaN")))
        self.assertFalse(self.controller.is_inside_bounds(None))
        self.assertTrue(self.controller.is_inside_bounds(Decimal("110")))
