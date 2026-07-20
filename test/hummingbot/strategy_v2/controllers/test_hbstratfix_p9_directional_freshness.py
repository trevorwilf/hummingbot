"""
hbstrat_fix Phase 9 tests — directional freshness gate + validators + DCA validation.

Findings covered (HBSTRAT_FINDINGS.md):
- CDX-001 / CLA-405 / CLA-406: candle `ready` is length-only — a frozen-but-full
  deque keeps driving entries (directional) and keeps an obsolete reference/spread
  quoting (pmm_dynamic). Shared interval-relative freshness gate, fail-closed to
  signal=0 / no-reference, generous per-market-configurable bound (sparse pairs
  legitimately have old closed bars — regression caveat from both engines).
- CLA-014: indicator-period validators (gt=0 + macd ordering + bollingrid geometry).
- CLA-016: pmm_dynamic degenerate-NATR fallback of 1 misread as 100/200/400%
  spreads; quoting now pauses (no reference) with an accurate log.
- CLA-2b-004: mean_reversion volume_filter_window participates in required_records.
- CLA-2b-006: dman_v3 dynamic_target no longer a no-op when dynamic_order_spread off.
- CDX-010 / CLA-010: DCA validation after all parse forms (dman_v3, dman_maker_v2);
  amounts validator uses .get() so a bad dca_spreads doesn't surface as KeyError.
- CLA-001 (ema/mean_reversion instances): omitted candle-alias fields normalize to
  connector_name / trading_pair (before-validators do not run on omitted defaults).
"""
import asyncio
import math
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
from pydantic import ValidationError

from controllers._shared.candle_freshness import (
    DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS,
    CandleFreshnessGate,
    interval_to_seconds,
)
from controllers.directional_trading.bollinger_v1 import BollingerV1Controller, BollingerV1ControllerConfig
from controllers.directional_trading.bollinger_v2 import BollingerV2Controller, BollingerV2ControllerConfig
from controllers.directional_trading.bollingrid import BollinGridController, BollinGridControllerConfig
from controllers.directional_trading.dman_v3 import DManV3Controller, DManV3ControllerConfig
from controllers.directional_trading.ema_regime_hold_v1 import EMARegimeHoldV1, EMARegimeHoldV1Config
from controllers.directional_trading.macd_bb_v1 import MACDBBV1Controller, MACDBBV1ControllerConfig
from controllers.directional_trading.mean_reversion_bb_rsi_v1 import (
    MeanReversionBBRSIV1,
    MeanReversionBBRSIV1Config,
)
from controllers.directional_trading.supertrend_v1 import SuperTrend, SuperTrendConfig
from controllers.market_making.dman_maker_v2 import DManMakerV2Config
from controllers.market_making.pmm_dynamic import PMMDynamicController, PMMDynamicControllerConfig
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop

FIVE_MIN = 300.0


def _base_kwargs(**overrides):
    kwargs = dict(
        id="p9-test",
        connector_name="binance_perpetual",
        trading_pair="ETH-USDT",
        total_amount_quote=Decimal("100"),
    )
    kwargs.update(overrides)
    return kwargs


def make_candles(closes, last_ts: float, interval_s: float = FIVE_MIN) -> pd.DataFrame:
    """Synthetic OHLCV frame whose newest bar opens at last_ts."""
    n = len(closes)
    return pd.DataFrame({
        "timestamp": [last_ts - interval_s * (n - 1 - i) for i in range(n)],
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [10.0] * n,
    })


def sharp_drop_closes(n_flat=39, drop_to=80.0):
    """39 flat bars near 100 then a crash bar: %B of the last bar is far below 0,
    i.e. below any bb_long_threshold >= 0 — a LONG signal by construction."""
    return [100.0 + 0.01 * (i % 3) for i in range(n_flat)] + [drop_to]


