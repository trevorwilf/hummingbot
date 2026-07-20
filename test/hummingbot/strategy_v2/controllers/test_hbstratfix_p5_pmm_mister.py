"""
hbstrat_fix Phase 5 tests — pmm_mister.py.

Covers:
- CLA-401 (High): a level_id=None executor (the global TP/SL exit OrderExecutor
  reports the key present-with-None) must not brick update_processed_data /
  determine_executor_actions; the exit order now carries a sentinel level_id
  that every level parser ignores — including the ledger-derived level union.
- CDX-008 / CDX-M02 (High): target_base_pct=0 (or an unordered min/target/max
  band) is rejected at config time, and a zero target that bypasses pydantic
  cannot reach the (target_position - held) / target_position division.
- CLA-301 (Med): the per-level entry cooldown arms ONLY on fills — a routine
  refresh CANCEL must not re-arm it (fill-not-cancel pattern), for both the
  gate (_analyze_by_level_id / get_levels_to_execute) and the display
  (_calculate_cooldown_status).
- CLA-003 (Med, PC): portfolio_allocation validated finite 0 < v <= 1
  (validation ONLY — the exposure denominator is unchanged per the finding).
- CLA-004 (Low, PC): min_skew bounded finite [0, 1]; the 1.0 "skew disabled"
  default is unchanged.
- CLA-409 (Low): all-zero amount weights are rejected at config time, and the
  runtime weight normalization degrades to zero-sized levels instead of a
  division-by-zero freeze.
- CLA-001 (cross-cutting, pmm_mister instance): omitted spreads/amounts
  defaults are real typed lists, not unparsed strings.

Expected values are derived from HBSTRAT_FINDINGS.md (F-3, F-4, CLA-301,
CLA-003, CLA-004, CLA-409) and the Phase 5 fix contract, not from running the
implementation.
"""
import asyncio
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from controllers.generic.pmm_mister import GLOBAL_EXIT_LEVEL_ID, PMMister, PMMisterConfig
from hummingbot.core.data_type.common import PositionAction, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.data_types import PositionSummary
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


CONNECTOR = "nonkyc"
PAIR = "XMR-USDT"


def make_order_executor_info(executor_id: str, side: TradeType, *,
                             level_id="__unset__", amount: Decimal = Decimal("1"),
                             filled_amount_quote: Decimal = Decimal("0"),
                             custom_info: dict = None,
                             timestamp: float = 1000.0,
                             status: RunnableStatus = RunnableStatus.RUNNING,
                             is_trading: bool = False) -> ExecutorInfo:
    """OrderExecutor-shaped ExecutorInfo. Pass level_id=None explicitly to model
    the pre-fix global exit order whose custom_info reports the key PRESENT with
    value None (the CLA-401 trigger)."""
    config = OrderExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name=CONNECTOR,
        trading_pair=PAIR,
        side=side,
        amount=amount,
        position_action=PositionAction.CLOSE,
        execution_strategy=ExecutionStrategy.MARKET,
        level_id=None if level_id == "__unset__" else level_id,
    )
    info_custom = {"side": side}
    if level_id != "__unset__":
        info_custom["level_id"] = level_id  # key present even when value is None
    if custom_info:
        info_custom.update(custom_info)
    is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    return ExecutorInfo(
        id=executor_id, timestamp=timestamp, type="order_executor", status=status,
        config=config, net_pnl_pct=Decimal("0"), net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"), filled_amount_quote=filled_amount_quote,
        is_active=is_active, is_trading=is_trading, custom_info=info_custom,
        close_timestamp=None if is_active else timestamp,
    )


