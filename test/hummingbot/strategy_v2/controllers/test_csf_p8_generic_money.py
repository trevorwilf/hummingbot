"""
CSF-V1 Phase 8 tests — generic controllers: money paths.

Covers:
- GEN-1: stat_arb signal-unavailable paths return (None, None); update_processed_data
  completes with safe defaults; the global TP/SL evaluation still runs during a
  candles outage (would-have-caught: old code raised TypeError on unpack per tick).
- GEN-2: stat_arb reduce actions deduct in-flight close order-executor amounts
  (no duplicate full-size market closes); stop filter only targets active executors.
- GEN-3: pmm_mister never fabricates a Decimal("100") reference price — skips the
  cycle on price-unavailable, reuses the previous price only within a max age, and
  stops creating orders beyond it.
- GEN-4: pmm_mister global take-profit / stop-loss enforced against positions_held
  with an in-flight close guard.
- GEN-5/7/15: xemm_multiple_levels profitability-floor validation, per-level
  replenishment (filter instead of boolean-map), provider time for sell configs,
  clamped executor min_profitability, NaN-mid guard.
- GEN-12: arbitrage_controller NaN/zero rate rejected, zero quantized amount
  rejected, FAILED executors excluded from the imbalance, imbalance survives
  archival of the executors_info buffer.
- GEN-13: hedge_asset deducts in-flight hedge order-executor amounts from the gap
  (no double-hedge inside the fill-settle window); spot reference pair configurable.
"""
import asyncio
import math
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
from pydantic import ValidationError

from controllers.generic.arbitrage_controller import ArbitrageController, ArbitrageControllerConfig
from controllers.generic.hedge_asset import HedgeAssetConfig, HedgeAssetController
from controllers.generic.pmm_mister import PMMister, PMMisterConfig
from controllers.generic.stat_arb import StatArb, StatArbConfig
from controllers.generic.xemm_multiple_levels import XEMMMultipleLevels, XEMMMultipleLevelsConfig
from hummingbot.core.data_type.common import MarketDict, PositionAction, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.arbitrage_executor.data_types import ArbitrageExecutorConfig
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def make_executor_info(config, executor_type: str, executor_id: str = "e1", timestamp: float = 1000.0,
                       is_active: bool = True, is_trading: bool = False, custom_info=None,
                       close_type=None, close_timestamp=None, filled_amount_quote=Decimal("0")):
    return ExecutorInfo(
        id=executor_id,
        timestamp=timestamp,
        type=executor_type,
        status=RunnableStatus.RUNNING if is_active else RunnableStatus.TERMINATED,
        config=config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=filled_amount_quote,
        is_active=is_active,
        is_trading=is_trading,
        custom_info=custom_info or {},
        close_type=close_type,
        close_timestamp=close_timestamp,
    )


def make_order_executor_info(connector_name: str, trading_pair: str, side: TradeType,
                             amount: Decimal, executor_id: str = "oe1", is_active: bool = True,
                             timestamp: float = 1000.0):
    config = OrderExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name=connector_name,
        trading_pair=trading_pair,
        side=side,
        amount=amount,
        position_action=PositionAction.CLOSE,
        execution_strategy=ExecutionStrategy.MARKET,
    )
    return make_executor_info(config, "order_executor", executor_id=executor_id,
                              timestamp=timestamp, is_active=is_active,
                              custom_info={"side": side})


def make_position_executor_info(connector_name: str, trading_pair: str, side: TradeType,
                                executor_id: str = "pe1", is_active: bool = True,
                                is_trading: bool = False, timestamp: float = 1000.0,
                                level_id: str = "buy_0"):
    config = PositionExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name=connector_name,
        trading_pair=trading_pair,
        side=side,
        entry_price=Decimal("100"),
        amount=Decimal("1"),
    )
    return make_executor_info(config, "position_executor", executor_id=executor_id,
                              timestamp=timestamp, is_active=is_active, is_trading=is_trading,
                              custom_info={"side": side, "level_id": level_id})