class TestCandleFreshnessGateUnit(IsolatedAsyncioWrapperTestCase):
    """Unit tests for the shared gate itself."""

    def test_interval_parsing(self):
        self.assertEqual(300.0, interval_to_seconds("5m"))
        self.assertEqual(60.0, interval_to_seconds("1m"))
        self.assertEqual(14400.0, interval_to_seconds("4h"))
        self.assertEqual(86400.0, interval_to_seconds("1d"))
        self.assertEqual(604800.0, interval_to_seconds("1w"))
        self.assertEqual(2592000.0, interval_to_seconds("1M"))
        self.assertEqual(1.0, interval_to_seconds("1s"))
        self.assertIsNone(interval_to_seconds("bogus"))
        self.assertIsNone(interval_to_seconds(None))

    def test_fresh_within_bound_and_stale_beyond(self):
        gate = CandleFreshnessGate()
        df = make_candles([100.0] * 5, last_ts=10000.0)
        # age exactly at the bound is still fresh; one second past is stale
        bound_s = 48.0 * FIVE_MIN
        self.assertTrue(gate.check(df, "5m", 10000.0 + bound_s, 48.0).fresh)
        stale = gate.check(df, "5m", 10000.0 + bound_s + 1.0, 48.0)
        self.assertFalse(stale.fresh)
        self.assertEqual("stale", stale.reason)

    def test_sparse_but_within_bound_is_fresh(self):
        # REGRESSION CAVEAT (both engines): sparse pairs legitimately have old
        # closed bars; an age of many intervals below the bound must pass.
        gate = CandleFreshnessGate()
        df = make_candles([100.0] * 5, last_ts=10000.0, interval_s=14400.0)
        ten_intervals_later = 10000.0 + 10 * 14400.0
        self.assertTrue(gate.check(df, "4h", ten_intervals_later, 48.0).fresh)

    def test_non_positive_or_non_finite_bound_fails_closed(self):
        # Adjudicated CDX-R01: the gate is a live-money risk control -- a bound
        # that cannot gate (0, negative, inf, nan, unparseable) must FAIL
        # CLOSED, never silently restore the legacy length-only behavior.
        # Sparse markets widen the bound; nothing disables the gate.
        logger = MagicMock()
        gate = CandleFreshnessGate(logger=logger)
        now = 10000.0
        # Brand-new bar: if the check fails it is the bound handling, not age.
        fresh_df = make_candles([100.0] * 5, last_ts=now)
        for bad_bound in (0, -1.0, float("inf"), float("nan"), None):
            result = gate.check(fresh_df, "5m", now, bad_bound)
            self.assertFalse(result.fresh, f"bound={bad_bound!r} must fail closed")
            self.assertEqual("invalid_bound", result.reason)
        self.assertGreaterEqual(logger.warning.call_count, 1)

    def test_empty_or_missing_data_is_stale(self):
        gate = CandleFreshnessGate()
        self.assertFalse(gate.check(None, "5m", 1000.0, 48.0).fresh)
        self.assertFalse(gate.check(pd.DataFrame(), "5m", 1000.0, 48.0).fresh)
        no_ts = pd.DataFrame({"close": [1.0, 2.0]})
        self.assertFalse(gate.check(no_ts, "5m", 1000.0, 48.0).fresh)
        bad_ts = pd.DataFrame({"timestamp": ["x", "y"], "close": [1.0, 2.0]})
        self.assertFalse(gate.check(bad_ts, "5m", 1000.0, 48.0).fresh)

    def test_unknown_interval_fails_closed(self):
        # Adjudicated CDX-R01: an unparseable interval means an uncomputable
        # age bound -- fail closed. This cannot brick a legitimate deployment:
        # CandlesBase rejects unsupported intervals at feed construction, so a
        # live feed's interval always parses.
        logger = MagicMock()
        gate = CandleFreshnessGate(logger=logger)
        df = make_candles([100.0] * 5, last_ts=1e12)  # newest possible bar
        result = gate.check(df, "weird", 1e12, 48.0)
        self.assertFalse(result.fresh)
        self.assertEqual("unknown_interval", result.reason)
        logger.warning.assert_called_once()
        # Warn once per unknown value, not every tick.
        gate.check(df, "weird", 1e12 + 1.0, 48.0)
        logger.warning.assert_called_once()

    def test_stale_warning_is_rate_limited(self):
        logger = MagicMock()
        gate = CandleFreshnessGate(logger=logger)
        df = make_candles([100.0] * 5, last_ts=0.0)
        now = 1_000_000.0
        gate.check(df, "5m", now, 1.0)
        gate.check(df, "5m", now + 10.0, 1.0)  # inside the 60s warn window
        self.assertEqual(1, logger.warning.call_count)
        gate.check(df, "5m", now + 61.0, 1.0)
        self.assertEqual(2, logger.warning.call_count)


class _DirectionalHarness(IsolatedAsyncioWrapperTestCase):
    NOW = 1_000_000.0

    def _mdp(self):
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.time = MagicMock(return_value=self.NOW)
        return mdp

    def _build(self, controller_cls, config):
        mdp = self._mdp()
        return controller_cls(
            config=config, market_data_provider=mdp,
            actions_queue=AsyncMock(spec=asyncio.Queue))


