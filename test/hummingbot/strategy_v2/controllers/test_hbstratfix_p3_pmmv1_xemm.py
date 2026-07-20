"""
hbstrat_fix Phase 3 tests — pmm_v1.py + pmm_simple.py + xemm_multiple_levels.py.

Covers:
- CDX-009 / CLA-302: pmm_v1 fill detection is per-executor-id terminal-transition based.
  A clean cancel at a level that once filled must NOT re-arm filled_order_delay (the old
  level-scan matched any retained POSITION_HOLD corpse), and a real fill surfacing while
  the executor is SHUTTING_DOWN (is_active excludes SHUTTING_DOWN) MUST arm the delay.
- CDX-012 / CLA-402: Decimal("NaN") reference price yields Decimal("0") and
  update_processed_data completes instead of raising InvalidOperation each tick.
- CLA-013: price_floor >= price_ceiling rejected (silent no-quote band);
  order_amount must be finite and positive.
- CLA-2b-005: refresh tolerance compares each refresh-age executor against the proposal
  price of ITS OWN level — a missing level (e.g. one in filled_order_delay) no longer
  forces a guaranteed length mismatch that churns every remaining order.
- CLA-001 (pmm_v1 / pmm_simple instances): omitted-field defaults are already-normalized
  typed values (defaults bypass mode="before" validators on the base model).
- CLA-303 / CLA-M02: xemm executors-imbalance guard is cumulative per executor id and
  survives eviction of >100 counted executors from the executors_info buffer.
- CLA-006: xemm level "amounts" are relative weights of total_amount_quote/2, and the
  documented weight semantics hold (scaling all weights equally changes nothing).

Expected values in these tests are derived from the findings/spec (HBSTRAT_FINDINGS.md),
not from running the implementation.
"""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from controllers.generic.pmm_v1 import PMMV1, PMMV1Config
from controllers.generic.xemm_multiple_levels import XEMMMultipleLevels, XEMMMultipleLevelsConfig
from controllers.market_making.pmm_simple import PMMSimpleConfig
from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def make_order_executor_info(executor_id: str, level_id: str, side: TradeType,
                             status: RunnableStatus = RunnableStatus.RUNNING,
                             close_type=None, price: Decimal = Decimal("99"),
                             custom_filled_quote: Decimal = Decimal("0"),
                             timestamp: float = 1000.0,
                             is_trading: bool = False) -> ExecutorInfo:
    """Build an ExecutorInfo shaped like a real pmm_v1 OrderExecutor snapshot.

    Mirrors OrderExecutor semantics: the public filled_amount_quote stays 0 for
    POSITION_HOLD; the exact fill accounting is exposed via custom_info.
    """
    config = OrderExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name="binance",
        trading_pair="BTC-USDT",
        side=side,
        amount=Decimal("1"),
        execution_strategy=ExecutionStrategy.LIMIT,
        price=price,
        level_id=level_id,
    )
    is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    return ExecutorInfo(
        id=executor_id,
        timestamp=timestamp,
        type="order_executor",
        status=status,
        config=config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=Decimal("0"),
        is_active=is_active,
        is_trading=is_trading,
        custom_info={
            "level_id": level_id,
            "side": side,
            "filled_amount_quote": custom_filled_quote,
        },
        close_type=close_type,
    )


