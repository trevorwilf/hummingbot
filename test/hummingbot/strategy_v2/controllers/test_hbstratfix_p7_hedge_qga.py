"""
hbstrat_fix Phase 7 tests — hedge_asset.py + quantum_grid_allocator.py
(zero-balance guard theme).

Covers:
- CDX-007: config-time validator that `asset_to_hedge` matches the base of
  `hedge_trading_pair` (wrapped-token allow-list) — a mismatch would submit a
  mixed-unit gap on the wrong asset. CDX-R01: the allow-list holds only strictly
  unit-equivalent wrappers — value-accruing share tokens (wstETH, WBETH) are
  rejected.
- CLA-403 (transient-zero trigger treated as real per human decision): a
  zero/absent spot balance read while a hedge is held must NOT fire a full-size
  MARKET unwind — skip and warn. Per-order hedge step cap
  (`max_hedge_order_quote`) is ON by default (CDX-R03; explicit 0 is the unsafe
  legacy opt-out) and fails closed when the price is unusable; perp
  available-balance collateral cap bounds SELL sizing.
- CLA-411 (same decision): a zero/absent base-balance read must NOT produce
  deviation=-1 and a live BUY grid — skip and warn. CDX-R02: a first-observation
  zero is also suspect (restart history is process-local) and is trusted only
  after persisting for `zero_balance_confirmation_seconds`, so an all-quote cold
  start still bootstraps after the window.
- CLA-011: dead `hedge_ratio` removed from QGAConfig; a legacy config carrying
  it is rejected with a migration error (CDX-R04); allocations validated
  positive and finite.
- CLA-308 (enhancement, DEFAULT OFF): opt-in consecutive-stop-out breaker —
  inert at the default 0, bounds same-tick respawn when enabled.
"""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from controllers.generic.hedge_asset import HedgeAssetConfig, HedgeAssetController
from controllers.generic.quantum_grid_allocator import QGAConfig, QuantumGridAllocator
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.data_types import PositionSummary
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def make_position_summary(connector_name: str, trading_pair: str, side: TradeType,
                          amount: Decimal, breakeven_price: Decimal):
    return PositionSummary(
        connector_name=connector_name,
        trading_pair=trading_pair,
        volume_traded_quote=amount * breakeven_price,
        side=side,
        amount=amount,
        breakeven_price=breakeven_price,
        unrealized_pnl_quote=Decimal("0"),
        realized_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
    )


def make_grid_executor_info(trading_pair: str, executor_id: str, close_type,
                            close_timestamp: float, connector_name: str = "kraken",
                            is_active: bool = False, side: TradeType = TradeType.BUY):
    config = GridExecutorConfig(
        id=executor_id,
        timestamp=close_timestamp - 100.0,
        connector_name=connector_name,
        trading_pair=trading_pair,
        side=side,
        start_price=Decimal("9.5"),
        end_price=Decimal("10.5"),
        limit_price=Decimal("9.0") if side == TradeType.BUY else Decimal("11.0"),
        total_amount_quote=Decimal("100"),
        keep_position=True,
        triple_barrier_config=TripleBarrierConfig(
            take_profit=Decimal("0.001"),
            open_order_type=OrderType.LIMIT_MAKER,
            take_profit_order_type=OrderType.LIMIT_MAKER,
            stop_loss=None,
            time_limit=None,
            trailing_stop=None,
        ),
    )
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
        is_trading=False,
        custom_info={"side": side},
        close_type=close_type,
        close_timestamp=close_timestamp,
    )