class TestStaleCandlesZeroSignal(_DirectionalHarness):
    """CDX-001 / CLA-405: frozen-but-full candles must zero the signal.

    Each stale-case fixture WOULD produce a live signal (or computed indicator
    columns) if the age check were removed — deleting the gate flips these tests.
    """

    async def test_bollinger_v1_fresh_signals_and_stale_zeroes(self):
        config = BollingerV1ControllerConfig(**_base_kwargs(bb_length=20))
        controller = self._build(BollingerV1Controller, config)
        fresh_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - 60.0)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=fresh_df)
        await controller.update_processed_data()
        self.assertEqual(1, controller.processed_data["signal"])

        # Same frame, but the feed froze: newest bar is beyond the bound.
        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        stale_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - stale_age)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=stale_df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])

    async def test_bollinger_v1_sparse_but_within_bound_still_signals(self):
        # Regression guard: a closed bar several intervals old (sparse market)
        # must NOT be rejected while inside the configured bound.
        config = BollingerV1ControllerConfig(**_base_kwargs(bb_length=20))
        controller = self._build(BollingerV1Controller, config)
        sparse_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - 10 * FIVE_MIN)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=sparse_df)
        await controller.update_processed_data()
        self.assertEqual(1, controller.processed_data["signal"])

    async def test_bollinger_v1_tightened_bound_is_honored(self):
        config = BollingerV1ControllerConfig(
            **_base_kwargs(bb_length=20, stale_candle_max_age_intervals=5.0))
        controller = self._build(BollingerV1Controller, config)
        df = make_candles(sharp_drop_closes(), last_ts=self.NOW - 6 * FIVE_MIN)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])

    async def test_bollinger_v2_stale_zeroes_signal(self):
        config = BollingerV2ControllerConfig(**_base_kwargs(bb_length=20))
        controller = self._build(BollingerV2Controller, config)
        fresh_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - 60.0)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=fresh_df)
        await controller.update_processed_data()
        self.assertEqual(1, controller.processed_data["signal"])

        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        stale_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - stale_age)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=stale_df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])
        # No indicator computation ran on the stale frame.
        self.assertNotIn("percent", controller.processed_data["features"].columns)

    async def test_bollingrid_stale_zeroes_signal_and_grid_params(self):
        config = BollinGridControllerConfig(**_base_kwargs(bb_length=20))
        controller = self._build(BollinGridController, config)
        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        stale_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - stale_age)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=stale_df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])
        grid = controller.processed_data["grid_params"]
        self.assertIsNone(grid["start_price"])
        self.assertIsNone(grid["end_price"])
        self.assertIsNone(grid["limit_price"])

    async def test_macd_bb_stale_skips_indicators_and_zeroes(self):
        config = MACDBBV1ControllerConfig(
            **_base_kwargs(bb_length=20, macd_fast=3, macd_slow=5, macd_signal=2))
        controller = self._build(MACDBBV1Controller, config)
        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        stale_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - stale_age)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=stale_df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])
        # If the gate were removed, the MACD/BB columns would have been appended.
        self.assertNotIn("MACD_3_5_2", controller.processed_data["features"].columns)

    async def test_supertrend_stale_skips_indicators_and_zeroes(self):
        config = SuperTrendConfig(**_base_kwargs(length=10))
        controller = self._build(SuperTrend, config)
        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        stale_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - stale_age)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=stale_df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertNotIn("SUPERT_10_4.0", controller.processed_data["features"].columns)

    async def test_dman_v3_stale_skips_indicators_and_zeroes(self):
        config = DManV3ControllerConfig(
            **_base_kwargs(bb_length=20, dynamic_order_spread=False, dynamic_target=False))
        controller = self._build(DManV3Controller, config)
        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        stale_df = make_candles(sharp_drop_closes(), last_ts=self.NOW - stale_age)
        controller.market_data_provider.get_candles_df = MagicMock(return_value=stale_df)
        await controller.update_processed_data()
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertNotIn("BBP_20_2.0_2.0", controller.processed_data["features"].columns)