class PMMV1HarnessMixin:
    def _make_config(self, **overrides):
        kwargs = dict(
            id="pmm-v1-test",
            connector_name="binance",
            trading_pair="BTC-USDT",
            order_amount=Decimal("1"),
            buy_spreads=[0.01],
            sell_spreads=[0.01],
            order_refresh_time=30,
            filled_order_delay=60,
        )
        kwargs.update(overrides)
        return PMMV1Config(**kwargs)

    def _make_controller(self, config=None):
        config = config or self._make_config()
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("100"))
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("10"))
        self.market_data_provider.quantize_order_amount = MagicMock(side_effect=lambda c, t, a: a)
        self.market_data_provider.quantize_order_price = MagicMock(side_effect=lambda c, t, p: p)
        controller = PMMV1(
            config=config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        controller.processed_data = {
            "reference_price": Decimal("100"),
            "price_ceiling": None,
            "price_floor": None,
            "buy_proposal_prices": [Decimal("99")],
            "sell_proposal_prices": [Decimal("101")],
        }
        return controller

    def _set_time(self, now: float):
        self.market_data_provider.time = MagicMock(return_value=now)


class TestPMMV1FillDetection(PMMV1HarnessMixin, IsolatedAsyncioWrapperTestCase):
    """CDX-009 / CLA-302."""

    def test_clean_cancel_at_level_that_once_filled_is_not_counted_as_fill(self):
        # Would-have-caught CDX-009: the old code rebuilt filled_levels from ALL retained
        # POSITION_HOLD corpses, so B's clean cancel at buy_0 re-armed the delay because
        # corpse A (an old fill at buy_0) was still in the buffer.
        controller = self._make_controller()

        # t=1000: old fill A at buy_0 terminates POSITION_HOLD -> delay armed to 1060
        corpse_a = make_order_executor_info(
            "a", "buy_0", TradeType.BUY, status=RunnableStatus.TERMINATED,
            close_type=CloseType.POSITION_HOLD, custom_filled_quote=Decimal("100"))
        controller.executors_info = [corpse_a]
        controller._detect_filled_executors()
        self.assertEqual(1060.0, controller._level_next_create_timestamps["buy_0"])

        # t=1100 (delay expired): replacement B is active at buy_0
        self._set_time(1100.0)
        active_b = make_order_executor_info("b", "buy_0", TradeType.BUY, timestamp=1100.0)
        controller.executors_info = [corpse_a, active_b]
        controller._detect_filled_executors()

        # t=1200: B is cancelled CLEANLY (EARLY_STOP, zero fill); corpse A still retained
        self._set_time(1200.0)
        corpse_b = make_order_executor_info(
            "b", "buy_0", TradeType.BUY, status=RunnableStatus.TERMINATED,
            close_type=CloseType.EARLY_STOP, custom_filled_quote=Decimal("0"), timestamp=1100.0)
        controller.executors_info = [corpse_a, corpse_b]
        controller._detect_filled_executors()

        # The clean cancel must NOT arm a new delay: the armed timestamp is still the
        # old 1060, and buy_0 is immediately eligible for a new order.
        self.assertEqual(1060.0, controller._level_next_create_timestamps["buy_0"])
        self.assertIn("buy_0", controller.get_levels_to_execute())

    def test_real_fill_during_shutting_down_arms_delay(self):
        # Would-have-caught CLA-302: is_active excludes SHUTTING_DOWN, so the old
        # active->inactive level transition fired before close_type was POSITION_HOLD
        # and the real fill's delay was skipped entirely.
        controller = self._make_controller()

        active_c = make_order_executor_info("c", "buy_0", TradeType.BUY)
        controller.executors_info = [active_c]
        controller._detect_filled_executors()
        self.assertNotIn("buy_0", controller._level_next_create_timestamps)

        # t=1010: C is SHUTTING_DOWN with a real fill visible in custom_info only
        # (public filled_amount_quote stays 0 for OrderExecutor POSITION_HOLD paths)
        self._set_time(1010.0)
        shutting_c = make_order_executor_info(
            "c", "buy_0", TradeType.BUY, status=RunnableStatus.SHUTTING_DOWN,
            close_type=None, custom_filled_quote=Decimal("100"))
        controller.executors_info = [shutting_c]
        controller._detect_filled_executors()
        self.assertEqual(1070.0, controller._level_next_create_timestamps["buy_0"])
        self.assertNotIn("buy_0", controller.get_levels_to_execute())

        # Terminal POSITION_HOLD later must NOT re-arm (id already resolved)
        self._set_time(1030.0)
        corpse_c = make_order_executor_info(
            "c", "buy_0", TradeType.BUY, status=RunnableStatus.TERMINATED,
            close_type=CloseType.POSITION_HOLD, custom_filled_quote=Decimal("100"))
        controller.executors_info = [corpse_c]
        controller._detect_filled_executors()
        self.assertEqual(1070.0, controller._level_next_create_timestamps["buy_0"])

    def test_zero_fill_shutdown_not_latched_partial_fill_still_detected(self):
        # A zero-fill SHUTTING_DOWN snapshot must not mark the id as resolved: the
        # cancel confirmation can still surface a partial fill at termination.
        controller = self._make_controller()
        shutting = make_order_executor_info(
            "d", "buy_0", TradeType.BUY, status=RunnableStatus.SHUTTING_DOWN,
            close_type=None, custom_filled_quote=Decimal("0"))
        controller.executors_info = [shutting]
        controller._detect_filled_executors()
        self.assertNotIn("buy_0", controller._level_next_create_timestamps)

        self._set_time(1020.0)
        corpse = make_order_executor_info(
            "d", "buy_0", TradeType.BUY, status=RunnableStatus.TERMINATED,
            close_type=CloseType.POSITION_HOLD, custom_filled_quote=Decimal("40"))
        controller.executors_info = [corpse]
        controller._detect_filled_executors()
        self.assertEqual(1080.0, controller._level_next_create_timestamps["buy_0"])

    def test_terminal_position_hold_fill_arms_delay(self):
        # Regression: the normal fill path still arms filled_order_delay.
        controller = self._make_controller()
        corpse = make_order_executor_info(
            "e", "sell_0", TradeType.SELL, status=RunnableStatus.TERMINATED,
            close_type=CloseType.POSITION_HOLD, custom_filled_quote=Decimal("100"))
        controller.executors_info = [corpse]
        controller._detect_filled_executors()
        self.assertEqual(1060.0, controller._level_next_create_timestamps["sell_0"])
        self.assertNotIn("sell_0", controller.get_levels_to_execute())

    def test_processed_ids_pruned_after_eviction(self):
        controller = self._make_controller()
        corpse = make_order_executor_info(
            "f", "buy_0", TradeType.BUY, status=RunnableStatus.TERMINATED,
            close_type=CloseType.EARLY_STOP)
        controller.executors_info = [corpse]
        controller._detect_filled_executors()
        self.assertIn("f", controller._processed_fill_executor_ids)
        controller.executors_info = []
        controller._detect_filled_executors()
        self.assertEqual(set(), controller._processed_fill_executor_ids)


class TestPMMV1ReferencePrice(PMMV1HarnessMixin, IsolatedAsyncioWrapperTestCase):
    """CDX-012 / CLA-402."""

    def test_decimal_nan_reference_price_returns_zero(self):
        # Would-have-caught CDX-012: the old guard only caught float NaN; Decimal("NaN")
        # escaped and raised InvalidOperation at `reference_price > 0` outside any try.
        controller = self._make_controller()
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))
        self.assertEqual(Decimal("0"), controller._get_reference_price())

    def test_float_nan_none_and_nonpositive_prices_return_zero(self):
        controller = self._make_controller()
        for bad in (float("nan"), None, Decimal("-1"), Decimal("0"), Decimal("Infinity")):
            self.market_data_provider.get_price_by_type = MagicMock(return_value=bad)
            self.assertEqual(Decimal("0"), controller._get_reference_price(), msg=f"price={bad!r}")

    def test_valid_price_passes_through(self):
        controller = self._make_controller()
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("123.45"))
        self.assertEqual(Decimal("123.45"), controller._get_reference_price())

    async def test_update_processed_data_completes_with_decimal_nan_price(self):
        # The controller must keep ticking (orders refreshable) instead of freezing.
        controller = self._make_controller()
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("NaN"))
        await controller.update_processed_data()
        self.assertEqual(Decimal("0"), controller.processed_data["reference_price"])
        # And no orders are created on a zero reference price
        self.assertEqual([], controller.create_actions_proposal())