def make_position_executor_info(executor_id: str, side: TradeType, level_id: str, *,
                                filled_amount_quote: Decimal = Decimal("0"),
                                custom_info: dict = None,
                                timestamp: float = 1000.0,
                                status: RunnableStatus = RunnableStatus.RUNNING,
                                is_trading: bool = False) -> ExecutorInfo:
    config = PositionExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name=CONNECTOR,
        trading_pair=PAIR,
        side=side,
        entry_price=Decimal("100"),
        amount=Decimal("1"),
        level_id=level_id,
    )
    info_custom = {"side": side, "level_id": level_id}
    if custom_info:
        info_custom.update(custom_info)
    is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    return ExecutorInfo(
        id=executor_id, timestamp=timestamp, type="position_executor", status=status,
        config=config, net_pnl_pct=Decimal("0"), net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"), filled_amount_quote=filled_amount_quote,
        is_active=is_active, is_trading=is_trading, custom_info=info_custom,
        close_timestamp=None if is_active else timestamp,
    )


def make_position_summary(side: TradeType, amount: Decimal, breakeven_price: Decimal,
                          unrealized_pnl_quote: Decimal = Decimal("0")) -> PositionSummary:
    return PositionSummary(
        connector_name=CONNECTOR,
        trading_pair=PAIR,
        volume_traded_quote=amount * breakeven_price,
        side=side,
        amount=amount,
        breakeven_price=breakeven_price,
        unrealized_pnl_quote=unrealized_pnl_quote,
        realized_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
    )


def make_config(**overrides) -> PMMisterConfig:
    kwargs = dict(
        id="pmm-mister-p5-test",
        connector_name=CONNECTOR,
        trading_pair=PAIR,
        total_amount_quote=Decimal("1000"),
        buy_spreads="0.0005",
        sell_spreads="0.0005",
        buy_amounts_pct="1",
        sell_amounts_pct="1",
        buy_cooldown_time=60,
        sell_cooldown_time=60,
    )
    kwargs.update(overrides)
    return PMMisterConfig(**kwargs)


def make_controller(config=None, now: float = 1000.0) -> PMMister:
    config = config or make_config()
    market_data_provider = MagicMock(spec=MarketDataProvider)
    market_data_provider.time = MagicMock(return_value=now)
    market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("100"))
    market_data_provider.quantize_order_amount = MagicMock(
        side_effect=lambda connector, pair, amount: amount)
    return PMMister(
        config=config,
        market_data_provider=market_data_provider,
        actions_queue=AsyncMock(spec=asyncio.Queue),
    )