class TestMeanReversionFreshness(_DirectionalHarness):
    """CDX-001 on mean_reversion_bb_rsi_v1 — including the sparse-market guard."""

    def _entry_config(self, **overrides):
        kwargs = dict(
            id="mr-p9",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            candles_connector="nonkyc",
            candles_trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
            use_trend_filter=False,
            volume_filter_window=0,       # disable the volume gate; not under test
            max_spread_pct=0.0,           # disable the spread gate; not under test
        )
        kwargs.update(overrides)
        return MeanReversionBBRSIV1Config(**kwargs)

    @staticmethod
    def _entry_closes():
        # 185 calm bars near 100 then a 15-bar slide to ~85: last close sits far
        # below the lower Bollinger band (%B < 0 <= 0.20) and RSI(14) over a
        # near-monotone decline is far below 40 -> entry conditions hold.
        calm = [100.0 + 0.05 * math.sin(i / 3.0) for i in range(185)]
        slide = [100.0 - (i + 1) * 1.0 for i in range(15)]
        return calm + slide

    async def _run(self, controller, df):
        controller.market_data_provider.get_candles_df = MagicMock(return_value=df)
        await controller.update_processed_data()

    async def test_fresh_entry_conditions_signal_long(self):
        controller = self._build(MeanReversionBBRSIV1, self._entry_config())
        df = make_candles(self._entry_closes(), last_ts=self.NOW - 60.0)
        await self._run(controller, df)
        self.assertEqual(1, controller.processed_data["signal"])

    async def test_stale_feed_zeroes_signal(self):
        config = self._entry_config()
        controller = self._build(MeanReversionBBRSIV1, config)
        stale_age = (config.stale_candle_max_age_intervals + 1) * FIVE_MIN
        df = make_candles(self._entry_closes(), last_ts=self.NOW - stale_age)
        await self._run(controller, df)
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertTrue(controller.processed_data["features"].empty)

    async def test_sparse_old_bar_within_bound_still_signals(self):
        # Regression guard: a legitimately old closed bar (10 intervals) inside
        # the default bound must not be rejected.
        controller = self._build(MeanReversionBBRSIV1, self._entry_config())
        df = make_candles(self._entry_closes(), last_ts=self.NOW - 10 * FIVE_MIN)
        await self._run(controller, df)
        self.assertEqual(1, controller.processed_data["signal"])

    async def test_tightened_bound_is_honored(self):
        controller = self._build(
            MeanReversionBBRSIV1,
            self._entry_config(stale_candle_max_age_intervals=5.0))
        df = make_candles(self._entry_closes(), last_ts=self.NOW - 6 * FIVE_MIN)
        await self._run(controller, df)
        self.assertEqual(0, controller.processed_data["signal"])


class TestEMARegimeFreshness(_DirectionalHarness):
    """CDX-001 on ema_regime_hold_v1: both feeds are gated on their own interval."""

    def _config(self, **overrides):
        kwargs = dict(
            id="ema-p9",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            candles_connector="nonkyc",
            candles_trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
        )
        kwargs.update(overrides)
        return EMARegimeHoldV1Config(**kwargs)

    def _feeds(self, fast_last_ts, slow_last_ts):
        closes_fast = [100.0 + 0.1 * (i % 5) for i in range(400)]
        closes_slow = [100.0 + i * 0.05 for i in range(260)]
        df_fast = make_candles(closes_fast, last_ts=fast_last_ts, interval_s=300.0)
        df_slow = make_candles(closes_slow, last_ts=slow_last_ts, interval_s=14400.0)
        return df_fast, df_slow

    async def _run(self, controller, df_fast, df_slow):
        controller.market_data_provider.get_candles_df = MagicMock(
            side_effect=lambda **kwargs: df_fast if kwargs.get("interval") == "5m" else df_slow)
        await controller.update_processed_data()

    async def test_fresh_feeds_produce_features(self):
        controller = self._build(EMARegimeHoldV1, self._config())
        df_fast, df_slow = self._feeds(self.NOW - 60.0, self.NOW - 3600.0)
        await self._run(controller, df_fast, df_slow)
        self.assertFalse(controller.processed_data["features"].empty)

    async def test_frozen_fast_feed_zeroes_signal(self):
        config = self._config()
        controller = self._build(EMARegimeHoldV1, config)
        stale_fast_ts = self.NOW - (config.stale_candle_max_age_intervals + 1) * 300.0
        df_fast, df_slow = self._feeds(stale_fast_ts, self.NOW - 3600.0)
        await self._run(controller, df_fast, df_slow)
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertTrue(controller.processed_data["features"].empty)

    async def test_frozen_regime_feed_zeroes_signal(self):
        config = self._config()
        controller = self._build(EMARegimeHoldV1, config)
        stale_slow_ts = self.NOW - (config.stale_candle_max_age_intervals + 1) * 14400.0
        df_fast, df_slow = self._feeds(self.NOW - 60.0, stale_slow_ts)
        await self._run(controller, df_fast, df_slow)
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertTrue(controller.processed_data["features"].empty)

    async def test_sparse_regime_bar_within_bound_ok(self):
        # 4h regime bar 10 intervals old (~1.7 days) is legitimate on a sparse
        # pair and inside the default bound: features must still be computed.
        controller = self._build(EMARegimeHoldV1, self._config())
        df_fast, df_slow = self._feeds(self.NOW - 60.0, self.NOW - 10 * 14400.0)
        await self._run(controller, df_fast, df_slow)
        self.assertFalse(controller.processed_data["features"].empty)