class TestPMMV1ConfigValidators(IsolatedAsyncioWrapperTestCase):
    """CLA-013 + CLA-001 (pmm_v1 instances)."""

    def test_floor_at_or_above_ceiling_rejected(self):
        # Would-have-caught CLA-013: floor >= ceiling creates a silent no-quote band.
        with self.assertRaises(ValidationError):
            PMMV1Config(id="t", price_floor=Decimal("100"), price_ceiling=Decimal("50"))
        with self.assertRaises(ValidationError):
            PMMV1Config(id="t", price_floor=Decimal("100"), price_ceiling=Decimal("100"))

    def test_valid_and_disabled_price_bands_accepted(self):
        config = PMMV1Config(id="t", price_floor=Decimal("50"), price_ceiling=Decimal("100"))
        self.assertEqual(Decimal("50"), config.price_floor)
        # Defaults (-1 / -1 = both disabled) must stay accepted
        config = PMMV1Config(id="t")
        self.assertEqual(Decimal("-1"), config.price_floor)
        # A single enabled band is fine
        config = PMMV1Config(id="t", price_floor=Decimal("50"))
        self.assertEqual(Decimal("50"), config.price_floor)

    def test_order_amount_must_be_finite_positive(self):
        for bad in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(ValidationError, msg=f"order_amount={bad}"):
                PMMV1Config(id="t", order_amount=bad)
        config = PMMV1Config(id="t", order_amount=Decimal("0.01"))
        self.assertEqual(Decimal("0.01"), config.order_amount)

    def test_cla001_omitted_spreads_default_to_parsed_lists(self):
        # Would-have-caught CLA-001: the old default was the STRING "0.01" (defaults
        # bypass the mode="before" parser), quoting len("0.01")==4 garbage levels.
        config = PMMV1Config(id="t")
        self.assertEqual([0.01], config.buy_spreads)
        self.assertEqual([0.01], config.sell_spreads)
        self.assertIsInstance(config.buy_spreads, list)
        # Explicit string input still parses through the validator
        config = PMMV1Config(id="t", buy_spreads="0.01,0.02")
        self.assertEqual([0.01, 0.02], config.buy_spreads)