class TestNullLevelIdExitOrder(IsolatedAsyncioWrapperTestCase):
    """CLA-401: level_id=None global-exit order must not brick the update loop."""

    async def test_null_level_id_executor_does_not_brick_update_loop(self):
        controller = make_controller(now=1000.0)
        controller.executors_info = [
            # The pre-fix global-exit shape: custom_info key present, value None.
            make_order_executor_info("exit-1", TradeType.SELL, level_id=None),
            make_position_executor_info("buy-1", TradeType.BUY, "buy_0"),
        ]
        # Pre-fix this raised AttributeError (None.startswith) inside
        # _calculate_cooldown_status / _calculate_refresh_tracking every tick.
        await controller.update_processed_data()
        self.assertEqual(Decimal("100"), controller.processed_data["reference_price"])
        self.assertIn("cooldown_status", controller.processed_data)
        # And the action loop (get_levels_to_execute over analyze_all_levels)
        # must not raise either.
        actions = controller.determine_executor_actions()
        self.assertIsInstance(actions, list)

    async def test_null_level_id_does_not_suppress_real_levels(self):
        controller = make_controller(now=1000.0)
        controller.executors_info = [
            make_order_executor_info("exit-1", TradeType.SELL, level_id=None),
        ]
        await controller.update_processed_data()
        actions = controller.determine_executor_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        created_levels = sorted(a.executor_config.level_id for a in creates)
        # Quoting continues (pre-fix the crash left processed_data empty and no
        # orders were ever proposed). With no position held, current_base_pct=0
        # is below min_base_pct, so only the buy side is quotable — by design.
        self.assertEqual(["buy_0"], created_levels)

    def test_global_exit_order_carries_sentinel_level_id(self):
        controller = make_controller(now=1000.0)
        # amount 1 @ breakeven 100 → amount_quote 100; −6 → −6% ≤ −5% stop loss
        controller.positions_held = [
            make_position_summary(TradeType.BUY, Decimal("1"), Decimal("100"), Decimal("-6"))]
        actions = controller.global_tp_sl_actions()
        creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
        self.assertEqual(1, len(creates))
        cfg = creates[0].executor_config
        self.assertEqual(GLOBAL_EXIT_LEVEL_ID, cfg.level_id)
        # The sentinel must be a level id the parsers ignore, not a buy_N/sell_N.
        self.assertFalse(controller._is_quoting_level_id(GLOBAL_EXIT_LEVEL_ID))

    async def test_sentinel_level_id_is_ignored_by_level_parsers(self):
        controller = make_controller(now=1000.0)
        controller.executors_info = [
            make_order_executor_info("exit-1", TradeType.SELL, level_id=GLOBAL_EXIT_LEVEL_ID),
        ]
        # Pre-fix a non-numeric level id reaching get_level_from_level_id raised
        # ValueError (int("exit")) in get_levels_to_execute.
        await controller.update_processed_data()
        analyzed = [a["level_id"] for a in controller.analyze_all_levels()]
        self.assertNotIn(GLOBAL_EXIT_LEVEL_ID, analyzed)
        actions = controller.determine_executor_actions()
        self.assertIsInstance(actions, list)

    def test_ledger_recorded_exit_fill_does_not_enter_level_analysis(self):
        # A FILLED global-exit order lands in the persisted ledger with the
        # sentinel level_id; the ledger-derived level union must filter it out,
        # or the next tick's get_levels_to_execute would raise on int("exit").
        controller = make_controller(now=1030.0)
        exit_fill = make_order_executor_info(
            "exit-filled", TradeType.SELL, level_id=GLOBAL_EXIT_LEVEL_ID,
            filled_amount_quote=Decimal("100"), status=RunnableStatus.TERMINATED)
        controller._trade_ledger.observe_executors([exit_fill], now=1000.0)
        controller.executors_info = []
        analyzed = [a["level_id"] for a in controller.analyze_all_levels()]
        self.assertNotIn(GLOBAL_EXIT_LEVEL_ID, analyzed)
        controller.processed_data = {
            "reference_price": Decimal("100"),
            "current_base_pct": Decimal("0.5"),
        }
        levels = controller.get_levels_to_execute()  # must not raise
        self.assertEqual(sorted(["buy_0", "sell_0"]), sorted(levels))


class TestTargetBasePctDivision(IsolatedAsyncioWrapperTestCase):
    """CDX-008 / CDX-M02: no zero denominator can reach the target division."""

    def test_target_base_pct_zero_rejected(self):
        for bad in (Decimal("0"), "0", 0):
            with self.assertRaises(ValidationError):
                make_config(target_base_pct=bad)

    def test_target_base_pct_out_of_range_rejected(self):
        for bad in (Decimal("1.5"), Decimal("-0.2"), Decimal("NaN"), float("inf")):
            with self.assertRaises(ValidationError):
                make_config(target_base_pct=bad, min_base_pct=Decimal("0"),
                            max_base_pct=Decimal("1"))

    def test_unordered_base_pct_band_rejected(self):
        with self.assertRaises(ValidationError):
            make_config(min_base_pct=Decimal("0.6"), target_base_pct=Decimal("0.5"))
        with self.assertRaises(ValidationError):
            make_config(target_base_pct=Decimal("0.8"), max_base_pct=Decimal("0.7"))

    def test_ordered_band_accepted(self):
        config = make_config(min_base_pct=Decimal("0.2"), target_base_pct=Decimal("0.4"),
                             max_base_pct=Decimal("0.9"))
        self.assertEqual(Decimal("0.4"), config.target_base_pct)

    async def test_update_loop_survives_zero_target_with_held_position(self):
        # A zero target that bypasses pydantic (direct mutation / hot-reload edge)
        # must not perma-fail update_processed_data — that killed the global SL.
        controller = make_controller(now=1000.0)
        object.__setattr__(controller.config, "target_base_pct", Decimal("0"))
        controller.positions_held = [
            make_position_summary(TradeType.BUY, Decimal("1"), Decimal("100"))]
        await controller.update_processed_data()  # pre-guard: DivisionByZero
        self.assertEqual(Decimal("0"), controller.processed_data["deviation"])
        self.assertEqual(Decimal("100"), controller.processed_data["reference_price"])