class TestPMMDynamicFreshness(IsolatedAsyncioWrapperTestCase):
    """CDX-001 / CLA-406 / CLA-016 on pmm_dynamic."""

    NOW = 1_000_000.0

    def _make_controller(self):
        config = PMMDynamicControllerConfig(
            id="pmm-p9",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
            buy_spreads=[1.0], sell_spreads=[1.0],
            candles_connector="nonkyc", candles_trading_pair="XMR-USDT",
            interval="1m", macd_fast=3, macd_slow=5, macd_signal=2, natr_length=3,
        )
        controller = PMMDynamicController.__new__(PMMDynamicController)
        controller.config = config
        controller.max_records = max(config.macd_slow, config.macd_fast,
                                     config.macd_signal, config.natr_length) + 100
        controller.market_data_provider = MagicMock(spec=MarketDataProvider)
        controller.market_data_provider.time = MagicMock(return_value=self.NOW)
        controller.processed_data = {}
        controller._degenerate_indicator_warning_ts = 0.0
        return controller

    def _healthy_candles(self, last_ts, rows=60):
        closes = [100.0 + (i % 7) - 3 + i * 0.1 for i in range(rows)]
        return make_candles(closes, last_ts=last_ts, interval_s=60.0)

    async def test_healthy_fresh_candles_publish_reference(self):
        controller = self._make_controller()
        controller.market_data_provider.get_candles_df = MagicMock(
            return_value=self._healthy_candles(self.NOW - 30.0))
        await controller.update_processed_data()
        self.assertIn("reference_price", controller.processed_data)
        self.assertTrue(controller.processed_data["reference_price"] > 0)

    async def test_stale_candles_clear_previous_reference(self):
        # CLA-406: pmm_dynamic previously RETAINED the prior processed_data on an
        # unusable update, quoting an obsolete reference indefinitely. On a stale
        # feed the reference must be gone, not merely not-refreshed.
        controller = self._make_controller()
        controller.market_data_provider.get_candles_df = MagicMock(
            return_value=self._healthy_candles(self.NOW - 30.0))
        await controller.update_processed_data()
        self.assertIn("reference_price", controller.processed_data)

        stale_age = (controller.config.stale_candle_max_age_intervals + 1) * 60.0
        controller.market_data_provider.get_candles_df = MagicMock(
            return_value=self._healthy_candles(self.NOW - stale_age))
        await controller.update_processed_data()
        self.assertNotIn("reference_price", controller.processed_data)
        self.assertNotIn("spread_multiplier", controller.processed_data)

    async def test_degenerate_natr_pauses_and_clears_reference(self):
        # CLA-016: flat closes -> NATR 0. The old fallback published
        # spread_multiplier=1, i.e. a 100% spread for a "1"-unit config, while
        # logging "plain mid quoting". Now: no reference, no spread multiplier.
        controller = self._make_controller()
        controller.market_data_provider.get_candles_df = MagicMock(
            return_value=self._healthy_candles(self.NOW - 30.0))
        await controller.update_processed_data()
        self.assertIn("reference_price", controller.processed_data)

        flat = make_candles([100.0] * 60, last_ts=self.NOW - 30.0, interval_s=60.0)
        flat["high"] = 100.0
        flat["low"] = 100.0
        controller.market_data_provider.get_candles_df = MagicMock(return_value=flat)
        await controller.update_processed_data()
        self.assertNotIn("reference_price", controller.processed_data)
        self.assertNotIn("spread_multiplier", controller.processed_data)

    async def test_sparse_old_bar_within_bound_still_quotes(self):
        controller = self._make_controller()
        controller.market_data_provider.get_candles_df = MagicMock(
            return_value=self._healthy_candles(self.NOW - 10 * 60.0))
        await controller.update_processed_data()
        self.assertIn("reference_price", controller.processed_data)