class TestHedgeAssetPairValidator(IsolatedAsyncioWrapperTestCase):
    """CDX-007 — asset_to_hedge must be the base of hedge_trading_pair."""

    def _config(self, **overrides):
        params = dict(
            id="test-hedge",
            total_amount_quote=Decimal("1"),
            spot_connector_name="kraken",
            asset_to_hedge="SOL",
            hedge_connector_name="binance_perpetual",
            hedge_trading_pair="SOL-USDT",
            hedge_ratio=Decimal("1"),
        )
        params.update(overrides)
        return HedgeAssetConfig(**params)

    def test_matching_base_accepted(self):
        config = self._config()
        self.assertEqual("SOL", config.asset_to_hedge)

    def test_wrong_base_rejected(self):
        # Would-have-caught CDX-007: BTC balance + SOL-USDT pair sells SOL.
        with self.assertRaisesRegex(ValidationError, "does not match the base asset"):
            self._config(asset_to_hedge="BTC")

    def test_wrapped_alias_accepted(self):
        config = self._config(asset_to_hedge="WBTC", hedge_trading_pair="BTC-USDT")
        self.assertEqual("WBTC", config.asset_to_hedge)

    def test_wrapped_alias_with_wrong_base_rejected(self):
        with self.assertRaisesRegex(ValidationError, "does not match the base asset"):
            self._config(asset_to_hedge="WBTC", hedge_trading_pair="SOL-USDT")

    def test_share_token_aliases_rejected(self):
        # CDX-R01: wstETH/WBETH are value-accruing share tokens (1 token != 1 ETH,
        # and drifting) — a symbol-level alias would reintroduce the mixed-unit
        # mis-sized hedge the validator exists to prevent.
        for asset in ("WSTETH", "WBETH"):
            with self.assertRaisesRegex(ValidationError, "does not match the base asset"):
                self._config(asset_to_hedge=asset, hedge_trading_pair="ETH-USDT")

    def test_rebasing_unit_pegged_alias_accepted(self):
        # stETH rebases: 1 stETH == 1 ETH of stake at all times (unit-equivalent).
        config = self._config(asset_to_hedge="STETH", hedge_trading_pair="ETH-USDT")
        self.assertEqual("STETH", config.asset_to_hedge)

    def test_case_insensitive_match(self):
        config = self._config(asset_to_hedge="sol")
        self.assertEqual("sol", config.asset_to_hedge)


class HedgeAssetControllerTestBase(IsolatedAsyncioWrapperTestCase):
    HEDGE_PAIR = "SOL-USDT"

    def make_controller(self, **config_overrides):
        params = dict(
            id="test-hedge",
            total_amount_quote=Decimal("1"),
            spot_connector_name="kraken",
            asset_to_hedge="SOL",
            hedge_connector_name="binance_perpetual",
            hedge_trading_pair=self.HEDGE_PAIR,
            hedge_ratio=Decimal("1"),
            leverage=20,
            min_notional_size=10,
            cooldown_time=0.0,
        )
        params.update(config_overrides)
        self.config = HedgeAssetConfig(**params)
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("100"))
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("10"))
        self.market_data_provider.get_available_balance = MagicMock(return_value=Decimal("1000"))
        self.controller = HedgeAssetController(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return self.controller


class TestHedgeAssetZeroBalanceGuard(HedgeAssetControllerTestBase):
    """CLA-403 — a zero/absent spot read while a hedge is held must not unwind."""

    async def test_zero_spot_read_with_hedge_held_skips_unwind(self):
        # Would-have-caught CLA-403: without the guard the gap becomes
        # -hedge_position_size and a full-size BUY MARKET unwind is created.
        self.make_controller()
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("0"))
        self.controller.positions_held = [
            make_position_summary("binance_perpetual", self.HEDGE_PAIR,
                                  TradeType.SELL, Decimal("10"), Decimal("100"))]
        with patch.object(HedgeAssetController, "logger") as logger_mock:
            await self.controller.update_processed_data()
            actions = self.controller.determine_executor_actions()
        self.assertTrue(self.controller.processed_data["spot_balance_suspect"])
        self.assertEqual([], actions)
        self.assertTrue(logger_mock.return_value.warning.called)

    async def test_none_spot_read_treated_as_absent(self):
        self.make_controller()
        self.market_data_provider.get_balance = MagicMock(return_value=None)
        self.controller.positions_held = [
            make_position_summary("binance_perpetual", self.HEDGE_PAIR,
                                  TradeType.SELL, Decimal("10"), Decimal("100"))]
        await self.controller.update_processed_data()
        self.assertTrue(self.controller.processed_data["spot_balance_suspect"])
        self.assertEqual(Decimal("0"), self.controller.processed_data["spot_balance"])
        self.assertEqual([], self.controller.determine_executor_actions())

    async def test_partial_decrease_still_hedges(self):
        # Regression guard: a genuine nonzero decrease still reduces the hedge.
        self.make_controller()
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("5"))
        self.controller.positions_held = [
            make_position_summary("binance_perpetual", self.HEDGE_PAIR,
                                  TradeType.SELL, Decimal("10"), Decimal("100"))]
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(TradeType.BUY, actions[0].executor_config.side)
        self.assertEqual(Decimal("5"), actions[0].executor_config.amount)

    async def test_zero_spot_without_hedge_is_not_suspect(self):
        # Nothing held and nothing to hedge — no warning, no action.
        self.make_controller()
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("0"))
        with patch.object(HedgeAssetController, "logger") as logger_mock:
            await self.controller.update_processed_data()
            actions = self.controller.determine_executor_actions()
        self.assertFalse(self.controller.processed_data["spot_balance_suspect"])
        self.assertEqual([], actions)
        self.assertFalse(logger_mock.return_value.warning.called)