class TestCooldownArmsOnFillsOnly(unittest.TestCase):
    """CLA-301: refresh cancels must not arm the entry cooldown; fills must."""

    def _controller_with_closed_executor(self, filled_amount_quote,
                                         custom_info=None, now=1010.0):
        controller = make_controller(now=now)
        closed = make_position_executor_info(
            "closed-1", TradeType.BUY, "buy_0",
            filled_amount_quote=filled_amount_quote,
            custom_info={"open_order_last_update": 1000.0, **(custom_info or {})},
            status=RunnableStatus.TERMINATED)
        controller.executors_info = [closed]
        controller.processed_data = {
            "reference_price": Decimal("100"),
            "current_base_pct": Decimal("0.5"),
        }
        return controller

    def test_refresh_cancel_does_not_arm_cooldown(self):
        # A routine refresh CANCEL: closed executor, zero filled. 10s after the
        # cancel (cooldown 60s) the level must be quotable again — pre-fix it
        # went dark for the full cooldown after every refresh.
        controller = self._controller_with_closed_executor(Decimal("0"))
        analysis = controller._analyze_by_level_id("buy_0")
        self.assertIsNone(analysis["open_order_last_update"])
        self.assertIn("buy_0", controller.get_levels_to_execute())
        status = controller._calculate_cooldown_status(1010.0)
        self.assertFalse(status["buy"]["active"])

    def test_real_fill_arms_cooldown(self):
        controller = self._controller_with_closed_executor(Decimal("25"))
        analysis = controller._analyze_by_level_id("buy_0")
        self.assertEqual(1000.0, analysis["open_order_last_update"])
        self.assertNotIn("buy_0", controller.get_levels_to_execute())
        status = controller._calculate_cooldown_status(1010.0)
        self.assertTrue(status["buy"]["active"])
        self.assertEqual(50.0, status["buy"]["remaining_time"])

    def test_position_hold_custom_info_fill_arms_cooldown(self):
        # OrderExecutor POSITION_HOLD keeps the public filled_amount_quote at 0
        # and reports the fill via custom_info; that is still a fill.
        controller = self._controller_with_closed_executor(
            Decimal("0"), custom_info={"filled_amount_quote": Decimal("30")})
        analysis = controller._analyze_by_level_id("buy_0")
        self.assertEqual(1000.0, analysis["open_order_last_update"])
        self.assertNotIn("buy_0", controller.get_levels_to_execute())

    def test_cooldown_releases_after_expiry_with_fill(self):
        # Not over-restrictive: 61s after a real fill the level reopens.
        controller = self._controller_with_closed_executor(Decimal("25"), now=1061.0)
        self.assertIn("buy_0", controller.get_levels_to_execute())