def make_position_summary(connector_name: str, trading_pair: str, side: TradeType,
                          amount: Decimal, breakeven_price: Decimal,
                          unrealized_pnl_quote: Decimal = Decimal("0")):
    return PositionSummary(
        connector_name=connector_name,
        trading_pair=trading_pair,
        volume_traded_quote=amount * breakeven_price,
        side=side,
        amount=amount,
        breakeven_price=breakeven_price,
        unrealized_pnl_quote=unrealized_pnl_quote,
        realized_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
    )


class TestStatArbGlobalRiskAndInFlightGuard(IsolatedAsyncioWrapperTestCase):
    """GEN-1 / GEN-2."""

    DOMINANT = ConnectorPair(connector_name="kraken", trading_pair="SOL-USDT")
    HEDGE = ConnectorPair(connector_name="kraken", trading_pair="ADA-USDT")

    def setUp(self):
        self.config = StatArbConfig(
            id="test-stat-arb",
            total_amount_quote=Decimal("200"),
            connector_pair_dominant=self.DOMINANT,
            connector_pair_hedge=self.HEDGE,
            tp_global=Decimal("0.01"),
            sl_global=Decimal("0.05"),
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("100"))
        self.market_data_provider.get_candles_df = MagicMock(return_value=pd.DataFrame())
        self.controller = StatArb(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    def _losing_position(self, amount=Decimal("1")):
        # breakeven 100, amount 1 → amount_quote 100; unrealized −20 → pnl −20% < −5%
        return make_position_summary(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                     TradeType.BUY, amount, Decimal("100"), Decimal("-20"))

    def test_spread_and_z_score_return_two_none_on_empty_candles(self):
        # Would-have-caught GEN-1: old code returned bare None → TypeError on unpack.
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNone(spread)
        self.assertIsNone(z_score)

    async def test_candles_outage_still_evaluates_global_stop_loss(self):
        self.controller.positions_held = [self._losing_position()]
        await self.controller.update_processed_data()  # old code raised TypeError here
        self.assertEqual(0, self.controller.processed_data["signal"])
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        cfg = creates[0].executor_config
        self.assertEqual("order_executor", cfg.type)
        self.assertEqual(TradeType.SELL, cfg.side)
        self.assertEqual(Decimal("1"), cfg.amount)
        self.assertEqual(PositionAction.CLOSE, cfg.position_action)

    def _distinct_aligned_frames(self, n: int):
        # Two DISTINCT nondegenerate series on the same timestamp grid, so nothing
        # (zero spread std, shared frame) can mask the aligned-row threshold check.
        timestamps = [i * 60.0 for i in range(n)]
        dom = pd.DataFrame({"timestamp": timestamps,
                            "close": [100.0 * (1 + 0.001 * math.sin(i)) for i in range(n)]})
        hedge = pd.DataFrame({"timestamp": timestamps,
                              "close": [50.0 * (1 + 0.001 * math.cos(i / 3)) for i in range(n)]})

        def by_pair(**kwargs):
            return dom if kwargs["trading_pair"] == "SOL-USDT" else hedge
        self.market_data_provider.get_candles_df = MagicMock(side_effect=by_pair)
        self.market_data_provider.time = MagicMock(return_value=n * 60.0 + 120.0)

    async def test_short_lookback_returns_none_pair(self):
        # lookback_period - 1 = 299 aligned rows < 300 → second early exit at the
        # exact configured boundary (CDX-R05: a hard-coded lower minimum like 10
        # would let 11–299 rows through and this test would catch it).
        self.assertEqual(300, self.config.lookback_period)
        self._distinct_aligned_frames(self.config.lookback_period - 1)
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNone(spread)
        self.assertIsNone(z_score)

    async def test_lookback_boundary_exact_rows_produce_signal(self):
        # Exactly lookback_period aligned rows → the threshold is the configured
        # value, not a dead strategy: a finite spread/z-score is produced.
        self._distinct_aligned_frames(self.config.lookback_period)
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNotNone(spread)
        self.assertIsNotNone(z_score)
        self.assertTrue(math.isfinite(float(z_score)))

    def test_reduce_suppressed_while_close_executor_active(self):
        # Would-have-caught GEN-2: full-size close re-emitted every tick.
        position = make_position_summary(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                         TradeType.BUY, Decimal("5"), Decimal("100"))
        self.controller.executors_info = [
            make_order_executor_info(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                     TradeType.SELL, Decimal("5"))
        ]
        self.assertEqual([], self.controller.get_executors_to_reduce_position(position))

    def test_reduce_emits_only_remaining_amount(self):
        position = make_position_summary(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                         TradeType.BUY, Decimal("5"), Decimal("100"))
        self.controller.executors_info = [
            make_order_executor_info(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                     TradeType.SELL, Decimal("3"))
        ]
        actions = self.controller.get_executors_to_reduce_position(position)
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("2"), actions[0].executor_config.amount)

    def test_terminated_close_executor_does_not_suppress_reduce(self):
        position = make_position_summary(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                         TradeType.BUY, Decimal("5"), Decimal("100"))
        self.controller.executors_info = [
            make_order_executor_info(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                     TradeType.SELL, Decimal("5"), is_active=False)
        ]
        actions = self.controller.get_executors_to_reduce_position(position)
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("5"), actions[0].executor_config.amount)

    def test_opposite_signal_stop_filter_skips_terminated_executors(self):
        # signal 1 → dominant executors on the SELL side get stopped — but only active ones.
        self.controller.processed_data["signal"] = 1
        active = make_position_executor_info(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                             TradeType.SELL, executor_id="active-sell")
        terminated = make_position_executor_info(self.DOMINANT.connector_name, self.DOMINANT.trading_pair,
                                                 TradeType.SELL, executor_id="dead-sell", is_active=False)
        self.controller.executors_info = [active, terminated]
        actions = self.controller.get_executors_to_reduce_position_on_opposite_signal()
        stops = [a for a in actions if isinstance(a, StopExecutorAction)]
        self.assertEqual(["active-sell"], [a.executor_id for a in stops])