class TestHedgeAssetOrderCaps(HedgeAssetControllerTestBase):
    """CLA-403 — per-order step cap and perp collateral cap."""

    def test_cap_enabled_by_default(self):
        # CDX-R03: the per-order cap must be ON in a normal configuration.
        self.make_controller()
        self.assertEqual(Decimal("5000"), self.config.max_hedge_order_quote)

    async def test_gap_below_default_cap_not_reduced(self):
        # gap = 10 SOL @ 100 → 1000 quote, under the 5000 default cap.
        self.make_controller()
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(TradeType.SELL, actions[0].executor_config.side)
        self.assertEqual(Decimal("10"), actions[0].executor_config.amount)

    async def test_default_cap_bounds_single_order(self):
        # CDX-R03: a huge gap is chased in capped steps, never one full-size
        # MARKET order. gap = 100 SOL @ 100 → 10000 quote; default 5000 → 50 SOL.
        self.make_controller()
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("100"))
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("50"), actions[0].executor_config.amount)

    async def test_explicit_zero_disables_cap(self):
        # Unsafe legacy opt-out: an explicit 0 restores full-gap sizing.
        self.make_controller(max_hedge_order_quote=Decimal("0"))
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("100"))
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("100"), actions[0].executor_config.amount)

    def test_cap_with_unusable_price_fails_closed(self):
        # CDX-R03: if the price is unusable the cap cannot be converted to base
        # units — skip and warn rather than submit an uncapped MARKET order.
        self.make_controller()
        self.controller.processed_data = {
            "spot_balance_suspect": False,
            "cool_down_time_condition": True,
            "min_notional_size_condition": True,
            "hedge_position_gap": Decimal("10"),
            "current_price": Decimal("NaN"),
            "perp_available_balance": Decimal("1000"),
        }
        with patch.object(HedgeAssetController, "logger") as logger_mock:
            actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertTrue(logger_mock.return_value.warning.called)

    async def test_max_hedge_order_quote_caps_amount(self):
        # gap = 10 SOL @ 100 → 1000 quote; cap 500 quote → 5 SOL per order.
        self.make_controller(max_hedge_order_quote=Decimal("500"))
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("5"), actions[0].executor_config.amount)

    async def test_sell_capped_by_perp_collateral(self):
        # available 25 * leverage 20 / price 100 = 5 SOL max sellable.
        self.make_controller()
        self.market_data_provider.get_available_balance = MagicMock(return_value=Decimal("25"))
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(TradeType.SELL, actions[0].executor_config.side)
        self.assertEqual(Decimal("5"), actions[0].executor_config.amount)

    async def test_sell_skipped_when_no_collateral_read(self):
        self.make_controller()
        self.market_data_provider.get_available_balance = MagicMock(return_value=Decimal("0"))
        with patch.object(HedgeAssetController, "logger") as logger_mock:
            await self.controller.update_processed_data()
            actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertTrue(logger_mock.return_value.warning.called)

    async def test_buy_close_not_collateral_gated(self):
        # Reducing the short needs no fresh collateral — a zero available-balance
        # read must not block the risk-reducing direction.
        self.make_controller()
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("5"))
        self.market_data_provider.get_available_balance = MagicMock(return_value=Decimal("0"))
        self.controller.positions_held = [
            make_position_summary("binance_perpetual", self.HEDGE_PAIR,
                                  TradeType.SELL, Decimal("10"), Decimal("100"))]
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(TradeType.BUY, actions[0].executor_config.side)
        self.assertEqual(Decimal("5"), actions[0].executor_config.amount)