class TestIndicatorPeriodValidators(IsolatedAsyncioWrapperTestCase):
    """CLA-014: gt=0 + ordering validators; bollingrid grid geometry."""

    def test_bollinger_v1_rejects_non_positive_periods(self):
        with self.assertRaises(ValidationError):
            BollingerV1ControllerConfig(**_base_kwargs(bb_length=0))
        with self.assertRaises(ValidationError):
            BollingerV1ControllerConfig(**_base_kwargs(bb_std=0.0))
        with self.assertRaises(ValidationError):
            BollingerV1ControllerConfig(**_base_kwargs(bb_std=-2.0))

    def test_bollinger_v2_rejects_non_positive_periods(self):
        with self.assertRaises(ValidationError):
            BollingerV2ControllerConfig(**_base_kwargs(bb_length=-5))
        with self.assertRaises(ValidationError):
            BollingerV2ControllerConfig(**_base_kwargs(bb_std=0.0))

    def test_macd_bb_rejects_non_positive_and_misordered_periods(self):
        with self.assertRaises(ValidationError):
            MACDBBV1ControllerConfig(**_base_kwargs(macd_fast=0))
        with self.assertRaises(ValidationError):
            MACDBBV1ControllerConfig(**_base_kwargs(macd_signal=-1))
        with self.assertRaises(ValidationError):
            MACDBBV1ControllerConfig(**_base_kwargs(macd_fast=50, macd_slow=20))
        with self.assertRaises(ValidationError):
            MACDBBV1ControllerConfig(**_base_kwargs(macd_fast=21, macd_slow=21))

    def test_supertrend_rejects_non_positive_periods(self):
        with self.assertRaises(ValidationError):
            SuperTrendConfig(**_base_kwargs(length=0))
        with self.assertRaises(ValidationError):
            SuperTrendConfig(**_base_kwargs(multiplier=0.0))

    def test_bollingrid_rejects_bad_grid_geometry(self):
        with self.assertRaises(ValidationError):
            BollinGridControllerConfig(**_base_kwargs(
                grid_start_price_coefficient=0.5, grid_limit_price_coefficient=0.4))
        with self.assertRaises(ValidationError):
            BollinGridControllerConfig(**_base_kwargs(
                grid_start_price_coefficient=0.35, grid_limit_price_coefficient=0.35))
        # Sane geometry still constructs.
        config = BollinGridControllerConfig(**_base_kwargs(
            grid_start_price_coefficient=0.25, grid_limit_price_coefficient=0.35))
        self.assertEqual(0.35, config.grid_limit_price_coefficient)

    def test_pmm_dynamic_rejects_non_positive_and_misordered_periods(self):
        with self.assertRaises(ValidationError):
            PMMDynamicControllerConfig(**_base_kwargs(natr_length=0))
        with self.assertRaises(ValidationError):
            PMMDynamicControllerConfig(**_base_kwargs(macd_fast=42, macd_slow=21))

    def test_dman_v3_rejects_non_positive_periods(self):
        with self.assertRaises(ValidationError):
            DManV3ControllerConfig(**_base_kwargs(
                bb_length=0, dynamic_order_spread=False, dynamic_target=False))

    def test_defaults_still_construct(self):
        # The validators must not reject any stock default.
        for cls in (BollingerV1ControllerConfig, BollingerV2ControllerConfig,
                    BollinGridControllerConfig, MACDBBV1ControllerConfig,
                    SuperTrendConfig, PMMDynamicControllerConfig):
            cls(**_base_kwargs())

    def test_all_configs_reject_non_positive_stale_bound(self):
        # Adjudicated CDX-R01: stale_candle_max_age_intervals is gt=0 in every
        # wired config -- the freshness gate cannot be disabled from config.
        # Sparse markets widen the bound instead of bypassing the control.
        cases = [
            (BollingerV1ControllerConfig, {}),
            (BollingerV2ControllerConfig, {}),
            (BollinGridControllerConfig, {}),
            (MACDBBV1ControllerConfig, {}),
            (SuperTrendConfig, {}),
            (PMMDynamicControllerConfig, {}),
            (DManV3ControllerConfig, {"dynamic_order_spread": False, "dynamic_target": False}),
            (EMARegimeHoldV1Config, {}),
            (MeanReversionBBRSIV1Config, {}),
        ]
        for cls, extra in cases:
            for bad_bound in (0, -1.0):
                with self.assertRaises(ValidationError,
                                       msg=f"{cls.__name__} must reject bound={bad_bound}"):
                    cls(**_base_kwargs(stale_candle_max_age_intervals=bad_bound, **extra))