class TestConfigValidators(unittest.TestCase):
    """CLA-003 (portfolio_allocation), CLA-004 (min_skew)."""

    def test_portfolio_allocation_rejections(self):
        for bad in (Decimal("0"), Decimal("-1"), Decimal("1.5"), Decimal("NaN"),
                    float("nan"), "garbage"):
            with self.assertRaises(ValidationError, msg=f"accepted {bad!r}"):
                make_config(portfolio_allocation=bad)

    def test_portfolio_allocation_accepts_full_and_partial(self):
        self.assertEqual(Decimal("1"), make_config(portfolio_allocation=Decimal("1")).portfolio_allocation)
        self.assertEqual(Decimal("0.25"), make_config(portfolio_allocation="0.25").portfolio_allocation)

    def test_min_skew_rejections(self):
        # min_skew=2 would DOUBLE every order (unbounded multiplier).
        for bad in (Decimal("2"), Decimal("1.01"), Decimal("-0.2"), Decimal("NaN")):
            with self.assertRaises(ValidationError, msg=f"accepted {bad!r}"):
                make_config(min_skew=bad)

    def test_min_skew_default_unchanged_and_valid_values_accepted(self):
        # The 1.0 "skew disabled" default is intentional — do not change it.
        self.assertEqual(Decimal("1.0"), make_config().min_skew)
        self.assertEqual(Decimal("0.3"), make_config(min_skew="0.3").min_skew)
        self.assertEqual(Decimal("0"), make_config(min_skew=Decimal("0")).min_skew)


class TestZeroWeightGuard(unittest.TestCase):
    """CLA-409: all-zero amount weights → div-by-zero freeze."""

    def test_all_zero_weights_rejected_at_config_time(self):
        with self.assertRaises(ValidationError):
            make_config(buy_amounts_pct="0", sell_amounts_pct="0")

    def test_one_sided_zero_weights_accepted(self):
        # Zeroing ONE side is a legitimate one-sided setup; only all-zero divides by 0.
        config = make_config(buy_amounts_pct="0", sell_amounts_pct="1")
        spreads, amounts = config.get_spreads_and_amounts_in_quote(TradeType.SELL)
        # sell weight 1 / total 1 → 1 * 1000 * 0.1 = 100
        self.assertEqual([Decimal("100")], amounts)
        _, buy_amounts = config.get_spreads_and_amounts_in_quote(TradeType.BUY)
        self.assertEqual([Decimal("0")], buy_amounts)

    def test_negative_weight_element_rejected(self):
        with self.assertRaises(ValidationError):
            make_config(buy_spreads="0.001,0.002", buy_amounts_pct="-1,2")

    def test_runtime_guard_degrades_to_zero_amounts(self):
        # All-zero weights bypassing pydantic must NOT raise DivisionByZero —
        # they degrade to zero-sized (skipped) levels.
        config = make_config()
        object.__setattr__(config, "buy_amounts_pct", [Decimal("0")])
        object.__setattr__(config, "sell_amounts_pct", [Decimal("0")])
        spreads, amounts = config.get_spreads_and_amounts_in_quote(TradeType.BUY)
        self.assertEqual([0.0005], spreads)
        self.assertEqual([Decimal("0")], amounts)


class TestOmittedDefaultsNormalized(unittest.TestCase):
    """CLA-001 (pmm_mister instance): omitted defaults must be typed lists."""

    def _minimal_config(self):
        # NO spreads/amounts passed: pydantic v2 skips mode="before" validators
        # on defaults, so pre-fix these fields stayed raw strings ("0.0005" →
        # len 6 phantom levels; sum("1") → TypeError).
        return PMMisterConfig(
            id="pmm-mister-p5-defaults",
            connector_name=CONNECTOR,
            trading_pair=PAIR,
            total_amount_quote=Decimal("1000"),
        )

    def test_spread_and_amount_defaults_are_lists(self):
        config = self._minimal_config()
        self.assertEqual([0.0005], config.buy_spreads)
        self.assertEqual([0.0005], config.sell_spreads)
        self.assertEqual([Decimal("1")], config.buy_amounts_pct)
        self.assertEqual([Decimal("1")], config.sell_amounts_pct)

    def test_default_config_produces_sane_level_amounts(self):
        config = self._minimal_config()
        spreads, amounts = config.get_spreads_and_amounts_in_quote(TradeType.BUY)
        self.assertEqual([0.0005], spreads)
        # weight 1 / total 2 → 0.5 * 1000 * 0.1 (default allocation) = 50
        self.assertEqual([Decimal("50")], amounts)


if __name__ == "__main__":
    unittest.main()
