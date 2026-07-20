"""
Phase-1 (hbstrat_fix) tests — stat_arb exit logic and candle alignment.

Covers:
- CDX-005: hedge-mode pair PnL aggregates EVERY PositionSummary for the configured
  pairs (both sides during a signal flip), unrealized-only, denominated on gross
  |amount_quote| exposure — would fail if only the current/first side were summed.
- CLA-304: a global TP/SL breach stops all active entry (position) executors while
  excluding in-flight close order executors, and latches a stop-out cooldown that
  gates get_executors_to_quote so the still-hot z-score cannot immediately re-enter;
  the latch expires (no dead strategy) and risk-reducing paths stay live during it.
- CDX-004: candles are paired by timestamp (inner join), never by array position;
  forming bars, NaN/non-positive closes and duplicate timestamps are cleaned before
  the regression, and misaligned grids yield "signal unavailable" instead of a
  fabricated spread.
- CLA-012: config validators for pos_hedge_ratio (the -1 __init__ crash),
  entry_threshold, tp_global/sl_global, lookback_period, stop_out_cooldown, interval,
  and distinct dominant/hedge markets.

All expected values are hand-derived from the findings/spec, never captured from
running the implementation.
"""
import asyncio
import math
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
from pydantic import ValidationError

from controllers.generic.stat_arb import StatArb, StatArbConfig
from hummingbot.core.data_type.common import PositionAction, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

DOMINANT = ConnectorPair(connector_name="kraken", trading_pair="SOL-USDT")
HEDGE = ConnectorPair(connector_name="kraken", trading_pair="ADA-USDT")


def make_executor_info(config, executor_type: str, executor_id: str = "e1", timestamp: float = 1000.0,
                       is_active: bool = True, is_trading: bool = False, custom_info=None,
                       filled_amount_quote=Decimal("0")):
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
        close_type=None,
        close_timestamp=None,
    )


def make_order_executor_info(connector_pair: ConnectorPair, side: TradeType, amount: Decimal,
                             executor_id: str = "oe1", is_active: bool = True, timestamp: float = 1000.0):
    config = OrderExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name=connector_pair.connector_name,
        trading_pair=connector_pair.trading_pair,
        side=side,
        amount=amount,
        position_action=PositionAction.CLOSE,
        execution_strategy=ExecutionStrategy.MARKET,
    )
    return make_executor_info(config, "order_executor", executor_id=executor_id,
                              timestamp=timestamp, is_active=is_active,
                              custom_info={"side": side})


def make_position_executor_info(connector_pair: ConnectorPair, side: TradeType,
                                executor_id: str = "pe1", is_active: bool = True,
                                is_trading: bool = False, timestamp: float = 1000.0):
    config = PositionExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name=connector_pair.connector_name,
        trading_pair=connector_pair.trading_pair,
        side=side,
        entry_price=Decimal("100"),
        amount=Decimal("1"),
    )
    return make_executor_info(config, "position_executor", executor_id=executor_id,
                              timestamp=timestamp, is_active=is_active, is_trading=is_trading,
                              custom_info={"side": side})


def make_position_summary(connector_pair: ConnectorPair, side: TradeType, amount: Decimal,
                          breakeven_price: Decimal, unrealized_pnl_quote: Decimal = Decimal("0"),
                          realized_pnl_quote: Decimal = Decimal("0"),
                          cum_fees_quote: Decimal = Decimal("0")):
    return PositionSummary(
        connector_name=connector_pair.connector_name,
        trading_pair=connector_pair.trading_pair,
        volume_traded_quote=amount * breakeven_price,
        side=side,
        amount=amount,
        breakeven_price=breakeven_price,
        unrealized_pnl_quote=unrealized_pnl_quote,
        realized_pnl_quote=realized_pnl_quote,
        cum_fees_quote=cum_fees_quote,
    )


def config_kwargs(**overrides):
    kwargs = dict(
        id="test-stat-arb-p1",
        total_amount_quote=Decimal("200"),
        connector_pair_dominant=DOMINANT,
        connector_pair_hedge=HEDGE,
        tp_global=Decimal("0.01"),
        sl_global=Decimal("0.05"),
    )
    kwargs.update(overrides)
    return kwargs