class TestMeanReversionRequiredRecords(IsolatedAsyncioWrapperTestCase):
    """CLA-2b-004: volume_filter_window participates in history sizing."""

    def _config(self, **overrides):
        kwargs = dict(
            id="mr-req", connector_name="nonkyc", trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"))
        kwargs.update(overrides)
        return MeanReversionBBRSIV1Config(**kwargs)

    def test_volume_window_dominates_when_largest(self):
        config = self._config(volume_filter_window=5000)
        self.assertEqual(5500, config.required_records)
        self.assertEqual(5500, config.candles_config[0].max_records)

    def test_default_window_included_in_max(self):
        # Defaults: bb 80, ema 200, rsi 14, atr 14, volume window 288 -> 288+500.
        config = self._config()
        self.assertEqual(288 + 500, config.required_records)


class TestDManV3DynamicTargetCoupling(IsolatedAsyncioWrapperTestCase):
    """CLA-2b-006: dynamic_target must work without dynamic_order_spread."""

    def _make_controller(self, **config_overrides):
        params = dict(
            id="dman-p9", connector_name="binance_perpetual", trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"), bb_length=20, bb_std=2.0,
            dca_spreads="0.001,0.018,0.15,0.25", trailing_stop="0.015,0.005",
            dynamic_order_spread=False, dynamic_target=False)
        params.update(config_overrides)
        config = DManV3ControllerConfig(**params)
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.time = MagicMock(return_value=1700000000.0)
        return DManV3Controller(
            config=config, market_data_provider=mdp,
            actions_queue=AsyncMock(spec=asyncio.Queue))

    def _features_with_bbb(self, controller, bbb_value):
        column = f"BBB_{controller.config.bb_length}_{controller.config.bb_std}_{controller.config.bb_std}"
        return pd.DataFrame({column: [bbb_value]})

    def test_dynamic_target_scales_barriers_without_dynamic_spread(self):
        # BBB=40 -> multiplier 40/200 = 0.2. The old code scaled targets by
        # get_spread_multiplier(), which is pinned to 1 when dynamic_order_spread
        # is off — making dynamic_target a silent no-op.
        controller = self._make_controller(dynamic_target=True)
        controller.processed_data = {"features": self._features_with_bbb(controller, 40.0)}
        executor_config = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        self.assertEqual(controller.config.take_profit * Decimal("0.2"), executor_config.take_profit)
        self.assertEqual(controller.config.stop_loss * Decimal("0.2"), executor_config.stop_loss)
        self.assertEqual(Decimal("0.015") * Decimal("0.2"),
                         executor_config.trailing_stop.activation_price)

    def test_order_prices_not_scaled_when_dynamic_spread_off(self):
        # Only the targets go dynamic; entry spreads stay static (multiplier 1).
        controller = self._make_controller(dynamic_target=True)
        controller.processed_data = {"features": self._features_with_bbb(controller, 40.0)}
        self.assertEqual(Decimal("1.0"), controller.get_spread_multiplier())
        executor_config = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        expected_first = Decimal("100") * (1 - Decimal("0.001"))
        self.assertEqual(expected_first, executor_config.prices[0])

    def test_both_flags_on_unchanged(self):
        controller = self._make_controller(dynamic_order_spread=True, dynamic_target=True)
        controller.processed_data = {"features": self._features_with_bbb(controller, 40.0)}
        self.assertEqual(Decimal("0.2"), controller.get_spread_multiplier())
        executor_config = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        self.assertEqual(controller.config.take_profit * Decimal("0.2"), executor_config.take_profit)


class TestDCAValidation(IsolatedAsyncioWrapperTestCase):
    """CDX-010 / CLA-010: validation after all parse forms, .get() for spreads."""

    # ---------------- dman_v3 ----------------

    def _dman_v3(self, **overrides):
        params = _base_kwargs(dynamic_order_spread=False, dynamic_target=False)
        params.update(overrides)
        return DManV3ControllerConfig(**params)

    def test_dman_v3_rejects_individual_negative_weight(self):
        # The old sum-only check let "0.5,-0.5,1,1" through -> negative order
        # amounts at the executor.
        with self.assertRaises(ValidationError):
            self._dman_v3(dca_spreads="0.01,0.02,0.03,0.04", dca_amounts_pct="0.5,-0.5,1,1")

    def test_dman_v3_rejects_comma_string_length_mismatch(self):
        with self.assertRaises(ValidationError):
            self._dman_v3(dca_spreads="0.01,0.02", dca_amounts_pct="1,2,3")

    def test_dman_v3_rejects_empty_spreads(self):
        with self.assertRaises(ValidationError):
            self._dman_v3(dca_spreads="")
        with self.assertRaises(ValidationError):
            self._dman_v3(dca_spreads=[])

    def test_dman_v3_rejects_non_numeric(self):
        with self.assertRaises(ValidationError):
            self._dman_v3(dca_spreads="0.01,abc")

    def test_dman_v3_empty_amounts_string_yields_equal_weights(self):
        config = self._dman_v3(dca_spreads="0.01,0.02", dca_amounts_pct="")
        self.assertEqual([Decimal("0.5"), Decimal("0.5")], config.dca_amounts_pct)

    def test_dman_v3_omitted_defaults_are_typed(self):
        # CLA-001: the old defaults were raw comma strings that bypassed the
        # "before" validators when the fields were omitted.
        config = self._dman_v3()
        self.assertEqual(
            [Decimal("0.001"), Decimal("0.018"), Decimal("0.15"), Decimal("0.25")],
            config.dca_spreads)
        self.assertIsInstance(config.trailing_stop, TrailingStop)
        self.assertEqual(Decimal("0.015"), config.trailing_stop.activation_price)

    # ---------------- dman_maker_v2 ----------------

    def test_dman_maker_rejects_comma_string_length_mismatch(self):
        # The old length check sat in an `elif` skipped by the comma-string path,
        # so "1,2,3" against 2 spreads zip-truncated a DCA level silently.
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(dca_spreads="0.01,0.02", dca_amounts="1,2,3"))

    def test_dman_maker_rejects_list_length_mismatch(self):
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(
                dca_spreads="0.01,0.02", dca_amounts=[Decimal("1"), Decimal("2"), Decimal("3")]))

    def test_dman_maker_rejects_empty_spreads(self):
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(dca_spreads=""))
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(dca_spreads=[]))

    def test_dman_maker_rejects_negative_amount(self):
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(dca_spreads="0.01,0.02", dca_amounts="1,-1"))

    def test_dman_maker_rejects_non_positive_spread(self):
        # Adjudicated CDX-R03: a negative spread inverts the maker price (a
        # nominal BUY level prices ABOVE the reference -> marketable order);
        # a zero spread collapses the level onto the reference. Cover the
        # comma-string path (the common YAML form) and the list path.
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(dca_spreads="0.01,-0.02", dca_amounts="1,1"))
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(dca_spreads="0,0.02", dca_amounts="1,1"))
        with self.assertRaises(ValidationError):
            DManMakerV2Config(**_base_kwargs(
                dca_spreads=[Decimal("0.01"), Decimal("-0.02")],
                dca_amounts=[Decimal("1"), Decimal("1")]))

    def test_dman_maker_empty_amounts_yield_equal_weights(self):
        config = DManMakerV2Config(**_base_kwargs(dca_spreads="0.01,0.02", dca_amounts=""))
        self.assertEqual([Decimal("1"), Decimal("1")], config.dca_amounts)

    def test_dman_maker_bad_spreads_surface_their_own_error_not_keyerror(self):
        # The old validator indexed validation_info.data['dca_spreads']; when
        # dca_spreads failed its own validation the key was absent and a bare
        # KeyError escaped the constructor, masking the real problem.
        try:
            DManMakerV2Config(**_base_kwargs(dca_spreads="abc", dca_amounts="0.1,0.2"))
        except ValidationError as e:
            self.assertIn("dca_spreads", str(e))
        else:
            self.fail("expected ValidationError")

    def test_dman_maker_omitted_defaults_are_typed(self):
        config = DManMakerV2Config(**_base_kwargs())
        self.assertEqual(
            [Decimal("0.01"), Decimal("0.02"), Decimal("0.04"), Decimal("0.08")],
            config.dca_spreads)
        self.assertEqual(
            [Decimal("0.1"), Decimal("0.2"), Decimal("0.4"), Decimal("0.8")],
            config.dca_amounts)