class TestPMMisterPriceHandling(IsolatedAsyncioWrapperTestCase):
    """GEN-3."""

    def setUp(self):
        self.config = PMMisterConfig(
            id="test-pmm-mister",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            total_amount_quote=Decimal("1000"),
            reference_price_max_age=60,
            # pass explicitly — pydantic v2 does not run "before" validators on defaults,
            # so string defaults would reach the controller unparsed
            buy_spreads="0.0005",
            sell_spreads="0.0005",
            buy_amounts_pct="1",
            sell_amounts_pct="1",
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.market_data_provider.quantize_order_amount = MagicMock(
            side_effect=lambda connector, pair, amount: amount)
        self.controller = PMMister(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    def _set_price(self, price):
        self.market_data_provider.get_price_by_type = MagicMock(return_value=price)

    def _set_time(self, now: float):
        self.market_data_provider.time = MagicMock(return_value=now)

    async def test_no_fabricated_ladder_when_price_never_available(self):
        # Would-have-caught GEN-3: old code quoted a full ladder around literal 100.
        self._set_price(None)
        await self.controller.update_processed_data()
        self.assertNotIn("reference_price", self.controller.processed_data)
        self.assertEqual([], self.controller.determine_executor_actions())

    async def test_exception_on_price_fetch_skips_cycle(self):
        self.market_data_provider.get_price_by_type = MagicMock(side_effect=RuntimeError("no book"))
        await self.controller.update_processed_data()
        self.assertEqual([], self.controller.determine_executor_actions())

    async def test_nan_price_skips_cycle(self):
        self._set_price(Decimal("NaN"))
        await self.controller.update_processed_data()
        self.assertEqual([], self.controller.determine_executor_actions())

    async def test_previous_price_reused_within_max_age(self):
        self._set_price(Decimal("200"))
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("200"), self.controller.processed_data["reference_price"])

        self._set_time(1030.0)  # 30s later, inside the 60s max age
        self._set_price(None)
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("200"), self.controller.processed_data["reference_price"])
        self.assertFalse(self.controller.processed_data["price_expired"])
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertGreater(len(creates), 0)
        for action in creates:
            # Prices derive from the real previous price, never from a fabricated 100
            self.assertAlmostEqual(200.0, float(action.executor_config.entry_price), delta=1.0)

    async def test_creates_stop_when_price_older_than_max_age(self):
        self._set_price(Decimal("200"))
        await self.controller.update_processed_data()

        self._set_time(1100.0)  # 100s later, beyond the 60s max age
        self._set_price(None)
        await self.controller.update_processed_data()
        self.assertTrue(self.controller.processed_data["price_expired"])
        actions = self.controller.determine_executor_actions()
        self.assertEqual([], [a for a in actions if isinstance(a, CreateExecutorAction)])

    async def test_fresh_price_recovers_after_expiry(self):
        self._set_price(Decimal("200"))
        await self.controller.update_processed_data()
        self._set_time(1100.0)
        self._set_price(None)
        await self.controller.update_processed_data()
        self.assertTrue(self.controller.processed_data["price_expired"])

        self._set_time(1110.0)
        self._set_price(Decimal("210"))
        await self.controller.update_processed_data()
        self.assertFalse(self.controller.processed_data["price_expired"])
        self.assertEqual(Decimal("210"), self.controller.processed_data["reference_price"])