def make_controller(config: StatArbConfig):
    market_data_provider = MagicMock(spec=MarketDataProvider)
    market_data_provider.time = MagicMock(return_value=1000.0)
    market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("100"))
    market_data_provider.get_candles_df = MagicMock(return_value=pd.DataFrame())
    controller = StatArb(
        config=config,
        market_data_provider=market_data_provider,
        actions_queue=AsyncMock(spec=asyncio.Queue),
    )
    return controller, market_data_provider


class TestStatArbConfigValidators(TestCase):
    """CLA-012."""

    def test_pos_hedge_ratio_rejects_minus_one_crash_value(self):
        # -1 makes __init__ divide by zero: total_amount_quote * (1 / (1 + (-1)))
        with self.assertRaises(ValidationError):
            StatArbConfig(**config_kwargs(pos_hedge_ratio=Decimal("-1")))

    def test_pos_hedge_ratio_rejects_non_positive_and_non_finite(self):
        for bad in (Decimal("0"), Decimal("-2"), Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(ValidationError, msg=f"pos_hedge_ratio={bad} was accepted"):
                StatArbConfig(**config_kwargs(pos_hedge_ratio=bad))

    def test_entry_threshold_rejects_non_positive(self):
        # A zero/negative threshold turns every z-score reading into an entry signal
        for bad in (Decimal("0"), Decimal("-2"), Decimal("NaN")):
            with self.assertRaises(ValidationError, msg=f"entry_threshold={bad} was accepted"):
                StatArbConfig(**config_kwargs(entry_threshold=bad))

    def test_sl_global_rejects_non_positive(self):
        # sl_global <= 0 fires the global stop at zero PnL
        for bad in (Decimal("0"), Decimal("-0.05"), Decimal("NaN")):
            with self.assertRaises(ValidationError, msg=f"sl_global={bad} was accepted"):
                StatArbConfig(**config_kwargs(sl_global=bad))

    def test_tp_global_rejects_non_positive(self):
        for bad in (Decimal("0"), Decimal("-0.01")):
            with self.assertRaises(ValidationError, msg=f"tp_global={bad} was accepted"):
                StatArbConfig(**config_kwargs(tp_global=bad))

    def test_lookback_period_rejects_lt_two(self):
        with self.assertRaises(ValidationError):
            StatArbConfig(**config_kwargs(lookback_period=1))

    def test_stop_out_cooldown_rejects_negative(self):
        with self.assertRaises(ValidationError):
            StatArbConfig(**config_kwargs(stop_out_cooldown=-1))

    def test_interval_rejects_unknown_string(self):
        with self.assertRaises(ValidationError):
            StatArbConfig(**config_kwargs(interval="7m"))

    def test_identical_dominant_and_hedge_markets_rejected(self):
        with self.assertRaises(ValidationError):
            StatArbConfig(**config_kwargs(connector_pair_dominant=DOMINANT,
                                          connector_pair_hedge=DOMINANT))

    def test_valid_config_accepted(self):
        config = StatArbConfig(**config_kwargs(pos_hedge_ratio=Decimal("0.5"),
                                               entry_threshold=Decimal("1.5"),
                                               stop_out_cooldown=0))
        self.assertEqual(Decimal("0.5"), config.pos_hedge_ratio)
        # defaults construct too
        defaults = StatArbConfig(id="d", total_amount_quote=Decimal("100"))
        self.assertEqual(300, defaults.stop_out_cooldown)


class TestStatArbHedgeModePnlAggregation(IsolatedAsyncioWrapperTestCase):
    """CDX-005."""

    def setUp(self):
        self.config = StatArbConfig(**config_kwargs())
        self.controller, self.market_data_provider = make_controller(self.config)

    async def test_pair_pnl_aggregates_both_sides_of_hedge_mode(self):
        # Signal flip in HEDGE mode: winning new side +1, losing old side -20.
        # Summed unrealized = -19 over gross exposure 200 -> -9.5%.
        winning_new_side = make_position_summary(DOMINANT, TradeType.BUY, Decimal("1"),
                                                 Decimal("100"), Decimal("1"))
        losing_old_side = make_position_summary(DOMINANT, TradeType.SELL, Decimal("1"),
                                                Decimal("100"), Decimal("-20"))
        self.controller.positions_held = [winning_new_side, losing_old_side]
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("-0.095"), self.controller.processed_data["pair_pnl_pct"])
        self.assertEqual(Decimal("200"), self.controller.processed_data["position_dominant_quote"])

        # Deterministic under reordering — kills the old next(...) first-match pick
        self.controller.positions_held = [losing_old_side, winning_new_side]
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("-0.095"), self.controller.processed_data["pair_pnl_pct"])

    async def test_pair_pnl_spans_both_configured_pairs(self):
        # dominant +2 on 100 quote; hedge -8 on 0.5*200=100 quote -> -6/200 = -3%
        self.controller.positions_held = [
            make_position_summary(DOMINANT, TradeType.BUY, Decimal("1"), Decimal("100"), Decimal("2")),
            make_position_summary(HEDGE, TradeType.SELL, Decimal("0.5"), Decimal("200"), Decimal("-8")),
        ]
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("-0.03"), self.controller.processed_data["pair_pnl_pct"])
        self.assertEqual(Decimal("100"), self.controller.processed_data["position_hedge_quote"])

    async def test_global_sl_fires_on_losing_non_current_side(self):
        # Only the winning side visible would read +1% (no breach). With the losing
        # old side included: (-20 + 1)/200 = -9.5% < -5% -> the SL MUST close BOTH.
        self.controller.positions_held = [
            make_position_summary(DOMINANT, TradeType.BUY, Decimal("1"), Decimal("100"), Decimal("1")),
            make_position_summary(DOMINANT, TradeType.SELL, Decimal("1"), Decimal("100"), Decimal("-20")),
        ]
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(2, len(creates))
        by_side = {c.executor_config.side: c.executor_config for c in creates}
        self.assertEqual({TradeType.SELL, TradeType.BUY}, set(by_side.keys()))
        for cfg in by_side.values():
            self.assertEqual("order_executor", cfg.type)
            self.assertEqual(Decimal("1"), cfg.amount)
            self.assertEqual(PositionAction.CLOSE, cfg.position_action)

    async def test_pair_pnl_is_unrealized_only(self):
        # Banked realized profit (+50, survives restarts in this fork) must not mask a
        # live -20% unrealized loss: pct is -0.20, not +0.30.
        self.controller.positions_held = [
            make_position_summary(DOMINANT, TradeType.BUY, Decimal("1"), Decimal("100"),
                                  unrealized_pnl_quote=Decimal("-20"),
                                  realized_pnl_quote=Decimal("50")),
        ]
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("-0.2"), self.controller.processed_data["pair_pnl_pct"])
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        self.assertEqual(TradeType.SELL, creates[0].executor_config.side)

    async def test_flat_book_reads_zero_pnl(self):
        self.controller.positions_held = []
        await self.controller.update_processed_data()
        self.assertEqual(Decimal("0"), self.controller.processed_data["pair_pnl_pct"])