class TestQGAConfigCLA011(IsolatedAsyncioWrapperTestCase):
    """CLA-011 — dead hedge_ratio removed; allocations validated positive."""

    def test_hedge_ratio_field_removed(self):
        self.assertNotIn("hedge_ratio", QGAConfig.model_fields)
        config = QGAConfig(id="test-qga")
        self.assertFalse(hasattr(config, "hedge_ratio"))

    def test_legacy_config_with_hedge_ratio_rejected_with_migration_error(self):
        # CDX-R04: the removed knob must fail loudly with a migration hint, not
        # be silently discarded (which would perpetuate the original no-op).
        with self.assertRaisesRegex(ValidationError, "hedge_ratio.*was removed"):
            QGAConfig(id="test-qga", hedge_ratio=Decimal("2"))

    def test_zero_allocation_rejected(self):
        with self.assertRaisesRegex(ValidationError, "positive finite"):
            QGAConfig(id="test-qga", portfolio_allocation={"SOL": Decimal("0")})

    def test_negative_allocation_rejected(self):
        with self.assertRaisesRegex(ValidationError, "positive finite"):
            QGAConfig(id="test-qga", portfolio_allocation={"SOL": Decimal("-0.1")})

    def test_nan_allocation_rejected(self):
        # Pydantic's Decimal schema (allow_inf_nan=False) rejects NaN before the
        # field validator runs; either layer failing keeps the config fail-closed.
        with self.assertRaisesRegex(ValidationError, "finite"):
            QGAConfig(id="test-qga", portfolio_allocation={"SOL": Decimal("NaN")})

    def test_valid_allocation_accepted(self):
        config = QGAConfig(id="test-qga", portfolio_allocation={"SOL": Decimal("0.5")})
        self.assertEqual(Decimal("0.5"), config.portfolio_allocation["SOL"])