class TestPMMisterGlobalTpSl(IsolatedAsyncioWrapperTestCase):
    """GEN-4."""

    def setUp(self):
        self.config = PMMisterConfig(
            id="test-pmm-mister-tpsl",
            connector_name="kraken",
            trading_pair="SOL-USDT",
            total_amount_quote=Decimal("1000"),
            global_take_profit=Decimal("0.03"),
            global_stop_loss=Decimal("0.05"),
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.controller = PMMister(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        self.controller.processed_data = {"reference_price": Decimal("100"), "price_expired": False}

    def _position(self, unrealized: Decimal):
        # amount 1 @ breakeven 100 → amount_quote 100 → pnl_pct = unrealized / 100
        return make_position_summary("kraken", "SOL-USDT", TradeType.BUY,
                                     Decimal("1"), Decimal("100"), unrealized)

    def test_stop_loss_closes_position_and_cancels_executors(self):
        # Would-have-caught GEN-4: old code never enforced the displayed stop loss.
        self.controller.positions_held = [self._position(Decimal("-6"))]  # −6% ≤ −5%
        open_order = make_position_executor_info("kraken", "SOL-USDT", TradeType.BUY,
                                                 executor_id="maker-1")
        self.controller.executors_info = [open_order]
        actions = self.controller.determine_executor_actions()
        stops = [a for a in actions if isinstance(a, StopExecutorAction)]
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(["maker-1"], [a.executor_id for a in stops])
        self.assertTrue(all(a.keep_position for a in stops))
        self.assertEqual(1, len(creates))
        cfg = creates[0].executor_config
        self.assertEqual("order_executor", cfg.type)
        self.assertEqual(TradeType.SELL, cfg.side)
        self.assertEqual(Decimal("1"), cfg.amount)
        self.assertEqual(PositionAction.CLOSE, cfg.position_action)
        self.assertEqual(ExecutionStrategy.MARKET, cfg.execution_strategy)

    def test_take_profit_closes_position(self):
        self.controller.positions_held = [self._position(Decimal("4"))]  # +4% ≥ +3%
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        self.assertEqual(TradeType.SELL, creates[0].executor_config.side)

    def test_no_trigger_inside_barriers(self):
        self.controller.positions_held = [self._position(Decimal("-1"))]  # −1%
        self.assertIsNone(self.controller.global_tp_sl_actions())

    def test_in_flight_close_suppresses_duplicate_exit(self):
        self.controller.positions_held = [self._position(Decimal("-6"))]
        in_flight = make_order_executor_info("kraken", "SOL-USDT", TradeType.SELL, Decimal("1"),
                                             executor_id="closer-1")
        self.controller.executors_info = [in_flight]
        actions = self.controller.determine_executor_actions()
        # No new close order, and the in-flight closer must NOT be stopped
        self.assertEqual([], [a for a in actions if isinstance(a, CreateExecutorAction)])
        self.assertEqual([], [a for a in actions if isinstance(a, StopExecutorAction)])

    def test_partial_in_flight_close_emits_remainder(self):
        self.controller.positions_held = [
            make_position_summary("kraken", "SOL-USDT", TradeType.BUY,
                                  Decimal("4"), Decimal("100"), Decimal("-24"))]
        in_flight = make_order_executor_info("kraken", "SOL-USDT", TradeType.SELL, Decimal("1"))
        self.controller.executors_info = [in_flight]
        actions = self.controller.global_tp_sl_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        self.assertEqual(Decimal("3"), creates[0].executor_config.amount)


class TestXEMMMultipleLevels(IsolatedAsyncioWrapperTestCase):
    """GEN-5 / GEN-7 / GEN-15."""

    def _config_kwargs(self, **overrides):
        kwargs = dict(
            id="test-xemm",
            total_amount_quote=Decimal("120"),
            maker_connector="nonkyc",
            maker_trading_pair="PEPE-USDT",
            taker_connector="binance",
            taker_trading_pair="PEPE-USDT",
            buy_levels_targets_amount="0.003,10-0.006,20",
            sell_levels_targets_amount="0.003,10-0.006,20",
            min_profitability=Decimal("0.001"),
            max_profitability=Decimal("0.01"),
        )
        kwargs.update(overrides)
        return kwargs

    def _make_controller(self, config):
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1234.0)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("2"))
        with patch.object(ConnectorPair, "is_amm_connector", return_value=False):
            return XEMMMultipleLevels(
                config=config,
                market_data_provider=self.market_data_provider,
                actions_queue=AsyncMock(spec=asyncio.Queue),
            )

    def _make_xemm_executor(self, target_profitability: Decimal, maker_side: TradeType,
                            executor_id: str = "x1", is_active: bool = True,
                            filled_amount_quote=Decimal("0"), close_type=None):
        config = XEMMExecutorConfig(
            id=executor_id,
            timestamp=1000.0,
            buying_market=ConnectorPair(connector_name="nonkyc", trading_pair="PEPE-USDT"),
            selling_market=ConnectorPair(connector_name="binance", trading_pair="PEPE-USDT"),
            maker_side=maker_side,
            order_amount=Decimal("10"),
            min_profitability=Decimal("0.002"),
            target_profitability=target_profitability,
            max_profitability=Decimal("0.01"),
        )
        return make_executor_info(config, "xemm_executor", executor_id=executor_id,
                                  is_active=is_active, custom_info={"side": maker_side},
                                  close_type=close_type, filled_amount_quote=filled_amount_quote)

    def test_default_config_zero_floor_rejected(self):
        # Would-have-caught GEN-5: target 0.003 − min_profitability 0.003 = 0% floor.
        with self.assertRaises(ValidationError):
            XEMMMultipleLevelsConfig(**self._config_kwargs(min_profitability=Decimal("0.003")))

    def test_negative_floor_rejected(self):
        with self.assertRaises(ValidationError):
            XEMMMultipleLevelsConfig(**self._config_kwargs(min_profitability=Decimal("0.01")))

    def test_zero_amount_level_rejected(self):
        with self.assertRaises(ValidationError):
            XEMMMultipleLevelsConfig(**self._config_kwargs(
                buy_levels_targets_amount="0.003,0-0.006,0"))

    def test_valid_config_accepted(self):
        config = XEMMMultipleLevelsConfig(**self._config_kwargs())
        self.assertEqual(2, len(config.buy_levels_targets_amount))

    def test_levels_replenish_independently(self):
        # Would-have-caught GEN-7: boolean-map meant ANY active buy executor
        # suppressed EVERY buy level.
        config = XEMMMultipleLevelsConfig(**self._config_kwargs())
        controller = self._make_controller(config)
        controller.executors_info = [
            self._make_xemm_executor(Decimal("0.003"), TradeType.BUY, executor_id="buy-l0"),
        ]
        actions = controller.determine_executor_actions()
        buy_targets = [a.executor_config.target_profitability for a in actions
                       if a.executor_config.maker_side == TradeType.BUY]
        self.assertEqual([Decimal("0.006")], buy_targets)
        sell_targets = sorted(a.executor_config.target_profitability for a in actions
                              if a.executor_config.maker_side == TradeType.SELL)
        self.assertEqual([Decimal("0.003"), Decimal("0.006")], sell_targets)

    def test_sell_config_uses_provider_time(self):
        # Would-have-caught GEN-15: sell configs were stamped with time.time().
        config = XEMMMultipleLevelsConfig(**self._config_kwargs())
        controller = self._make_controller(config)
        actions = controller.determine_executor_actions()
        self.assertGreater(len(actions), 0)
        for action in actions:
            self.assertEqual(1234.0, action.executor_config.timestamp)

    def test_min_profitability_positive_and_clamped(self):
        config = XEMMMultipleLevelsConfig(**self._config_kwargs())
        controller = self._make_controller(config)
        actions = controller.determine_executor_actions()
        for action in actions:
            expected = action.executor_config.target_profitability - Decimal("0.001")
            self.assertEqual(expected, action.executor_config.min_profitability)
            self.assertGreater(action.executor_config.min_profitability, Decimal("0"))
        # A zero floor sneaking past validation (validate_assignment would normally
        # reject it — bypass pydantic to simulate) is clamped at creation time
        object.__setattr__(controller.config, "min_profitability", Decimal("0.003"))
        actions = controller.determine_executor_actions()
        level_one = [a for a in actions
                     if a.executor_config.target_profitability == Decimal("0.003")]
        self.assertGreater(len(level_one), 0)
        for action in level_one:
            self.assertEqual(XEMMMultipleLevels.MIN_PROFITABILITY_FLOOR,
                             action.executor_config.min_profitability)

    def test_invalid_mid_price_skips_creation(self):
        config = XEMMMultipleLevelsConfig(**self._config_kwargs())
        controller = self._make_controller(config)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))
        self.assertEqual([], controller.determine_executor_actions())
        self.market_data_provider.get_price_by_type = MagicMock(return_value=None)
        self.assertEqual([], controller.determine_executor_actions())