class TestPMMV1RefreshTolerance(PMMV1HarnessMixin, IsolatedAsyncioWrapperTestCase):
    """CLA-2b-005."""

    def _controller_with_two_buy_levels(self, tolerance=Decimal("0.01")):
        config = self._make_config(
            buy_spreads=[0.01, 0.02], sell_spreads=[],
            order_refresh_tolerance_pct=tolerance)
        controller = self._make_controller(config)
        # reference 100 -> buy proposals per spec: [100*(1-0.01), 100*(1-0.02)] = [99, 98]
        controller.processed_data = {
            "reference_price": Decimal("100"),
            "price_ceiling": None,
            "price_floor": None,
            "buy_proposal_prices": [Decimal("99"), Decimal("98")],
            "sell_proposal_prices": [],
        }
        self._set_time(1100.0)  # every executor below (timestamp 1000) is past refresh age
        return controller

    def test_missing_level_does_not_churn_within_tolerance_orders(self):
        # Would-have-caught CLA-2b-005: buy_0 is missing (filled_order_delay), so the old
        # code compared 1 current price against 2 proposal prices -> guaranteed length
        # mismatch -> refreshed (churned) the in-tolerance buy_1 order every cycle.
        controller = self._controller_with_two_buy_levels()
        in_tolerance = make_order_executor_info(
            "g", "buy_1", TradeType.BUY, price=Decimal("98.5"))  # |98-98.5|/98.5 ~ 0.51% < 1%
        controller.executors_info = [in_tolerance]
        self.assertEqual([], controller._executors_to_refresh())

    def test_out_of_tolerance_order_is_refreshed(self):
        controller = self._controller_with_two_buy_levels()
        drifted = make_order_executor_info(
            "h", "buy_1", TradeType.BUY, price=Decimal("90"))  # |98-90|/90 ~ 8.9% > 1%
        controller.executors_info = [drifted]
        actions = controller._executors_to_refresh()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], StopExecutorAction)
        self.assertEqual("h", actions[0].executor_id)
        self.assertTrue(actions[0].keep_position)

    def test_unmatchable_level_is_refreshed_not_retained(self):
        # An executor at a level with no configured proposal must be refreshed.
        controller = self._controller_with_two_buy_levels()
        orphan = make_order_executor_info(
            "i", "buy_5", TradeType.BUY, price=Decimal("99"))
        controller.executors_info = [orphan]
        actions = controller._executors_to_refresh()
        self.assertEqual(["i"], [a.executor_id for a in actions])

    def test_disabled_tolerance_refreshes_all_past_refresh(self):
        # Regression: tolerance -1 (disabled) keeps the unconditional refresh path.
        config = self._make_config(order_refresh_tolerance_pct=Decimal("-1"))
        controller = self._make_controller(config)
        self._set_time(1100.0)
        executor = make_order_executor_info("j", "buy_0", TradeType.BUY)
        controller.executors_info = [executor]
        actions = controller._executors_to_refresh()
        self.assertEqual(["j"], [a.executor_id for a in actions])