class QGAControllerTestBase(IsolatedAsyncioWrapperTestCase):
    def make_controller(self, **config_overrides):
        params = dict(
            id="test-qga",
            connector_name="kraken",
            portfolio_allocation={"SOL": Decimal("0.5")},
        )
        params.update(config_overrides)
        self.config = QGAConfig(**params)
        self.balances = {"USDT": Decimal("1000"), "SOL": Decimal("10")}
        self.current_time = 1000.0
        trading_rules = MagicMock()
        trading_rules.min_notional_size = Decimal("5")
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(side_effect=lambda: self.current_time)
        self.market_data_provider.get_balance = MagicMock(
            side_effect=lambda connector, asset: self.balances.get(asset, Decimal("0")))
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("10"))
        self.market_data_provider.get_trading_rules = MagicMock(return_value=trading_rules)
        self.controller = QuantumGridAllocator(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return self.controller


class TestQGAZeroBalanceGuard(QGAControllerTestBase):
    """CLA-411 — transient zero base-balance read must not spawn a grid."""

    def test_cold_start_first_zero_read_is_suspect(self):
        # CDX-R02: the in-process nonzero history does not survive a restart, so a
        # first-observation zero (exactly what a transient post-restart read looks
        # like) must fail closed — no BUY grid from a single unconfirmed read.
        self.make_controller()
        self.balances["SOL"] = Decimal("0")
        with patch.object(QuantumGridAllocator, "logger") as logger_mock:
            actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertIn("SOL", self.controller._suspect_balance_assets)
        self.assertTrue(logger_mock.return_value.warning.called)

    def test_cold_start_zero_confirmed_after_window_allows_grid(self):
        # Regression guard: a genuinely flat all-quote start must still bootstrap —
        # the zero becomes trusted once it persists for the confirmation window.
        self.make_controller()
        self.balances["SOL"] = Decimal("0")
        self.assertEqual([], self.controller.determine_executor_actions())
        self.current_time = 1060.0  # default window (60s) served
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)
        self.assertEqual(TradeType.BUY, actions[0].executor_config.side)
        self.assertEqual("SOL-USDT", actions[0].executor_config.trading_pair)

    def test_seen_nonzero_zero_stays_suspect_beyond_window(self):
        # Once funded in-process, a zero read is suspect regardless of how long it
        # persists — the confirmation window applies only to never-funded assets.
        self.make_controller()
        self.controller.determine_executor_actions()  # funded tick: SOL=10
        self.balances["SOL"] = Decimal("0")
        self.current_time = 1200.0  # far beyond the 60s window
        self.assertEqual([], self.controller.determine_executor_actions())
        self.assertIn("SOL", self.controller._suspect_balance_assets)

    def test_invalid_read_restarts_zero_confirmation(self):
        # An invalid read says nothing about flatness — it must restart the
        # confirmation window rather than count toward trusting the zero.
        self.make_controller()
        self.balances["SOL"] = Decimal("0")
        self.assertEqual([], self.controller.determine_executor_actions())  # window starts @1000
        self.current_time = 1030.0
        self.balances["SOL"] = Decimal("NaN")
        self.assertEqual([], self.controller.determine_executor_actions())  # invalid: restart
        self.current_time = 1120.0
        self.balances["SOL"] = Decimal("0")
        # 120s after the first zero, but the window restarted at 1120 — still suspect.
        self.assertEqual([], self.controller.determine_executor_actions())
        self.assertIn("SOL", self.controller._suspect_balance_assets)

    def test_zero_confirmation_optout_restores_immediate_bootstrap(self):
        # Explicit unsafe opt-out: window 0 trusts a first-read zero immediately.
        self.make_controller(zero_balance_confirmation_seconds=0.0)
        self.balances["SOL"] = Decimal("0")
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(TradeType.BUY, actions[0].executor_config.side)

    def test_transient_zero_after_nonzero_blocks_creation(self):
        # Would-have-caught CLA-411: without the guard the zero read makes
        # deviation=-1 and a real BUY grid is created on this tick.
        self.make_controller()
        first_actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(first_actions))  # funded tick: SOL seen nonzero
        self.balances["SOL"] = Decimal("0")
        with patch.object(QuantumGridAllocator, "logger") as logger_mock:
            actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertIn("SOL", self.controller._suspect_balance_assets)
        self.assertTrue(logger_mock.return_value.warning.called)

    def test_recovered_read_resumes_creation(self):
        self.make_controller()
        self.controller.determine_executor_actions()
        self.balances["SOL"] = Decimal("0")
        self.assertEqual([], self.controller.determine_executor_actions())
        self.balances["SOL"] = Decimal("10")
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(set(), self.controller._suspect_balance_assets)

    def test_nan_balance_read_blocks_creation(self):
        # A non-finite read is never trustworthy, even on a cold start.
        self.make_controller()
        self.balances["SOL"] = Decimal("NaN")
        actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertIn("SOL", self.controller._suspect_balance_assets)

    def test_transient_zero_quote_read_blocks_creation(self):
        # A suspect quote read skews total portfolio value and therefore every
        # asset's theoretical allocation — the whole tick is unreliable.
        self.make_controller()
        self.controller.determine_executor_actions()
        self.balances["USDT"] = Decimal("0")
        actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertIn("USDT", self.controller._suspect_balance_assets)


