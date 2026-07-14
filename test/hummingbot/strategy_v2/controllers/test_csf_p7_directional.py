"""
CSF-V1 Phase 7 tests — directional controllers + base.

Covers:
- DIR-2: base cooldown survives executor close (measured on close_timestamp).
- DIR-3: side filter operator precedence (sell signals no longer match ALL executors).
- DIR-1: bollingrid produces a signal on a synthetic frame with pandas_ta 0.4.x
  column names (old code KeyError'd on every tick).
- DIR-6/7/8/11: dman_v3 degenerate-BBB skip, spread-multiplier floor,
  take_profit passthrough, validators, provider time.
- DIR-5: ai_livestream initialized processed_data, stale-signal zeroing,
  lazy listener retry.
"""
import asyncio
import math
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
from pydantic import ValidationError

from controllers.directional_trading.ai_livestream import AILivestreamController, AILivestreamControllerConfig
from controllers.directional_trading.bollingrid import BollinGridController, BollinGridControllerConfig
from controllers.directional_trading.dman_v3 import DManV3Controller, DManV3ControllerConfig
from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def make_executor_info(
    executor_id: str = "e1",
    side: TradeType = TradeType.BUY,
    timestamp: float = 1000.0,
    is_active: bool = True,
    close_timestamp=None,
):
    config = PositionExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name="binance_perpetual",
        trading_pair="ETH-USDT",
        side=side,
        entry_price=Decimal("100"),
        amount=Decimal("1"),
    )
    return ExecutorInfo(
        id=executor_id,
        timestamp=timestamp,
        type="position_executor",
        status=RunnableStatus.RUNNING if is_active else RunnableStatus.TERMINATED,
        config=config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=Decimal("0"),
        is_active=is_active,
        is_trading=is_active,
        custom_info={"side": side},
        close_timestamp=close_timestamp,
    )