class TestPMMSimpleConfigDefaults(IsolatedAsyncioWrapperTestCase):
    """CLA-001 (pmm_simple instances)."""

    def test_omitted_defaults_are_normalized(self):
        # Would-have-caught CLA-001: the base-class defaults are the STRINGS
        # "0.01,0.02" / "HEDGE" and None amounts (sum(None) -> TypeError in
        # get_spreads_and_amounts_in_quote) because defaults bypass the parsers.
        config = PMMSimpleConfig(id="t")
        self.assertEqual([0.01, 0.02], config.buy_spreads)
        self.assertEqual([0.01, 0.02], config.sell_spreads)
        self.assertIsInstance(config.buy_spreads, list)
        self.assertEqual([Decimal("1"), Decimal("1")], config.buy_amounts_pct)
        self.assertEqual([Decimal("1"), Decimal("1")], config.sell_amounts_pct)
        self.assertIs(PositionMode.HEDGE, config.position_mode)

    def test_default_config_allocates_quote_amounts(self):
        # total weights = 4 -> each of the 4 levels gets 25% of total_amount_quote (100)
        config = PMMSimpleConfig(id="t")
        spreads, amounts = config.get_spreads_and_amounts_in_quote(TradeType.BUY)
        self.assertEqual([0.01, 0.02], spreads)
        self.assertEqual([Decimal("25"), Decimal("25")], amounts)

    def test_explicit_string_inputs_still_parse(self):
        config = PMMSimpleConfig(id="t", buy_spreads="0.01,0.02,0.03",
                                 sell_spreads="0.01", position_mode="ONEWAY")
        self.assertEqual([0.01, 0.02, 0.03], config.buy_spreads)
        self.assertEqual([0.01], config.sell_spreads)
        self.assertIs(PositionMode.ONEWAY, config.position_mode)
        # Amounts normalized to equal weights matching each side's level count
        self.assertEqual([Decimal("1")] * 3, config.buy_amounts_pct)
        self.assertEqual([Decimal("1")], config.sell_amounts_pct)