class TestStatArbGlobalSlBreachBehavior(IsolatedAsyncioWrapperTestCase):
    """CLA-304."""

    def setUp(self):
        self.config = StatArbConfig(**config_kwargs())  # stop_out_cooldown default 300
        self.controller, self.market_data_provider = make_controller(self.config)

    def _set_time(self, now: float):
        self.market_data_provider.time = MagicMock(return_value=now)

    def _losing_position(self, amount=Decimal("1")):
        # unrealized -25 per unit on amount*100 quote -> -25% < -5% sl_global
        return make_position_summary(DOMINANT, TradeType.BUY, amount, Decimal("100"),
                                     Decimal("-25") * amount)

    def _arm_quote_path(self, signal=1):
        self.controller.processed_data.update({
            "signal": signal,
            "pair_pnl_pct": Decimal("0"),
            "dominant_gap": Decimal("100"),
            "hedge_gap": Decimal("100"),
            "filter_connector_pair": None,
            "executors_dominant_placed": [],
            "executors_dominant_filled": [],
            "executors_hedge_placed": [],
            "executors_hedge_filled": [],
            "min_price_dominant": Decimal("100"),
            "max_price_dominant": Decimal("100"),
            "min_price_hedge": Decimal("100"),
            "max_price_hedge": Decimal("100"),
            "dominant_price": Decimal("100"),
            "hedge_price": Decimal("100"),
        })

    async def test_breach_stops_active_entry_executors(self):
        self.controller.positions_held = [self._losing_position()]
        self.controller.executors_info = [
            make_position_executor_info(DOMINANT, TradeType.BUY, executor_id="entry-placed"),
            make_position_executor_info(DOMINANT, TradeType.BUY, executor_id="entry-filled", is_trading=True),
            make_position_executor_info(HEDGE, TradeType.SELL, executor_id="entry-hedge"),
        ]
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        stops = [a for a in actions if isinstance(a, StopExecutorAction)]
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual({"entry-placed", "entry-filled", "entry-hedge"},
                         {a.executor_id for a in stops})
        self.assertTrue(all(a.keep_position for a in stops))
        self.assertEqual(1, len(creates))
        self.assertEqual("order_executor", creates[0].executor_config.type)
        self.assertEqual(TradeType.SELL, creates[0].executor_config.side)

    async def test_breach_does_not_stop_in_flight_close_executor(self):
        self.controller.positions_held = [self._losing_position(amount=Decimal("4"))]
        self.controller.executors_info = [
            make_order_executor_info(DOMINANT, TradeType.SELL, Decimal("3"), executor_id="closer-1"),
        ]
        await self.controller.update_processed_data()
        actions = self.controller.determine_executor_actions()
        stops = [a for a in actions if isinstance(a, StopExecutorAction)]
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        # The in-flight close is NOT cancelled, and the new close covers only the rest
        self.assertEqual([], stops)
        self.assertEqual(1, len(creates))
        self.assertEqual(Decimal("1"), creates[0].executor_config.amount)

    async def test_breach_latches_cooldown_and_blocks_reentry(self):
        self._set_time(1000.0)
        self.controller.positions_held = [self._losing_position()]
        await self.controller.update_processed_data()
        self.controller.determine_executor_actions()  # breach tick -> latch until 1300

        # Now flat, but the z-score is still hot
        self.controller.positions_held = []
        self._arm_quote_path(signal=1)
        self._set_time(1100.0)
        self.assertTrue(self.controller.stop_out_cooldown_active())
        actions = self.controller.determine_executor_actions()
        self.assertEqual([], [a for a in actions if isinstance(a, CreateExecutorAction)])

    async def test_cooldown_expires_and_quoting_resumes(self):
        self._set_time(1000.0)
        self.controller.positions_held = [self._losing_position()]
        await self.controller.update_processed_data()
        self.controller.determine_executor_actions()  # latch until 1300

        self.controller.positions_held = []
        self._arm_quote_path(signal=1)
        self._set_time(1300.0)  # 1000 + stop_out_cooldown(300)
        self.assertFalse(self.controller.stop_out_cooldown_active())
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(2, len(creates))
        by_pair = {c.executor_config.trading_pair: c.executor_config for c in creates}
        self.assertEqual(TradeType.BUY, by_pair["SOL-USDT"].side)     # signal 1: long dominant
        self.assertEqual(TradeType.SELL, by_pair["ADA-USDT"].side)    # signal 1: short hedge
        self.assertEqual(Decimal("99.99"), by_pair["SOL-USDT"].entry_price)   # 100 * (1 - 0.0001)
        self.assertEqual(Decimal("100.01"), by_pair["ADA-USDT"].entry_price)  # 100 * (1 + 0.0001)

    def test_no_gate_without_breach(self):
        self._arm_quote_path(signal=1)
        self.assertFalse(self.controller.stop_out_cooldown_active())
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(2, len(creates))

    async def test_risk_reduction_not_gated_during_cooldown(self):
        self._set_time(1000.0)
        self.controller.positions_held = [self._losing_position()]
        await self.controller.update_processed_data()
        self.controller.determine_executor_actions()  # latch until 1300

        # Opposite-side position while cooldown is active and the signal is hot:
        # entry quoting is gated, but the risk-reducing close must still go out.
        self.controller.positions_held = [
            make_position_summary(DOMINANT, TradeType.SELL, Decimal("1"), Decimal("100"))]
        self.controller.executors_info = []
        self._arm_quote_path(signal=1)
        self._set_time(1100.0)
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        cfg = creates[0].executor_config
        self.assertEqual("order_executor", cfg.type)
        self.assertEqual(TradeType.BUY, cfg.side)
        self.assertEqual(PositionAction.CLOSE, cfg.position_action)

    async def test_relatch_extends_cooldown_from_last_breached_tick(self):
        self._set_time(1000.0)
        self.controller.positions_held = [self._losing_position()]
        await self.controller.update_processed_data()
        self.controller.determine_executor_actions()  # latch until 1300

        self._set_time(1200.0)  # still breached -> latch moves to 1500
        await self.controller.update_processed_data()
        self.controller.determine_executor_actions()

        self.controller.positions_held = []
        self._arm_quote_path(signal=1)
        self._set_time(1400.0)  # would be free under the first latch, not the second
        actions = self.controller.determine_executor_actions()
        self.assertEqual([], [a for a in actions if isinstance(a, CreateExecutorAction)])