class TestDirectionalBaseCooldownAndSideFilter(IsolatedAsyncioWrapperTestCase):
    COOLDOWN = 300

    def setUp(self):
        self.config = DirectionalTradingControllerConfigBase(
            id="test",
            controller_name="directional_trading_test_controller",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"),
            max_executors_per_side=2,
            cooldown_time=self.COOLDOWN,
            leverage=1,
            position_mode=PositionMode.HEDGE,
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.controller = DirectionalTradingControllerBase(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    def _set_time(self, now: float):
        self.market_data_provider.time = MagicMock(return_value=now)

    def test_cooldown_survives_executor_close(self):
        # Would-have-caught DIR-2: a CLOSED same-side executor must still enforce cooldown.
        closed = make_executor_info(
            side=TradeType.BUY, timestamp=500.0, is_active=False, close_timestamp=1000.0)
        self.controller.executors_info = [closed]
        self._set_time(1000.0 + 100.0)  # inside cooldown after close
        self.assertFalse(self.controller.can_create_executor(1))
        self._set_time(1000.0 + self.COOLDOWN + 1.0)  # past cooldown
        self.assertTrue(self.controller.can_create_executor(1))

    def test_cooldown_prefers_close_timestamp_over_creation(self):
        # Creation was long ago but close was recent: cooldown must key off the close.
        closed = make_executor_info(
            side=TradeType.BUY, timestamp=500.0, is_active=False, close_timestamp=1000.0)
        self.controller.executors_info = [closed]
        self._set_time(1250.0)  # 750s after creation, 250s after close
        self.assertFalse(self.controller.can_create_executor(1))

    def test_no_history_allows_create(self):
        self.controller.executors_info = []
        self._set_time(10.0)  # small clock: with last_ts==0 the old vacuous check could block
        self.assertTrue(self.controller.can_create_executor(1))

    def test_sell_side_filter_ignores_buy_executors(self):
        # Would-have-caught DIR-3: with the precedence bug, a SELL signal matched
        # ALL active executors, so two BUYs exhausted max_executors_per_side.
        buys = [
            make_executor_info(executor_id="b1", side=TradeType.BUY, timestamp=100.0),
            make_executor_info(executor_id="b2", side=TradeType.BUY, timestamp=100.0),
        ]
        self.controller.executors_info = buys
        self._set_time(100.0 + self.COOLDOWN + 1.0)
        self.assertTrue(self.controller.can_create_executor(-1))
        # BUY side genuinely full → blocked.
        self.assertFalse(self.controller.can_create_executor(1))

    def test_sell_cooldown_not_triggered_by_buy_close(self):
        closed_buy = make_executor_info(
            side=TradeType.BUY, timestamp=500.0, is_active=False, close_timestamp=1000.0)
        self.controller.executors_info = [closed_buy]
        self._set_time(1010.0)  # just after the BUY close
        self.assertTrue(self.controller.can_create_executor(-1))
        self.assertFalse(self.controller.can_create_executor(1))


class TestBollinGridSignal(IsolatedAsyncioWrapperTestCase):
    def setUp(self):
        self.config = BollinGridControllerConfig(
            id="test-bollingrid",
            controller_name="bollingrid",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"),
            bb_length=20,
            bb_std=2.0,
            bb_long_threshold=0.0,
            bb_short_threshold=1.0,
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.controller = BollinGridController(
            config=self.config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    @staticmethod
    def _synthetic_df(closes):
        n = len(closes)
        return pd.DataFrame({
            "timestamp": np.arange(n, dtype=float) * 300.0,
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [10.0] * n,
        })

    async def test_long_signal_on_sharp_drop(self):
        # Would-have-caught DIR-1: pandas_ta 0.4.x names the columns
        # BBP_20_2.0_2.0 — the old BBP_20_2.0 lookup raised KeyError every tick.
        closes = [100.0 + 0.01 * (i % 3) for i in range(39)] + [80.0]
        self.market_data_provider.get_candles_df = MagicMock(return_value=self._synthetic_df(closes))
        await self.controller.update_processed_data()
        self.assertEqual(1, self.controller.processed_data["signal"])
        grid = self.controller.processed_data["grid_params"]
        self.assertIsNotNone(grid["start_price"])
        self.assertIsNotNone(grid["end_price"])
        self.assertIsNotNone(grid["limit_price"])
        self.assertLess(grid["start_price"], grid["end_price"])

    async def test_no_signal_on_flat_frame(self):
        closes = [100.0 + 0.05 * math.sin(i) for i in range(40)]
        self.market_data_provider.get_candles_df = MagicMock(return_value=self._synthetic_df(closes))
        await self.controller.update_processed_data()
        self.assertEqual(0, self.controller.processed_data["signal"])
        self.assertIsNone(self.controller.processed_data["grid_params"]["start_price"])

    def test_max_records_has_warmup_margin(self):
        # DIR-12
        self.assertEqual(self.config.bb_length + 20, self.controller.max_records)

    def test_default_interval_is_5m(self):
        # DIR-10
        self.assertEqual("5m", BollinGridControllerConfig.model_fields["interval"].default)


class TestDManV3(IsolatedAsyncioWrapperTestCase):
    def _make_controller(self, **config_overrides):
        params = dict(
            id="test-dman",
            controller_name="dman_v3",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"),
            bb_length=20,
            bb_std=2.0,
            # Passed explicitly because pydantic does not run "before" validators
            # on defaults — the default string would reach the controller unparsed.
            dca_spreads="0.001,0.018,0.15,0.25",
            trailing_stop="0.015,0.005",
            dynamic_order_spread=False,
            dynamic_target=False,
        )
        params.update(config_overrides)
        config = DManV3ControllerConfig(**params)
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=1700000000.0)
        controller = DManV3Controller(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def _features_with_bbb(self, controller, bbb_value):
        column = f"BBB_{controller.config.bb_length}_{controller.config.bb_std}_{controller.config.bb_std}"
        return pd.DataFrame({column: [bbb_value]})

    def test_degenerate_bbb_skips_create(self):
        # Would-have-caught DIR-6: BBB=0 disarmed the stop-loss and made the
        # trailing stop instant-close; now creation is skipped entirely.
        controller = self._make_controller(dynamic_order_spread=True)
        controller.processed_data = {
            "signal": 1, "features": self._features_with_bbb(controller, 0.0)}
        self.assertFalse(controller.can_create_executor(1))
        controller.processed_data["features"] = self._features_with_bbb(controller, float("nan"))
        self.assertFalse(controller.can_create_executor(1))

    def test_healthy_bbb_allows_create(self):
        controller = self._make_controller(dynamic_order_spread=True)
        controller.processed_data = {
            "signal": 1, "features": self._features_with_bbb(controller, 10.0)}
        controller.executors_info = []
        self.assertTrue(controller.can_create_executor(1))

    def test_spread_multiplier_floored(self):
        controller = self._make_controller(dynamic_order_spread=True)
        controller.processed_data = {"features": self._features_with_bbb(controller, 0.5)}
        # 0.5 / 200 = 0.0025 < floor 0.01
        self.assertEqual(Decimal("0.01"), controller.get_spread_multiplier())
        controller.processed_data = {"features": self._features_with_bbb(controller, 40.0)}
        self.assertEqual(Decimal("0.2"), controller.get_spread_multiplier())

    def test_take_profit_passthrough_static(self):
        # Would-have-caught DIR-7: take_profit never reached DCAExecutorConfig.
        controller = self._make_controller()
        executor_config = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        self.assertEqual(controller.config.take_profit, executor_config.take_profit)
        self.assertEqual(controller.config.stop_loss, executor_config.stop_loss)

    def test_take_profit_scaled_when_dynamic_target(self):
        controller = self._make_controller(dynamic_order_spread=True, dynamic_target=True)
        controller.processed_data = {"features": self._features_with_bbb(controller, 40.0)}
        executor_config = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        self.assertEqual(controller.config.take_profit * Decimal("0.2"), executor_config.take_profit)
        self.assertEqual(controller.config.stop_loss * Decimal("0.2"), executor_config.stop_loss)

    def test_none_stop_loss_under_dynamic_target(self):
        # DIR-8: previously TypeError (None * Decimal).
        controller = self._make_controller(
            dynamic_order_spread=True, dynamic_target=True, stop_loss="", take_profit="")
        controller.processed_data = {"features": self._features_with_bbb(controller, 40.0)}
        executor_config = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        self.assertIsNone(executor_config.stop_loss)
        self.assertIsNone(executor_config.take_profit)

    def test_timestamp_uses_provider_time(self):
        # DIR-11
        controller = self._make_controller()
        executor_config = controller.get_executor_config(TradeType.SELL, Decimal("100"), Decimal("1"))
        self.assertEqual(1700000000.0, executor_config.timestamp)

    def test_validator_rejects_zero_sum_amounts(self):
        # DIR-8
        with self.assertRaises(ValidationError):
            DManV3ControllerConfig(
                id="t", controller_name="dman_v3", connector_name="binance_perpetual",
                trading_pair="ETH-USDT", total_amount_quote=Decimal("100"),
                dca_spreads="0.01,0.02", dca_amounts_pct="0,0",
                dynamic_order_spread=False, dynamic_target=False,
            )

    def test_validator_rejects_non_positive_spread(self):
        with self.assertRaises(ValidationError):
            DManV3ControllerConfig(
                id="t", controller_name="dman_v3", connector_name="binance_perpetual",
                trading_pair="ETH-USDT", total_amount_quote=Decimal("100"),
                dca_spreads="0,0.02",
                dynamic_order_spread=False, dynamic_target=False,
            )

    def test_max_records_has_warmup_margin(self):
        controller = self._make_controller()
        self.assertEqual(controller.config.bb_length + 20, controller.max_records)

    def test_default_interval_is_5m(self):
        self.assertEqual("5m", DManV3ControllerConfig.model_fields["interval"].default)


class TestAILivestream(IsolatedAsyncioWrapperTestCase):
    def _make_controller(self, **config_overrides):
        params = dict(
            id="test-ai",
            controller_name="ai_livestream",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"),
        )
        params.update(config_overrides)
        config = AILivestreamControllerConfig(**params)
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=1000.0)
        controller = AILivestreamController(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def test_processed_data_initialized(self):
        # DIR-5: previously {} → per-tick KeyError until the first MQTT message.
        controller = self._make_controller()
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertEqual({}, controller.processed_data["features"])

    async def test_stale_signal_zeroed(self):
        controller = self._make_controller(signal_max_age=300)
        controller.processed_data["signal"] = 1
        controller._last_signal_ts = 1000.0
        controller.market_data_provider.time = MagicMock(return_value=1200.0)
        await controller.update_processed_data()
        self.assertEqual(1, controller.processed_data["signal"])  # fresh: kept
        controller.market_data_provider.time = MagicMock(return_value=1400.0)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])  # stale: zeroed

    async def test_signal_without_timestamp_zeroed(self):
        controller = self._make_controller()
        controller.processed_data["signal"] = -1
        controller._last_signal_ts = None
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])

    async def test_listener_retry_when_missing(self):
        controller = self._make_controller()
        controller._ml_signal_listener = None
        with patch.object(controller, "_init_ml_signal_listener") as init_mock:
            await controller.update_processed_data()
            init_mock.assert_called_once()

    async def test_no_listener_retry_when_present(self):
        controller = self._make_controller()
        controller._ml_signal_listener = MagicMock()
        with patch.object(controller, "_init_ml_signal_listener") as init_mock:
            await controller.update_processed_data()
            init_mock.assert_not_called()

    def test_handle_signal_stamps_time(self):
        controller = self._make_controller()
        controller.market_data_provider.time = MagicMock(return_value=2222.0)
        controller._handle_ml_signal({"probabilities": [0.1, 0.2, 0.7]}, "topic")
        self.assertEqual(1, controller.processed_data["signal"])
        self.assertEqual(2222.0, controller._last_signal_ts)

    def test_stop_removes_listener(self):
        controller = self._make_controller()
        listener = MagicMock()
        controller._ml_signal_listener = listener
        with patch("controllers.directional_trading.ai_livestream.ExternalTopicFactory.remove_listener") as remove_mock:
            controller.stop()
            remove_mock.assert_called_once_with(listener)
        self.assertIsNone(controller._ml_signal_listener)