class TestXEMMCumulativeImbalance(IsolatedAsyncioWrapperTestCase):
    """CLA-303 / CLA-M02 + CLA-006."""

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
            max_executors_imbalance=1,
        )
        kwargs.update(overrides)
        return kwargs

    def _make_controller(self, config=None):
        config = config or XEMMMultipleLevelsConfig(**self._config_kwargs())
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1234.0)
        self.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("2"))
        with patch.object(ConnectorPair, "is_amm_connector", return_value=False):
            return XEMMMultipleLevels(
                config=config,
                market_data_provider=self.market_data_provider,
                actions_queue=AsyncMock(spec=asyncio.Queue),
            )

    def _make_xemm_executor(self, executor_id: str, maker_side: TradeType,
                            is_done: bool = True,
                            filled_amount_quote: Decimal = Decimal("10")) -> ExecutorInfo:
        config = XEMMExecutorConfig(
            id=executor_id,
            timestamp=1000.0,
            buying_market=ConnectorPair(connector_name="nonkyc", trading_pair="PEPE-USDT"),
            selling_market=ConnectorPair(connector_name="binance", trading_pair="PEPE-USDT"),
            maker_side=maker_side,
            order_amount=Decimal("10"),
            min_profitability=Decimal("0.002"),
            target_profitability=Decimal("0.003"),
            max_profitability=Decimal("0.01"),
        )
        return ExecutorInfo(
            id=executor_id,
            timestamp=1000.0,
            type="xemm_executor",
            status=RunnableStatus.TERMINATED if is_done else RunnableStatus.RUNNING,
            config=config,
            net_pnl_pct=Decimal("0"),
            net_pnl_quote=Decimal("0"),
            cum_fees_quote=Decimal("0"),
            filled_amount_quote=filled_amount_quote,
            is_active=not is_done,
            is_trading=False,
            custom_info={"side": maker_side},
            close_type=CloseType.COMPLETED if is_done else None,
        )

    @staticmethod
    def _buy_creates(actions):
        return [a for a in actions if isinstance(a, CreateExecutorAction)
                and a.executor_config.maker_side == TradeType.BUY]

    @staticmethod
    def _sell_creates(actions):
        return [a for a in actions if isinstance(a, CreateExecutorAction)
                and a.executor_config.maker_side == TradeType.SELL]

    def test_imbalance_survives_eviction_of_over_100_executors(self):
        # Would-have-caught CLA-303: with 105 filled buys and 104 filled sells the net
        # imbalance is +1 (buys halted). The old code re-derived the counts from
        # executors_info each tick, so evicting the corpses reset the imbalance to 0
        # and silently resumed the halted buy side.
        controller = self._make_controller()
        corpses = [self._make_xemm_executor(f"b{i}", TradeType.BUY) for i in range(105)]
        corpses += [self._make_xemm_executor(f"s{i}", TradeType.SELL) for i in range(104)]
        controller.executors_info = corpses

        actions = controller.determine_executor_actions()
        self.assertEqual([], self._buy_creates(actions))
        self.assertGreater(len(self._sell_creates(actions)), 0)

        # Evict ALL 209 counted executors from the buffer (archival)
        controller.executors_info = []
        actions = controller.determine_executor_actions()
        self.assertEqual([], self._buy_creates(actions),
                         "imbalance guard must not decay when counted executors evict")
        self.assertGreater(len(self._sell_creates(actions)), 0)

    def test_new_fill_after_eviction_rebalances_and_resumes(self):
        # The cumulative guard must keep counting: one more filled sell after the
        # eviction brings the imbalance back to 0 and buys resume.
        controller = self._make_controller()
        controller.executors_info = [self._make_xemm_executor("b0", TradeType.BUY)]
        actions = controller.determine_executor_actions()
        self.assertEqual([], self._buy_creates(actions))

        controller.executors_info = []  # b0 evicted; imbalance must stay 1
        actions = controller.determine_executor_actions()
        self.assertEqual([], self._buy_creates(actions))

        controller.executors_info = [self._make_xemm_executor("s0", TradeType.SELL)]
        actions = controller.determine_executor_actions()
        self.assertGreater(len(self._buy_creates(actions)), 0)

    def test_each_executor_counted_exactly_once(self):
        # Seeing the same done executor across many ticks must not inflate the count.
        controller = self._make_controller()
        corpse = self._make_xemm_executor("b0", TradeType.BUY)
        sell_corpse = self._make_xemm_executor("s0", TradeType.SELL)
        controller.executors_info = [corpse]
        for _ in range(5):
            controller.determine_executor_actions()
        self.assertEqual(1, controller._cumulative_filled_buys)
        # One filled sell nets it out -> buys resume (imbalance 0 < 1)
        controller.executors_info = [corpse, sell_corpse]
        actions = controller.determine_executor_actions()
        self.assertGreater(len(self._buy_creates(actions)), 0)

    def test_zero_fill_and_active_executors_not_counted(self):
        controller = self._make_controller()
        controller.executors_info = [
            self._make_xemm_executor("z0", TradeType.BUY, filled_amount_quote=Decimal("0")),
            self._make_xemm_executor("r0", TradeType.BUY, is_done=False),
        ]
        controller.determine_executor_actions()
        self.assertEqual(0, controller._cumulative_filled_buys)
        self.assertEqual(0, controller._cumulative_filled_sells)

    def test_cla006_level_weights_are_relative_not_absolute(self):
        # CLA-006: scaling every weight by 10x must not change order sizing — the values
        # are relative weights of total_amount_quote/2, not absolute amounts.
        controller_a = self._make_controller()
        actions_a = controller_a.determine_executor_actions()
        controller_b = self._make_controller(XEMMMultipleLevelsConfig(**self._config_kwargs(
            buy_levels_targets_amount="0.003,100-0.006,200",
            sell_levels_targets_amount="0.003,100-0.006,200")))
        actions_b = controller_b.determine_executor_actions()
        amounts_a = sorted(a.executor_config.order_amount for a in actions_a)
        amounts_b = sorted(a.executor_config.order_amount for a in actions_b)
        self.assertEqual(amounts_a, amounts_b)
        # And the sizing follows the spec: level0 buy = (10/30) * (120/2) / mid(2) = 10
        buy_amounts = sorted(a.executor_config.order_amount for a in self._buy_creates(actions_a))
        self.assertEqual([Decimal("10"), Decimal("20")], buy_amounts)