class TestStatArbCandleAlignment(IsolatedAsyncioWrapperTestCase):
    """CDX-004. interval 1m -> 60s bars; lookback_period 50."""

    def setUp(self):
        self.config = StatArbConfig(**config_kwargs(lookback_period=50))
        self.controller, self.market_data_provider = make_controller(self.config)

    @staticmethod
    def _frame(timestamps, closes):
        return pd.DataFrame({"timestamp": timestamps, "close": closes})

    @staticmethod
    def _grid(n, offset=0.0):
        return [i * 60.0 + offset for i in range(n)]

    def _set_candles(self, dominant_df, hedge_df):
        def by_pair(**kwargs):
            return dominant_df if kwargs["trading_pair"] == "SOL-USDT" else hedge_df
        self.market_data_provider.get_candles_df = MagicMock(side_effect=by_pair)

    def test_missing_candle_drops_row_instead_of_shifting(self):
        # hedge is missing bar i=30; every other timestamp must stay paired correctly
        dom = self._frame(self._grid(60), [100.0 + i for i in range(60)])
        hedge_ts = [i * 60.0 for i in range(60) if i != 30]
        hedge = self._frame(hedge_ts, [200.0 + t / 60.0 for t in hedge_ts])
        merged = self.controller._aligned_close_frames(dom, hedge, now=3720.0)
        self.assertEqual([i * 60.0 for i in range(60) if i != 30],
                         list(merged["timestamp"]))
        # Rows AFTER the gap are still paired by timestamp, not by position
        row_31 = merged[merged["timestamp"] == 1860.0].iloc[0]
        self.assertEqual(131.0, row_31["close_dominant"])
        self.assertEqual(231.0, row_31["close_hedge"])
        row_59 = merged[merged["timestamp"] == 3540.0].iloc[0]
        self.assertEqual(159.0, row_59["close_dominant"])
        self.assertEqual(259.0, row_59["close_hedge"])

    def test_forming_bar_is_dropped(self):
        dom = self._frame(self._grid(60), [100.0] * 60)
        hedge = self._frame(self._grid(60), [200.0] * 60)
        # now=3599: the bar opened at 3540 closes at 3600 -> still forming -> dropped
        merged = self.controller._aligned_close_frames(dom, hedge, now=3599.0)
        self.assertEqual(3480.0, merged["timestamp"].iloc[-1])
        self.assertNotIn(3540.0, list(merged["timestamp"]))

    def test_nan_and_non_positive_closes_are_dropped(self):
        dom_closes = [100.0 + i for i in range(60)]
        dom_closes[10] = 0.0          # non-positive close -> unusable
        hedge_closes = [200.0 + i for i in range(60)]
        hedge_closes[20] = float("nan")
        dom = self._frame(self._grid(60), dom_closes)
        hedge = self._frame(self._grid(60), hedge_closes)
        merged = self.controller._aligned_close_frames(dom, hedge, now=3720.0)
        self.assertNotIn(600.0, list(merged["timestamp"]))    # i=10 gone from both
        self.assertNotIn(1200.0, list(merged["timestamp"]))   # i=20 gone from both
        self.assertFalse(merged["close_dominant"].isna().any())
        self.assertFalse(merged["close_hedge"].isna().any())
        self.assertEqual(58, len(merged))

    def test_duplicate_timestamps_keep_last(self):
        dom = self._frame([0.0, 60.0, 60.0, 120.0], [100.0, 101.0, 999.0, 102.0])
        hedge = self._frame([0.0, 60.0, 120.0], [200.0, 201.0, 202.0])
        merged = self.controller._aligned_close_frames(dom, hedge, now=500.0)
        self.assertEqual([0.0, 60.0, 120.0], list(merged["timestamp"]))
        self.assertEqual(999.0, merged[merged["timestamp"] == 60.0].iloc[0]["close_dominant"])

    def test_frames_without_timestamp_column_are_unusable(self):
        dom = pd.DataFrame({"close": [100.0] * 60})
        hedge = self._frame(self._grid(60), [200.0] * 60)
        merged = self.controller._aligned_close_frames(dom, hedge, now=3720.0)
        self.assertEqual(0, len(merged))

    async def test_offset_grids_yield_no_signal_not_positional_fallback(self):
        # Two 60-row frames of equal length whose grids never coincide (30s skew).
        # Positional pairing would happily regress them; the timestamp join must
        # instead report the signal unavailable.
        dom = self._frame(self._grid(60),
                          [100.0 * (1 + 0.001 * math.sin(i)) for i in range(60)])
        hedge = self._frame(self._grid(60, offset=30.0),
                            [50.0 * (1 + 0.001 * math.cos(i / 3)) for i in range(60)])
        self._set_candles(dom, hedge)
        self.market_data_provider.time = MagicMock(return_value=3720.0)
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNone(spread)
        self.assertIsNone(z_score)
        await self.controller.update_processed_data()
        self.assertEqual(0, self.controller.processed_data["signal"])

    def test_aligned_clean_frames_produce_finite_signal(self):
        # Fail-closed must not mean permanently dead: a clean aligned pair of feeds
        # still produces a usable (finite) spread/z-score.
        dom = self._frame(self._grid(60),
                          [100.0 * (1 + 0.001 * math.sin(i)) for i in range(60)])
        hedge = self._frame(self._grid(60),
                            [50.0 * (1 + 0.001 * math.cos(i / 3)) for i in range(60)])
        self._set_candles(dom, hedge)
        self.market_data_provider.time = MagicMock(return_value=3720.0)
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNotNone(spread)
        self.assertIsNotNone(z_score)
        self.assertTrue(math.isfinite(float(spread)))
        self.assertTrue(math.isfinite(float(z_score)))

    def test_nan_rows_dropped_but_enough_data_still_signals(self):
        # Two NaN closes drop 2 of 60 rows -> 58 aligned >= lookback 50: the NaN must
        # be excluded from the regression rather than raising or killing the signal.
        hedge_closes = [50.0 * (1 + 0.001 * math.cos(i / 3)) for i in range(60)]
        hedge_closes[3] = float("nan")
        hedge_closes[7] = float("nan")
        dom = self._frame(self._grid(60),
                          [100.0 * (1 + 0.001 * math.sin(i)) for i in range(60)])
        hedge = self._frame(self._grid(60), hedge_closes)
        self._set_candles(dom, hedge)
        self.market_data_provider.time = MagicMock(return_value=3720.0)
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNotNone(z_score)
        self.assertTrue(math.isfinite(float(z_score)))

    async def test_too_many_nan_rows_yield_no_signal(self):
        # 12 NaN closes -> 48 aligned rows < lookback 50 -> fail closed, no crash
        hedge_closes = [50.0 * (1 + 0.001 * math.cos(i / 3)) for i in range(60)]
        for i in range(0, 60, 5):
            hedge_closes[i] = float("nan")
        dom = self._frame(self._grid(60),
                          [100.0 * (1 + 0.001 * math.sin(i)) for i in range(60)])
        hedge = self._frame(self._grid(60), hedge_closes)
        self._set_candles(dom, hedge)
        self.market_data_provider.time = MagicMock(return_value=3720.0)
        spread, z_score = self.controller.get_spread_and_z_score()
        self.assertIsNone(spread)
        self.assertIsNone(z_score)
        await self.controller.update_processed_data()
        self.assertEqual(0, self.controller.processed_data["signal"])

    async def test_malformed_frame_does_not_stall_risk_management(self):
        # A junk frame must degrade to signal-unavailable while the global SL still
        # runs (GEN-1): the losing position below must still be closed.
        junk = pd.DataFrame({"weird": [1, 2, 3]})
        self._set_candles(junk, junk)
        self.controller.positions_held = [
            make_position_summary(DOMINANT, TradeType.BUY, Decimal("1"), Decimal("100"), Decimal("-20"))]
        await self.controller.update_processed_data()
        self.assertEqual(0, self.controller.processed_data["signal"])
        actions = self.controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        self.assertEqual(TradeType.SELL, creates[0].executor_config.side)