class TestCLA001CandleAliasNormalization(IsolatedAsyncioWrapperTestCase):
    """CLA-001 (ema/mean_reversion instances): omitted candle aliases normalize."""

    def test_ema_omitted_aliases_default_to_market(self):
        # Omitted entirely — the mode="before" field validators never run on
        # omitted defaults, so without the model-level normalization these
        # stayed None and the candle feed subscribed to connector None.
        config = EMARegimeHoldV1Config(
            id="t", connector_name="nonkyc", trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"))
        self.assertEqual("nonkyc", config.candles_connector)
        self.assertEqual("XMR-USDT", config.candles_trading_pair)
        for candles_config in config.candles_config:
            self.assertEqual("nonkyc", candles_config.connector)
            self.assertEqual("XMR-USDT", candles_config.trading_pair)

    def test_mean_reversion_omitted_aliases_default_to_market(self):
        config = MeanReversionBBRSIV1Config(
            id="t", connector_name="nonkyc", trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"))
        self.assertEqual("nonkyc", config.candles_connector)
        self.assertEqual("XMR-USDT", config.candles_trading_pair)

    def test_explicit_aliases_not_overridden(self):
        config = MeanReversionBBRSIV1Config(
            id="t", connector_name="nonkyc", trading_pair="XMR-USDT",
            candles_connector="binance", candles_trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"))
        self.assertEqual("binance", config.candles_connector)

    def test_empty_string_aliases_normalize(self):
        config = EMARegimeHoldV1Config(
            id="t", connector_name="nonkyc", trading_pair="XMR-USDT",
            candles_connector="", candles_trading_pair="  ",
            total_amount_quote=Decimal("100"))
        self.assertEqual("nonkyc", config.candles_connector)
        self.assertEqual("XMR-USDT", config.candles_trading_pair)
