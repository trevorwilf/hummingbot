"""Executor simulator correctness tests (V2 strategy fixes phase 7 — C1, C2, C3).

Under test (synthetic dataframes; silent-wrong-result bugs):
- C1: each DCA stage's fills/PnL start at that stage's OWN entry timestamp — the old
  code backdated every stage to the last stage's entry via a leaked loop variable.
- C2: the position simulator charges round-trip fees (entry AND exit legs), consistent
  with the doubled round-trip volume at the close row.
- C3: a trailing-stop config whose trigger never fires completes without KeyError and
  without a trailing exit; a triggered trailing stop still fires (regression).
"""
import unittest
from decimal import Decimal

import pandas as pd

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy_v2.backtesting.executors_simulator.dca_executor_simulator import DCAExecutorSimulator
from hummingbot.strategy_v2.backtesting.executors_simulator.position_executor_simulator import (
    PositionExecutorSimulator,
)
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TrailingStop,
    TripleBarrierConfig,
)
from hummingbot.strategy_v2.models.executors import CloseType

START_TS = 1000
STEP = 60


def make_df(closes):
    # Float timestamps, like the real backtesting data provider produces — the
    # simulators slice with config.timestamp (float) offsets.
    timestamps = [float(START_TS + i * STEP) for i in range(len(closes))]
    return pd.DataFrame({
        "timestamp": timestamps,
        "close": [float(c) for c in closes],
        "low": [float(c) * 0.999 for c in closes],
        "high": [float(c) * 1.001 for c in closes],
    }, index=timestamps)


def market_barriers(**overrides):
    kwargs = dict(
        open_order_type=OrderType.MARKET,
        stop_loss_order_type=OrderType.MARKET,
        time_limit_order_type=OrderType.MARKET,
    )
    kwargs.update(overrides)
    return TripleBarrierConfig(**kwargs)


class TestDCAExecutorSimulator(unittest.TestCase):
    """C1 — per-stage entry timestamps."""

    def test_each_stage_fills_from_its_own_entry_timestamp(self):
        # Level prices 100 and 90: stage 0 enters at index 1 (close 100), stage 1 at
        # index 4 (close 90).
        closes = [101, 100, 95, 92, 90, 88, 87]
        df = make_df(closes)
        config = DCAExecutorConfig(
            timestamp=START_TS, connector_name="binance", trading_pair="ETH-USDT",
            side=TradeType.BUY, amounts_quote=[Decimal("100"), Decimal("200")],
            prices=[Decimal("100"), Decimal("90")], mode=DCAMode.MAKER,
            stop_loss=Decimal("0.5"),
        )
        simulation = DCAExecutorSimulator().simulate(df, config, trade_cost=0.001)
        result = simulation.executor_simulation

        entry0_ts = START_TS + 1 * STEP
        entry1_ts = START_TS + 4 * STEP

        # Before stage 0's entry: nothing filled.
        self.assertEqual(0.0, result.loc[START_TS, "filled_amount_quote"])
        # Between the two entries: ONLY stage 0's amount (the old bug backdated
        # stage 0's fills to stage 1's entry, leaving this window at 0).
        for ts in (entry0_ts, entry0_ts + STEP, entry0_ts + 2 * STEP):
            self.assertEqual(100.0, result.loc[ts, "filled_amount_quote"],
                             f"stage-0-only window wrong at {ts}")
            self.assertEqual(0.0, result.loc[ts, f"filled_amount_quote_{1}"])
        # From stage 1's entry: both amounts.
        self.assertEqual(300.0, result.loc[entry1_ts, "filled_amount_quote"])
        self.assertEqual(300.0, result.loc[entry1_ts + STEP, "filled_amount_quote"])

    def test_stage0_pnl_accrues_before_stage1_enters(self):
        closes = [101, 100, 95, 92, 90, 88, 87]
        df = make_df(closes)
        config = DCAExecutorConfig(
            timestamp=START_TS, connector_name="binance", trading_pair="ETH-USDT",
            side=TradeType.BUY, amounts_quote=[Decimal("100"), Decimal("200")],
            prices=[Decimal("100"), Decimal("90")], mode=DCAMode.MAKER,
            stop_loss=Decimal("0.5"),
        )
        simulation = DCAExecutorSimulator().simulate(df, config, trade_cost=0.0)
        result = simulation.executor_simulation
        # Price dropped 100 -> 95 with only stage 0 filled: its PnL column must be
        # negative in that window (the old bug left it at 0 until stage 1's entry).
        self.assertLess(result.loc[START_TS + 2 * STEP, "net_pnl_quote_0"], 0.0)


class TestPositionExecutorSimulator(unittest.TestCase):
    """C2 + C3."""

    def _config(self, barriers, amount="1"):
        return PositionExecutorConfig(
            timestamp=START_TS, connector_name="binance", trading_pair="ETH-USDT",
            side=TradeType.BUY, entry_price=Decimal("100"), amount=Decimal(amount),
            triple_barrier_config=barriers,
        )

    def test_round_trip_fees_charged_on_both_legs(self):
        trade_cost = 0.001
        closes = [100, 103, 106]
        config = self._config(market_barriers(take_profit=Decimal("0.05")))
        simulation = PositionExecutorSimulator().simulate(make_df(closes), config, trade_cost=trade_cost)
        result = simulation.executor_simulation

        self.assertEqual(CloseType.TAKE_PROFIT, simulation.close_type)
        notional = 1.0 * 100.0  # amount * entry price (single leg)
        # Fees = 2 legs x trade_cost x notional (volume at the close row is 2x notional).
        self.assertAlmostEqual(2 * trade_cost * notional, result["cum_fees_quote"].iloc[-1], places=10)
        self.assertAlmostEqual(2 * notional, result["filled_amount_quote"].iloc[-1], places=10)
        # net_pnl_pct is net of BOTH legs: at the entry row (zero return) it equals -2x cost.
        self.assertAlmostEqual(-2 * trade_cost, result["net_pnl_pct"].loc[START_TS], places=10)

    def test_trailing_stop_never_triggered_completes_without_error(self):
        # C3: flat prices, activation far away — the 'ts' mask is all-False. This used
        # to KeyError on df_filtered['ts'] depending on the pandas version.
        closes = [100] * 5
        config = self._config(market_barriers(
            time_limit=4 * STEP,
            trailing_stop=TrailingStop(activation_price=Decimal("0.5"), trailing_delta=Decimal("0.01")),
        ))
        simulation = PositionExecutorSimulator().simulate(make_df(closes), config, trade_cost=0.0)
        self.assertEqual(CloseType.TIME_LIMIT, simulation.close_type)
        self.assertTrue(simulation.executor_simulation["ts"].isna().all())

    def test_trailing_stop_still_fires_when_triggered(self):
        # Regression: rise past activation (1%), then fall below the trailing level.
        closes = [100, 103, 100, 100, 100]
        config = self._config(market_barriers(
            trailing_stop=TrailingStop(activation_price=Decimal("0.01"), trailing_delta=Decimal("0.005")),
        ))
        simulation = PositionExecutorSimulator().simulate(make_df(closes), config, trade_cost=0.0)
        self.assertEqual(CloseType.TRAILING_STOP, simulation.close_type)


if __name__ == "__main__":
    unittest.main()