class TestQGAStopOutBreaker(QGAControllerTestBase):
    """CLA-308 — opt-in consecutive-stop-out breaker, default off."""

    def _stopout_corpses(self, close_types_and_ts):
        return [
            make_grid_executor_info("SOL-USDT", f"grid-{i}", close_type, ts)
            for i, (close_type, ts) in enumerate(close_types_and_ts)
        ]

    def test_breaker_disabled_by_default(self):
        self.assertEqual(0, QGAConfig(id="d").stop_out_breaker_count)

    def test_default_config_ignores_consecutive_stopouts(self):
        # Default-off contract: existing recentering behavior is unchanged even
        # after many consecutive stop-outs.
        self.make_controller()
        self.controller.executors_info = self._stopout_corpses(
            [(CloseType.POSITION_HOLD, 900.0 + i * 10) for i in range(5)])
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)

    def test_breaker_blocks_after_n_consecutive_stopouts(self):
        self.make_controller(stop_out_breaker_count=2, stop_out_breaker_cooldown=300.0)
        self.controller.executors_info = self._stopout_corpses(
            [(CloseType.POSITION_HOLD, 900.0), (CloseType.POSITION_HOLD, 950.0)])
        with patch.object(QuantumGridAllocator, "logger") as logger_mock:
            actions = self.controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertEqual(2, self.controller._stopout_streaks["SOL-USDT"])
        self.assertTrue(logger_mock.return_value.warning.called)

    def test_stop_loss_close_type_also_counts(self):
        self.make_controller(stop_out_breaker_count=2, stop_out_breaker_cooldown=300.0)
        self.controller.executors_info = self._stopout_corpses(
            [(CloseType.STOP_LOSS, 900.0), (CloseType.STOP_LOSS, 950.0)])
        self.assertEqual([], self.controller.determine_executor_actions())

    def test_breaker_reopens_after_cooldown(self):
        self.make_controller(stop_out_breaker_count=2, stop_out_breaker_cooldown=300.0)
        self.controller.executors_info = self._stopout_corpses(
            [(CloseType.POSITION_HOLD, 900.0), (CloseType.POSITION_HOLD, 950.0)])
        self.assertEqual([], self.controller.determine_executor_actions())
        self.current_time = 1300.0  # 950 + 300 cooldown served
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(0, self.controller._stopout_streaks["SOL-USDT"])

    def test_take_profit_resets_streak(self):
        self.make_controller(stop_out_breaker_count=2, stop_out_breaker_cooldown=300.0)
        self.controller.executors_info = self._stopout_corpses(
            [(CloseType.POSITION_HOLD, 900.0), (CloseType.TAKE_PROFIT, 920.0),
             (CloseType.POSITION_HOLD, 950.0)])
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(1, self.controller._stopout_streaks["SOL-USDT"])

    def test_corpses_counted_once_across_ticks(self):
        self.make_controller(stop_out_breaker_count=5, stop_out_breaker_cooldown=300.0)
        self.controller.executors_info = self._stopout_corpses(
            [(CloseType.POSITION_HOLD, 900.0), (CloseType.POSITION_HOLD, 950.0)])
        self.controller.determine_executor_actions()
        self.controller.determine_executor_actions()
        self.assertEqual(2, self.controller._stopout_streaks["SOL-USDT"])