class TestArbitrageController(IsolatedAsyncioWrapperTestCase):
    """GEN-12."""

    PAIR_1 = ConnectorPair(connector_name="kraken", trading_pair="SOL-USDT")
    PAIR_2 = ConnectorPair(connector_name="binance", trading_pair="SOL-USDT")

    def setUp(self):
        self.config = ArbitrageControllerConfig(
            id="test-arb",
            total_amount_quote=Decimal("100"),
            exchange_pair_1=self.PAIR_1,
            exchange_pair_2=self.PAIR_2,
            rate_connector="kraken",
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        with patch.object(ConnectorPair, "is_amm_connector", return_value=False):
            self.controller = ArbitrageController(
                config=self.config,
                market_data_provider=self.market_data_provider,
                actions_queue=AsyncMock(spec=asyncio.Queue),
            )

    def _create_action(self):
        with patch.object(ConnectorPair, "is_amm_connector", return_value=False):
            return self.controller.create_arbitrage_executor_action(self.PAIR_1, self.PAIR_2)

    def _make_arb_executor(self, buying_market, executor_id="a1", is_active=False,
                           close_type=CloseType.COMPLETED, filled_amount_quote=Decimal("100"),
                           close_timestamp=500.0):
        arb_config = ArbitrageExecutorConfig(
            id=executor_id,
            timestamp=100.0,
            buying_market=buying_market,
            selling_market=self.PAIR_2 if buying_market == self.PAIR_1 else self.PAIR_1,
            order_amount=Decimal("1"),
            min_profitability=Decimal("0.01"),
        )
        return make_executor_info(arb_config, "arbitrage_executor", executor_id=executor_id,
                                  is_active=is_active, close_type=close_type,
                                  close_timestamp=close_timestamp,
                                  filled_amount_quote=filled_amount_quote)

    def test_nan_rate_rejected(self):
        # Would-have-caught GEN-12: Decimal("NaN") passed the falsy `if not rate` check.
        self.market_data_provider.get_rate = MagicMock(return_value=Decimal("NaN"))
        self.assertIsNone(self._create_action())

    def test_zero_and_none_rate_rejected(self):
        self.market_data_provider.get_rate = MagicMock(return_value=Decimal("0"))
        self.assertIsNone(self._create_action())
        self.market_data_provider.get_rate = MagicMock(return_value=None)
        self.assertIsNone(self._create_action())

    def test_zero_quantized_amount_rejected(self):
        self.market_data_provider.get_rate = MagicMock(return_value=Decimal("100"))
        self.market_data_provider.quantize_order_amount = MagicMock(return_value=Decimal("0"))
        self.assertIsNone(self._create_action())

    def test_valid_rate_creates_executor(self):
        self.market_data_provider.get_rate = MagicMock(return_value=Decimal("100"))
        self.market_data_provider.quantize_order_amount = MagicMock(return_value=Decimal("1"))
        action = self._create_action()
        self.assertIsInstance(action, CreateExecutorAction)
        self.assertEqual(Decimal("1"), action.executor_config.order_amount)

    def test_failed_executor_excluded_from_imbalance(self):
        # Would-have-caught GEN-12: a FAILED zero-fill executor stalled a direction.
        self.controller.executors_info = [
            self._make_arb_executor(self.PAIR_1, close_type=CloseType.FAILED,
                                    filled_amount_quote=Decimal("0"))]
        self.controller.update_arbitrage_stats()
        self.assertEqual(0, self.controller._imbalance)

    def test_completed_executor_counted_once_and_survives_archival(self):
        self.controller.executors_info = [self._make_arb_executor(self.PAIR_1)]
        self.controller.update_arbitrage_stats()
        self.assertEqual(1, self.controller._imbalance)
        self.assertEqual(500.0, self.controller._last_buy_closed_timestamp)
        # Re-running does not double count
        self.controller.update_arbitrage_stats()
        self.assertEqual(1, self.controller._imbalance)
        # Archival flush of the buffer must not reset the imbalance
        self.controller.executors_info = []
        self.controller.update_arbitrage_stats()
        self.assertEqual(1, self.controller._imbalance)

    def test_opposite_direction_balances_out(self):
        self.controller.executors_info = [
            self._make_arb_executor(self.PAIR_1, executor_id="buy-1"),
            self._make_arb_executor(self.PAIR_2, executor_id="sell-1"),
        ]
        self.controller.update_arbitrage_stats()
        self.assertEqual(0, self.controller._imbalance)


class TestHedgeAsset(IsolatedAsyncioWrapperTestCase):
    """GEN-13."""

    HEDGE_PAIR = "SOL-USDT"

    def setUp(self):
        self.config = HedgeAssetConfig(
            id="test-hedge",
            total_amount_quote=Decimal("1"),
            spot_connector_name="kraken",
            asset_to_hedge="SOL",
            hedge_connector_name="binance_perpetual",
            hedge_trading_pair=self.HEDGE_PAIR,
            hedge_ratio=Decimal("1"),
            min_notional_size=10,
            cooldown_time=0.0,
        )
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

    async def test_hedge_created_when_no_in_flight(self):
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        cfg = actions[0].executor_config
        self.assertEqual(TradeType.SELL, cfg.side)
        self.assertEqual(Decimal("10"), cfg.amount)

    async def test_no_double_hedge_while_order_in_flight(self):
        # Would-have-caught GEN-13: cooldown was the only guard against re-hedging
        # the same gap while the first hedge order settles.
        self.controller.executors_info = [
            make_order_executor_info("binance_perpetual", self.HEDGE_PAIR,
                                     TradeType.SELL, Decimal("10"), timestamp=999.0)]
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("0"), self.controller.processed_data["hedge_position_gap"])
        self.assertEqual([], self.controller.determine_executor_actions())

    async def test_partial_in_flight_hedges_remainder(self):
        self.controller.executors_info = [
            make_order_executor_info("binance_perpetual", self.HEDGE_PAIR,
                                     TradeType.SELL, Decimal("4"), timestamp=999.0)]
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("6"), actions[0].executor_config.amount)

    async def test_in_flight_buy_offsets_negative_gap(self):
        # Short 10 exists, spot dropped to 0 → gap −10; an in-flight BUY 10 already covers it.
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("0"))
        self.controller.positions_held = [
            make_position_summary("binance_perpetual", self.HEDGE_PAIR, TradeType.SELL,
                                  Decimal("10"), Decimal("100"))]
        self.controller.executors_info = [
            make_order_executor_info("binance_perpetual", self.HEDGE_PAIR,
                                     TradeType.BUY, Decimal("10"), timestamp=999.0)]
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("0"), self.controller.processed_data["hedge_position_gap"])
        self.assertEqual([], self.controller.determine_executor_actions())

    def test_reference_pair_default_preserved(self):
        markets = self.config.update_markets(MarketDict())
        self.assertIn("SOL-USDC", markets["kraken"])

    def test_reference_pair_configurable(self):
        config = self.config.model_copy(update={"spot_reference_quote": "USDT"})
        markets = config.update_markets(MarketDict())
        self.assertIn("SOL-USDT", markets["kraken"])
        self.assertNotIn("SOL-USDC", markets["kraken"])
