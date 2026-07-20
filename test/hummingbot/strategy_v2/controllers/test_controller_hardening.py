"""Controller-base + config hardening tests (V2 strategy fixes phase 4 — B4, B5, B7, B10, B11).

Under test:
- B4: an empty order book (raise or NaN) skips the controller cycle gracefully — previous
  processed_data is kept, one rate-limited warning, and the create path no-ops instead of
  KeyError-ing when no reference price ever arrived.
- B5: config validation — total_amount_quote > 0, no negative spreads (zero stays legal),
  amounts_pct must sum positive when provided; the total_pct division and the dman DCA
  division are guarded at runtime; non-positive computed order prices skip the level.
- B7: pmm_dynamic degenerate indicators (flat closes -> macd.std() == 0) fall back to
  plain mid quoting instead of publishing NaN/inf or raising.
- B10: a bad candles feed logs and is skipped; the controller/strategy still starts.
- B11: two controllers cannot double-start the same non-trading connector.
"""
import asyncio
import math
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
from pydantic import ValidationError

from controllers.market_making.dman_maker_v2 import DManMakerV2, DManMakerV2Config
from controllers.market_making.pmm_dynamic import PMMDynamicController, PMMDynamicControllerConfig
from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)


def _config_kwargs(**overrides):
    kwargs = dict(
        id="hardening_ctrl",
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
    kwargs.update(overrides)
    return kwargs


def _make_controller(config=None, **overrides):
    if config is None:
        config = MarketMakingControllerConfigBase(**_config_kwargs(**overrides))
    mdp = MagicMock(spec=MarketDataProvider)
    mdp.connectors = {config.connector_name: MagicMock()}
    mdp.time.return_value = 1000.0
    ctrl = MarketMakingControllerBase(
        config=config, market_data_provider=mdp, actions_queue=AsyncMock(spec=asyncio.Queue))
    ctrl.executors_info = []
    ctrl.positions_held = []
    return ctrl


class TestEmptyBookCycleSkip(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """B4 — empty-book / NaN reference price skips the cycle instead of aborting it."""

    def _warning_count(self):
        return sum(1 for record in self.log_records
                   if record.levelname == "WARNING"
                   and "Reference price unavailable" in record.getMessage())

    async def test_raising_price_keeps_previous_data_and_rate_limits_warning(self):
        ctrl = _make_controller()
        self.set_loggers(loggers=[ctrl.logger()])
        ctrl.market_data_provider.get_price_by_type.side_effect = EnvironmentError(
            "Order book is empty for ARRR-USDT")

        await ctrl.update_processed_data()
        self.assertEqual({}, ctrl.processed_data)  # first tick: left unset
        self.assertEqual(1, self._warning_count())

        # Repeat inside the rate-limit window: no extra warning.
        ctrl.market_data_provider.time.return_value = 1010.0
        await ctrl.update_processed_data()
        self.assertEqual(1, self._warning_count())

        # Book recovers.
        ctrl.market_data_provider.get_price_by_type.side_effect = None
        ctrl.market_data_provider.get_price_by_type.return_value = Decimal("0.25")
        await ctrl.update_processed_data()
        self.assertEqual(Decimal("0.25"), ctrl.processed_data["reference_price"])

        # Next outage keeps the PREVIOUS reference price rather than wiping it.
        ctrl.market_data_provider.get_price_by_type.side_effect = EnvironmentError("empty again")
        ctrl.market_data_provider.time.return_value = 1050.0
        await ctrl.update_processed_data()
        self.assertEqual(Decimal("0.25"), ctrl.processed_data["reference_price"])
        self.assertEqual(2, self._warning_count())  # re-armed after 30s

    async def test_nan_price_is_treated_as_unavailable(self):
        ctrl = _make_controller()
        self.set_loggers(loggers=[ctrl.logger()])
        ctrl.market_data_provider.get_price_by_type.return_value = Decimal("NaN")
        await ctrl.update_processed_data()
        self.assertEqual({}, ctrl.processed_data)
        self.assertEqual(1, self._warning_count())

    def test_create_path_noops_without_reference_price(self):
        # Book empty since startup: processed_data never populated. The create path must
        # return no actions instead of KeyError-ing in get_price_and_amount.
        ctrl = _make_controller()
        self.set_loggers(loggers=[ctrl.logger()])
        ctrl.processed_data = {}
        self.assertEqual([], ctrl.create_actions_proposal())


class TestConfigValidation(unittest.TestCase):
    """B5 — config validators."""

    def test_zero_total_amount_quote_rejected(self):
        with self.assertRaises(ValidationError):
            MarketMakingControllerConfigBase(**_config_kwargs(total_amount_quote=Decimal("0")))

    def test_negative_total_amount_quote_rejected(self):
        with self.assertRaises(ValidationError):
            MarketMakingControllerConfigBase(**_config_kwargs(total_amount_quote=Decimal("-5")))

    def test_negative_spread_rejected(self):
        with self.assertRaises(ValidationError):
            MarketMakingControllerConfigBase(**_config_kwargs(buy_spreads=[-0.01]))
        with self.assertRaises(ValidationError):
            MarketMakingControllerConfigBase(**_config_kwargs(sell_spreads="0.01,-0.02"))

    def test_zero_spread_stays_legal(self):
        # Top-of-book quoting is legitimate.
        config = MarketMakingControllerConfigBase(**_config_kwargs(
            buy_spreads=[0.0], buy_amounts_pct=[Decimal(50)]))
        self.assertEqual([0.0], config.buy_spreads)

    def test_zero_sum_amounts_pct_rejected_when_provided(self):
        with self.assertRaises(ValidationError):
            MarketMakingControllerConfigBase(**_config_kwargs(buy_amounts_pct=[Decimal(0)]))

    def test_total_pct_division_guard_returns_empty_allocation(self):
        # The validators forbid this from YAML; model_copy bypasses validation the same
        # way programmatic mutation would. The division site must not raise.
        config = MarketMakingControllerConfigBase(**_config_kwargs())
        config = config.model_copy(update={
            "buy_amounts_pct": [Decimal("0")], "sell_amounts_pct": [Decimal("0")]})
        spreads, amounts = config.get_spreads_and_amounts_in_quote(TradeType.BUY)
        self.assertEqual([0.01], spreads)
        self.assertEqual([Decimal("0")], amounts)

    def test_dman_zero_sum_dca_amounts_rejected(self):
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_config_kwargs(
                controller_name="dman_maker_v2",
                dca_spreads="0.01,0.02", dca_amounts="0,0"))

    def test_dman_division_guard_falls_back_to_equal_weights(self):
        config = DManMakerV2Config(**_config_kwargs(
            controller_name="dman_maker_v2",
            dca_spreads="0.01,0.02", dca_amounts="1,3"))
        config = config.model_copy(update={"dca_amounts": [Decimal("0"), Decimal("0")]})
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.connectors = {config.connector_name: MagicMock()}
        controller = DManMakerV2(
            config=config, market_data_provider=mdp, actions_queue=AsyncMock(spec=asyncio.Queue))
        self.assertEqual([Decimal("0.5"), Decimal("0.5")], controller.dca_amounts_pct)


class TestDegenerateLevelBackstop(unittest.TestCase, LoggerMixinForTest):
    """B5 runtime backstop — non-positive computed order price zeroes the level."""

    def test_non_positive_order_price_returns_zero_amount_and_warns(self):
        ctrl = _make_controller()
        self.set_loggers(loggers=[ctrl.logger()])
        # spread 1.0 (100%): buy price = ref * (1 - 1) = 0
        ctrl.processed_data = {"reference_price": Decimal("0.25"), "spread_multiplier": Decimal("1")}
        object.__setattr__(ctrl.config, "buy_spreads", [1.0])
        price, amount = ctrl.get_price_and_amount("buy_0")
        self.assertEqual(Decimal("0"), amount)
        self.assertLessEqual(price, Decimal("0"))
        self.assertTrue(self.is_partially_logged("WARNING", "order price"))

    def test_positive_order_price_unchanged(self):
        ctrl = _make_controller()
        ctrl.processed_data = {"reference_price": Decimal("0.25"), "spread_multiplier": Decimal("1")}
        price, amount = ctrl.get_price_and_amount("buy_0")
        # spreads are floats in config; compare numerically, not by Decimal identity
        self.assertAlmostEqual(0.2475, float(price), places=12)
        self.assertGreater(amount, Decimal("0"))


class TestPMMDynamicDegenerateIndicators(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """B7 — flat closes must not poison processed_data with NaN/inf."""

    def _make_pmm(self, candles_df: pd.DataFrame) -> PMMDynamicController:
        config = PMMDynamicControllerConfig(**_config_kwargs(
            controller_name="pmm_dynamic",
            buy_spreads=[1.0], sell_spreads=[1.0],
            candles_connector="nonkyc", candles_trading_pair="ARRR-USDT",
            interval="1m", macd_fast=3, macd_slow=5, macd_signal=2, natr_length=3))
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.connectors = {"nonkyc": MagicMock()}
        mdp.time.return_value = 1000.0
        mdp.get_candles_df.return_value = candles_df
        ctrl = PMMDynamicController(
            config=config, market_data_provider=mdp, actions_queue=AsyncMock(spec=asyncio.Queue))
        self.set_loggers(loggers=[ctrl.logger()])
        return ctrl

    @staticmethod
    def _timestamps(rows: int):
        # Newest bar 60s before the mocked clock (1000.0) so the CDX-001
        # freshness gate sees live data.
        return [1000.0 - 60.0 * (rows - i) for i in range(rows)]

    @classmethod
    def _flat_candles(cls, close=100.0, rows=60) -> pd.DataFrame:
        return pd.DataFrame({
            "timestamp": cls._timestamps(rows),
            "high": [close] * rows,
            "low": [close] * rows,
            "close": [close] * rows,
        })

    @classmethod
    def _healthy_candles(cls, rows=60) -> pd.DataFrame:
        closes = [100.0 + (i % 7) - 3 + i * 0.1 for i in range(rows)]
        return pd.DataFrame({
            "timestamp": cls._timestamps(rows),
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
        })

    async def test_flat_closes_pause_quoting_without_reference(self):
        # CLA-016: spreads are in units of volatility; the old fallback of
        # spread_multiplier=1 turned "1" into a 100% spread while the log claimed
        # "plain mid quoting". A degenerate NATR must now pause quoting entirely
        # (no reference published — the MM base treats that as nothing-to-quote).
        ctrl = self._make_pmm(self._flat_candles(close=100.0))
        await ctrl.update_processed_data()
        self.assertNotIn("reference_price", ctrl.processed_data)
        self.assertNotIn("spread_multiplier", ctrl.processed_data)
        self.assertTrue(self.is_partially_logged("WARNING", "Degenerate indicators"))
        self.assertTrue(self.is_partially_logged("WARNING", "pausing quoting"))

    async def test_healthy_candles_produce_finite_dynamic_values(self):
        ctrl = self._make_pmm(self._healthy_candles())
        await ctrl.update_processed_data()
        reference_price = ctrl.processed_data["reference_price"]
        spread_multiplier = ctrl.processed_data["spread_multiplier"]
        self.assertTrue(reference_price.is_finite() and reference_price > 0)
        self.assertTrue(spread_multiplier.is_finite() and spread_multiplier > 0)
        self.assertFalse(self.is_partially_logged("WARNING", "Degenerate indicators"))
        self.assertFalse(math.isnan(float(reference_price)))

    async def test_degenerate_warning_is_rate_limited(self):
        ctrl = self._make_pmm(self._flat_candles())
        await ctrl.update_processed_data()
        ctrl.market_data_provider.time.return_value = 1010.0
        await ctrl.update_processed_data()
        warnings = sum(1 for record in self.log_records
                       if record.levelname == "WARNING"
                       and "Degenerate indicators" in record.getMessage())
        self.assertEqual(1, warnings)


class TestBadCandlesFeedIsolation(unittest.TestCase, LoggerMixinForTest):
    """B10 — a bad candles feed is skipped; nothing else dies."""

    def test_controller_initialize_candles_survives_bad_feed(self):
        ctrl = _make_controller()
        self.set_loggers(loggers=[ctrl.logger()])
        ctrl.get_candles_config = lambda: [
            CandlesConfig(connector="typo_exchange", trading_pair="ARRR-USDT", interval="1m"),
            CandlesConfig(connector="nonkyc", trading_pair="ARRR-USDT", interval="1m"),
        ]
        ctrl.market_data_provider.initialize_candles_feed.side_effect = [
            Exception("Connector typo_exchange is not supported"), None]

        ctrl.initialize_candles()  # must not raise

        self.assertEqual(2, ctrl.market_data_provider.initialize_candles_feed.call_count)
        self.assertTrue(self.is_partially_logged("ERROR", "Failed to initialize candles feed"))

    def test_strategy_initialize_candles_isolates_raising_controller(self):
        strategy = MagicMock(spec=StrategyV2Base)
        bad_controller = MagicMock()
        bad_controller.initialize_candles.side_effect = Exception("boom")
        good_controller = MagicMock()
        strategy.controllers = {"bad": bad_controller, "good": good_controller}

        StrategyV2Base.initialize_candles(strategy)  # must not raise

        good_controller.initialize_candles.assert_called_once()


class TestNonTradingConnectorStartRace(IsolatedAsyncioWrapperTestCase):
    """B11 — concurrent initialize calls start the connector exactly once."""

    async def test_concurrent_starts_run_start_network_once(self):
        provider = MarketDataProvider(connectors={})
        start_calls = []

        async def start_network():
            start_calls.append(1)
            await asyncio.sleep(0)  # real suspension point: exposes the old race

        connector = MagicMock()
        connector._trading_pairs = []
        connector.start_network = start_network
        connector.order_book_tracker = MagicMock()
        connector.order_book_tracker._order_book_stream_listener_task = object()

        results = await asyncio.gather(
            provider._ensure_non_trading_connector_started(connector, "kraken", "ETH-USDT"),
            provider._ensure_non_trading_connector_started(connector, "kraken", "ETH-USDT"),
        )

        self.assertEqual([True, True], results)
        self.assertEqual(1, len(start_calls))
        self.assertTrue(provider._non_trading_connectors_started["kraken"])


if __name__ == "__main__":
    unittest.main()
