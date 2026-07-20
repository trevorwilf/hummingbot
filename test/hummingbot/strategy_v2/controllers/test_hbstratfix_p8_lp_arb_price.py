"""
hbstrat_fix Phase 8 tests — lp_rebalancer + arbitrage_controller (external-price + state-on-fail).

Covers:
- CDX-006 / CLA-408: pool price now carries a fetch timestamp; a price stale beyond
  max_pool_price_age_seconds (or with no provenance at all) is treated as unavailable and
  position creation is skipped with a WARNING (fail-closed). A consecutive-failure breaker
  pauses creation after N executors terminate without ever opening a position, so a
  failing on-chain create cannot burn gas/RPC every tick.
- CLA-306 / CLA-2b-007: the pending rebalance side and the closed-amount clamp are consumed
  only once the created executor is observed — a failed create retries with the same side
  and sizing instead of falling back to config.side (doubling down) at full configured size.
- CLA-009: trading_pair / pool_address must be non-empty (enforced at the model level so the
  check also fires on omitted defaults), position_width_pct must be positive.
- CLA-2b-002 / CLA-307 / CLA-407: gas_conversion_price gets the GEN-12 finite/>0 guard and
  creation is skipped while it is unavailable; gas-token discovery completes BEFORE rate
  sources are registered; the registered gas rate pair is the one actually queried
  (base-gas, not gas-quote).
- CLA-007: min_profitability / delay_between_executors / max_executors_imbalance validators
  (validators only — the "~2x capital" framing was rejected).

Expected values are derived from the findings/spec, never from running the implementation.
"""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from controllers.generic.arbitrage_controller import ArbitrageController, ArbitrageControllerConfig
from controllers.generic.lp_rebalancer.lp_rebalancer import LPRebalancer, LPRebalancerConfig
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.lp_executor.data_types import LPExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

LP_LOGGER = "controllers.generic.lp_rebalancer.lp_rebalancer"


def make_lp_executor_info(executor_id="lp1", is_active=False, custom_info=None,
                          close_type=CloseType.FAILED):
    executor_config = LPExecutorConfig(
        timestamp=1000.0,
        connector_name="meteora/clmm",
        trading_pair="SOL-USDC",
        pool_address="pool123",
        lower_price=Decimal("140"),
        upper_price=Decimal("160"),
        quote_amount=Decimal("50"),
        side=1,
    )
    return ExecutorInfo(
        id=executor_id,
        timestamp=1000.0,
        type="lp_executor",
        status=RunnableStatus.RUNNING if is_active else RunnableStatus.TERMINATED,
        config=executor_config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=Decimal("0"),
        is_active=is_active,
        is_trading=False,
        custom_info=custom_info or {},
        close_type=None if is_active else close_type,
        close_timestamp=None if is_active else 1000.0,
    )


class LPRebalancerTestBase(IsolatedAsyncioWrapperTestCase):

    def _make_controller(self, **config_overrides):
        config_kwargs = dict(
            id="test-lp-p8",
            connector_name="meteora/clmm",
            trading_pair="SOL-USDC",
            pool_address="pool123",
            total_amount_quote=Decimal("50"),
            side=1,  # BUY
        )
        config_kwargs.update(config_overrides)
        config = LPRebalancerConfig(**config_kwargs)
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        self.market_data_provider.get_balance = MagicMock(return_value=Decimal("100"))
        controller = LPRebalancer(
            config=config,
            market_data_provider=self.market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def _set_fresh_price(self, controller, price="150", timestamp=None):
        controller._pool_price = Decimal(price)
        controller._pool_price_timestamp = (
            self.market_data_provider.time() if timestamp is None else timestamp)


class TestLPRebalancerStalePrice(LPRebalancerTestBase):
    """CDX-006 / CLA-408 — stale pool price must not size a new position."""

    def test_stale_pool_price_skips_creation(self):
        # Would-have-caught: old code accepted any non-None/non-zero cached price,
        # however old — a terminated executor's replacement was sized (amounts AND
        # bounds) around an arbitrarily stale price.
        controller = self._make_controller()
        controller._pool_price = Decimal("150")
        controller._pool_price_timestamp = 880.0  # age 120s > max 60s
        with self.assertLogs(LP_LOGGER, level="WARNING") as logs:
            actions = controller.determine_executor_actions()
        self.assertEqual([], actions)
        self.assertTrue(any("stale" in rec.lower() or "unavailable" in rec.lower()
                            for rec in logs.output))

    def test_price_without_timestamp_skips_creation(self):
        # A price with no fetch provenance must be treated as unavailable, not fresh.
        controller = self._make_controller()
        controller._pool_price = Decimal("150")
        controller._pool_price_timestamp = None
        self.assertEqual([], controller.determine_executor_actions())

    def test_no_price_skips_creation(self):
        controller = self._make_controller()
        self.assertEqual([], controller.determine_executor_actions())

    def test_fresh_pool_price_creates_position(self):
        # Regression guard: the gate must not be over-tight — a fresh price creates.
        controller = self._make_controller()
        self._set_fresh_price(controller)
        actions = controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertIsInstance(actions[0], CreateExecutorAction)
        self.assertEqual(Decimal("50"), actions[0].executor_config.quote_amount)

    def test_age_equal_to_max_is_still_fresh(self):
        # Spec: "stale-BEYOND-max-age as unavailable" — age == max is still usable.
        controller = self._make_controller()
        controller._pool_price = Decimal("150")
        controller._pool_price_timestamp = 940.0  # age exactly 60s == max
        actions = controller.determine_executor_actions()
        self.assertEqual(1, len(actions))

    def test_configured_max_age_is_honored(self):
        controller = self._make_controller(max_pool_price_age_seconds=200)
        controller._pool_price = Decimal("150")
        controller._pool_price_timestamp = 880.0  # age 120s <= configured 200s
        self.assertEqual(1, len(controller.determine_executor_actions()))

    async def test_update_processed_data_stamps_fetch_time(self):
        controller = self._make_controller()
        self.market_data_provider.time = MagicMock(return_value=1234.0)
        connector = MagicMock()
        connector.get_pool_info_by_address = AsyncMock(
            return_value=SimpleNamespace(price="155.5"))
        self.market_data_provider.get_connector = MagicMock(return_value=connector)
        controller._pool_price_fetch_failures = 5
        await controller.update_processed_data()
        self.assertEqual(Decimal("155.5"), controller._pool_price)
        self.assertEqual(1234.0, controller._pool_price_timestamp)
        self.assertEqual(0, controller._pool_price_fetch_failures)

    async def test_fetch_failure_retains_price_but_does_not_refresh_timestamp(self):
        # A failed fetch keeps the old price/timestamp so it ages out naturally —
        # it must NOT re-stamp the stale value as fresh.
        controller = self._make_controller()
        controller._pool_price = Decimal("150")
        controller._pool_price_timestamp = 900.0
        connector = MagicMock()
        connector.get_pool_info_by_address = AsyncMock(side_effect=Exception("rpc down"))
        self.market_data_provider.get_connector = MagicMock(return_value=connector)
        await controller.update_processed_data()
        self.assertEqual(Decimal("150"), controller._pool_price)
        self.assertEqual(900.0, controller._pool_price_timestamp)
        self.assertEqual(1, controller._pool_price_fetch_failures)

    async def test_non_finite_fetched_price_is_a_failure(self):
        controller = self._make_controller()
        connector = MagicMock()
        connector.get_pool_info_by_address = AsyncMock(
            return_value=SimpleNamespace(price="NaN"))
        self.market_data_provider.get_connector = MagicMock(return_value=connector)
        await controller.update_processed_data()
        self.assertIsNone(controller._pool_price)
        self.assertIsNone(controller._pool_price_timestamp)
        self.assertEqual(1, controller._pool_price_fetch_failures)


class TestLPRebalancerStateOnFail(LPRebalancerTestBase):
    """CLA-306 / CLA-2b-007 — a failed create must not consume the side or the clamp."""

    def test_failed_create_preserves_rebalance_side(self):
        # Would-have-caught: old code cleared _pending_rebalance_side BEFORE
        # _create_executor_config could return None; a retry then fell back to
        # config.side (BUY) — doubling down instead of rebalancing to SELL.
        controller = self._make_controller()  # config.side == 1 (BUY)
        controller._pending_rebalance = True
        controller._pending_rebalance_side = 2  # SELL rebalance intent
        controller._pool_price = Decimal("150")
        controller._pool_price_timestamp = 880.0  # stale -> create fails

        self.assertEqual([], controller.determine_executor_actions())
        self.assertTrue(controller._pending_rebalance)
        self.assertEqual(2, controller._pending_rebalance_side)

        # Price recovers: the retry must still use the SELL intent, not config.side
        self._set_fresh_price(controller)
        actions = controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(2, actions[0].executor_config.side)

    def test_failed_create_preserves_closed_amount_clamp(self):
        # Would-have-caught: _calculate_amounts consumed _last_closed_* before the
        # bounds check could fail, so the NEXT attempt over-sized to the full total.
        # buy_price_min == buy_price_max makes the BUY bounds invalid
        # (upper = 100 * (1 - offset) < lower = 100) so the create fails after
        # _calculate_amounts already ran.
        controller = self._make_controller(
            buy_price_min=Decimal("100"), buy_price_max=Decimal("100"))
        self._set_fresh_price(controller)
        controller._last_closed_quote_amount = Decimal("10")
        controller._last_closed_quote_fee = Decimal("0.2")

        self.assertEqual([], controller.determine_executor_actions())
        self.assertEqual(Decimal("10"), controller._last_closed_quote_amount)
        self.assertEqual(Decimal("0.2"), controller._last_closed_quote_fee)

        # Bounds become valid again: the retry must still clamp to 10.2, not 50
        controller.config.buy_price_min = None
        controller.config.buy_price_max = None
        actions = controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("10.2"), actions[0].executor_config.quote_amount)

    def test_intent_and_clamp_consumed_on_executor_observation(self):
        # Once the created executor is actually observed, the one-shot state must be
        # consumed so it cannot bleed into a later, unrelated creation cycle.
        controller = self._make_controller()
        controller._pending_rebalance = True
        controller._pending_rebalance_side = 2
        controller._last_closed_quote_amount = Decimal("10")
        controller._last_closed_quote_fee = Decimal("0.2")
        controller._current_executor_id = None
        controller.executors_info = [make_lp_executor_info("lp-new", is_active=True)]

        controller.determine_executor_actions()

        self.assertEqual("lp-new", controller._current_executor_id)
        self.assertFalse(controller._pending_rebalance)
        self.assertIsNone(controller._pending_rebalance_side)
        self.assertIsNone(controller._last_closed_quote_amount)
        self.assertIsNone(controller._last_closed_quote_fee)


class TestLPRebalancerFailureBreaker(LPRebalancerTestBase):
    """CDX-006 — consecutive create failures latch a creation cooldown."""

    def _run_failed_cycle(self, controller, executor_id):
        controller._current_executor_id = executor_id
        controller.executors_info = [make_lp_executor_info(executor_id, custom_info={
            "base_amount": "0", "quote_amount": "0", "base_fee": "0", "quote_fee": "0"})]
        self._set_fresh_price(controller)
        return controller.determine_executor_actions()

    def test_breaker_trips_after_consecutive_failed_executors(self):
        # Would-have-caught: nothing bounded the per-tick recreate loop — a create
        # failing on-chain burned gas/RPC every single tick.
        controller = self._make_controller()

        self.assertEqual(1, len(self._run_failed_cycle(controller, "lp1")))
        self.assertEqual(1, len(self._run_failed_cycle(controller, "lp2")))
        # Third consecutive failure latches the cooldown: no create this tick
        with self.assertLogs(LP_LOGGER, level="WARNING") as logs:
            actions = self._run_failed_cycle(controller, "lp3")
        self.assertEqual([], actions)
        self.assertTrue(any("pausing creation" in rec for rec in logs.output))
        self.assertEqual(1300.0, controller._create_cooldown_until)  # 1000 + 300s

        # Still inside the cooldown: no terminated executor, still no create
        controller.executors_info = []
        self.assertEqual([], controller.determine_executor_actions())

        # After the cooldown expires, creation resumes (with a fresh price)
        self.market_data_provider.time = MagicMock(return_value=1301.0)
        self._set_fresh_price(controller)
        self.assertEqual(1, len(controller.determine_executor_actions()))

    def test_successful_position_resets_breaker(self):
        controller = self._make_controller()
        self._run_failed_cycle(controller, "lp1")
        self._run_failed_cycle(controller, "lp2")
        self.assertEqual(2, controller._consecutive_failed_executors)

        # A termination that DID hold a position resets the streak (and clamps sizing)
        controller._current_executor_id = "lp3"
        controller.executors_info = [make_lp_executor_info("lp3", custom_info={
            "base_amount": "0", "quote_amount": "30", "base_fee": "0", "quote_fee": "1"})]
        self._set_fresh_price(controller)
        actions = controller.determine_executor_actions()
        self.assertEqual(1, len(actions))
        self.assertEqual(Decimal("31"), actions[0].executor_config.quote_amount)
        self.assertEqual(0, controller._consecutive_failed_executors)
        self.assertEqual(0.0, controller._create_cooldown_until)


class TestLPRebalancerConfigValidation(IsolatedAsyncioWrapperTestCase):
    """CLA-009 — required-field and width validators (validation only; /100 unit kept)."""

    def _config_kwargs(self, **overrides):
        kwargs = dict(
            id="test-lp-cfg",
            connector_name="meteora/clmm",
            trading_pair="SOL-USDC",
            pool_address="pool123",
            total_amount_quote=Decimal("50"),
        )
        kwargs.update(overrides)
        return kwargs

    def test_valid_config_accepted(self):
        config = LPRebalancerConfig(**self._config_kwargs())
        self.assertEqual("SOL-USDC", config.trading_pair)
        self.assertEqual(Decimal("0.5"), config.position_width_pct)

    def test_empty_trading_pair_rejected(self):
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**self._config_kwargs(trading_pair=""))
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**self._config_kwargs(trading_pair="   "))

    def test_empty_pool_address_rejected(self):
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**self._config_kwargs(pool_address=""))

    def test_omitted_trading_pair_rejected(self):
        # CLA-001 mechanism: field defaults bypass mode="before" validators — the
        # non-empty check is model-level so an OMITTED field cannot slip through as "".
        kwargs = self._config_kwargs()
        del kwargs["trading_pair"]
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**kwargs)

    def test_omitted_pool_address_rejected(self):
        kwargs = self._config_kwargs()
        del kwargs["pool_address"]
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**kwargs)

    def test_non_positive_position_width_rejected(self):
        for bad in ("0", "-0.5", "NaN"):
            with self.assertRaises(ValidationError):
                LPRebalancerConfig(**self._config_kwargs(position_width_pct=Decimal(bad)))

    def test_non_positive_breaker_params_rejected(self):
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**self._config_kwargs(max_pool_price_age_seconds=0))
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**self._config_kwargs(create_failure_breaker_count=0))
        with self.assertRaises(ValidationError):
            LPRebalancerConfig(**self._config_kwargs(create_failure_cooldown_seconds=-1))


class ArbitrageTestBase(IsolatedAsyncioWrapperTestCase):

    CEX_PAIR = ConnectorPair(connector_name="binance", trading_pair="ETH-USDT")
    AMM_PAIR = ConnectorPair(connector_name="amm_dex", trading_pair="ETH-USDC")

    @staticmethod
    def _amm_patch():
        return patch.object(ConnectorPair, "is_amm_connector",
                            new=lambda self: self.connector_name == "amm_dex")

    def _make_controller(self, exchange_pair_2=None):
        config = ArbitrageControllerConfig(
            id="test-arb-p8",
            total_amount_quote=Decimal("100"),
            exchange_pair_1=self.CEX_PAIR,
            exchange_pair_2=exchange_pair_2 or self.AMM_PAIR,
            rate_connector="binance",
        )
        self.market_data_provider = MagicMock(spec=MarketDataProvider)
        self.market_data_provider.time = MagicMock(return_value=1000.0)
        # Construct with is_amm False so __init__ does not touch the gateway;
        # gas-token cache is seeded explicitly per test.
        with patch.object(ConnectorPair, "is_amm_connector", return_value=False):
            controller = ArbitrageController(
                config=config,
                market_data_provider=self.market_data_provider,
                actions_queue=AsyncMock(spec=asyncio.Queue),
            )
        return controller

    def _create_action(self, controller, buying, selling, rates, quantized=Decimal("1")):
        self.market_data_provider.get_rate = MagicMock(side_effect=lambda pair: rates.get(pair))
        self.market_data_provider.quantize_order_amount = MagicMock(return_value=quantized)
        with self._amm_patch():
            return controller.create_arbitrage_executor_action(buying, selling)


class TestArbitrageGasGuard(ArbitrageTestBase):
    """CLA-2b-002 / CLA-307 — gas_conversion_price gets the GEN-12 guard."""

    def test_missing_gas_token_skips_creation(self):
        # Would-have-caught: with the gas token undiscovered, old code shipped
        # gas_conversion_price=None into the executor, which divides by it and wedges.
        controller = self._make_controller()
        action = self._create_action(controller, self.AMM_PAIR, self.CEX_PAIR,
                                     rates={"ETH-USDT": Decimal("100")})
        self.assertIsNone(action)

    def test_nan_gas_rate_skips_creation(self):
        controller = self._make_controller()
        controller._gas_token_cache["amm_dex"] = "SOL"
        action = self._create_action(controller, self.AMM_PAIR, self.CEX_PAIR,
                                     rates={"ETH-SOL": Decimal("NaN"),
                                            "ETH-USDT": Decimal("100")})
        self.assertIsNone(action)

    def test_zero_and_none_gas_rate_skip_creation(self):
        controller = self._make_controller()
        controller._gas_token_cache["amm_dex"] = "SOL"
        for gas_rate in (Decimal("0"), None):
            action = self._create_action(controller, self.AMM_PAIR, self.CEX_PAIR,
                                         rates={"ETH-SOL": gas_rate,
                                                "ETH-USDT": Decimal("100")})
            self.assertIsNone(action)

    def test_selling_side_amm_gas_rate_also_guarded(self):
        controller = self._make_controller()
        controller._gas_token_cache["amm_dex"] = "SOL"
        action = self._create_action(controller, self.CEX_PAIR, self.AMM_PAIR,
                                     rates={"ETH-SOL": Decimal("NaN"),
                                            "ETH-USDT": Decimal("100")})
        self.assertIsNone(action)

    def test_valid_gas_rate_passed_to_executor(self):
        controller = self._make_controller()
        controller._gas_token_cache["amm_dex"] = "SOL"
        action = self._create_action(controller, self.AMM_PAIR, self.CEX_PAIR,
                                     rates={"ETH-SOL": Decimal("20"),
                                            "ETH-USDT": Decimal("100")})
        self.assertIsInstance(action, CreateExecutorAction)
        self.assertEqual(Decimal("20"), action.executor_config.gas_conversion_price)

    def test_gas_token_equal_to_base_uses_unit_rate(self):
        # The executor divides a gas cost denominated in the gas token by a BASE-GAS
        # rate; when the base IS the gas token the rate is exactly 1 and must not
        # depend on the oracle answering a degenerate "SOL-SOL" query.
        amm_sol = ConnectorPair(connector_name="amm_dex", trading_pair="SOL-USDC")
        cex_sol = ConnectorPair(connector_name="binance", trading_pair="SOL-USDT")
        controller = self._make_controller(exchange_pair_2=amm_sol)
        controller.base_asset = "SOL"
        controller._gas_token_cache["amm_dex"] = "SOL"
        action = self._create_action(controller, amm_sol, cex_sol,
                                     rates={"SOL-USDT": Decimal("100")})
        self.assertIsInstance(action, CreateExecutorAction)
        self.assertEqual(Decimal("1"), action.executor_config.gas_conversion_price)

    def test_cex_only_setup_needs_no_gas_price(self):
        # Regression guard: gas_conversion_price=None stays legitimate with no AMM leg.
        cex_2 = ConnectorPair(connector_name="kraken", trading_pair="ETH-USDT")
        controller = self._make_controller(exchange_pair_2=cex_2)
        action = self._create_action(controller, self.CEX_PAIR, cex_2,
                                     rates={"ETH-USDT": Decimal("100")})
        self.assertIsInstance(action, CreateExecutorAction)
        self.assertIsNone(action.executor_config.gas_conversion_price)


class TestArbitrageGasRegistration(ArbitrageTestBase):
    """CLA-407 / CLA-307 — register the queried pair, after discovery completes."""

    def test_registered_gas_pair_matches_queried_pair(self):
        # Would-have-caught: old code registered gas-quote ("SOL-USDC") while the
        # create path queries base-gas ("ETH-SOL") — the queried pair was never fed.
        controller = self._make_controller()
        controller._gas_token_cache["amm_dex"] = "SOL"
        self.market_data_provider.initialize_rate_sources.reset_mock()
        with self._amm_patch():
            controller.initialize_rate_sources()
        registered = self.market_data_provider.initialize_rate_sources.call_args[0][0]
        registered_pairs = [cp.trading_pair for cp in registered]
        self.assertIn("ETH-SOL", registered_pairs)
        self.assertNotIn("SOL-USDC", registered_pairs)

    def test_gas_pair_not_registered_when_gas_token_is_base(self):
        amm_sol = ConnectorPair(connector_name="amm_dex", trading_pair="SOL-USDC")
        controller = self._make_controller(exchange_pair_2=amm_sol)
        controller._gas_token_cache["amm_dex"] = "SOL"
        self.market_data_provider.initialize_rate_sources.reset_mock()
        with self._amm_patch():
            controller.initialize_rate_sources()
        registered = self.market_data_provider.initialize_rate_sources.call_args[0][0]
        self.assertNotIn("SOL-SOL", [cp.trading_pair for cp in registered])

    async def test_rate_sources_registered_after_gas_discovery(self):
        # Would-have-caught CLA-307: with a running loop, old __init__ fired the gas
        # fetch as a task and called initialize_rate_sources() immediately — the cache
        # was still empty, so the gas rate pair was silently never registered.
        config = ArbitrageControllerConfig(
            id="test-arb-race",
            total_amount_quote=Decimal("100"),
            exchange_pair_1=self.CEX_PAIR,
            exchange_pair_2=self.AMM_PAIR,
            rate_connector="binance",
        )
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=1000.0)
        gateway = MagicMock()
        gateway.get_connector_chain_network = AsyncMock(
            return_value=("solana", "mainnet-beta", None))
        gateway.get_native_currency_symbol = AsyncMock(return_value="SOL")
        with self._amm_patch(), \
                patch("controllers.generic.arbitrage_controller.GatewayHttpClient") as gw_cls:
            gw_cls.get_instance.return_value = gateway
            controller = ArbitrageController(
                config=config,
                market_data_provider=market_data_provider,
                actions_queue=AsyncMock(spec=asyncio.Queue),
            )
            for _ in range(10):
                await asyncio.sleep(0)
        market_data_provider.initialize_rate_sources.assert_called()
        registered = market_data_provider.initialize_rate_sources.call_args[0][0]
        self.assertIn("ETH-SOL", [cp.trading_pair for cp in registered])
        self.assertEqual("SOL", controller.get_gas_token("amm_dex"))


class TestArbitrageConfigValidation(IsolatedAsyncioWrapperTestCase):
    """CLA-007 — validators only (the "~2x capital" framing was rejected)."""

    def _config_kwargs(self, **overrides):
        kwargs = dict(
            id="test-arb-cfg",
            total_amount_quote=Decimal("100"),
            exchange_pair_1=ConnectorPair(connector_name="binance", trading_pair="ETH-USDT"),
            exchange_pair_2=ConnectorPair(connector_name="kraken", trading_pair="ETH-USDT"),
        )
        kwargs.update(overrides)
        return kwargs

    def test_defaults_accepted(self):
        config = ArbitrageControllerConfig(**self._config_kwargs())
        self.assertEqual(Decimal("0.01"), config.min_profitability)
        self.assertEqual(1, config.max_executors_imbalance)

    def test_negative_min_profitability_rejected(self):
        # A negative floor authorizes guaranteed-loss round-trips.
        with self.assertRaises(ValidationError):
            ArbitrageControllerConfig(**self._config_kwargs(min_profitability=Decimal("-0.01")))

    def test_nan_min_profitability_rejected(self):
        with self.assertRaises(ValidationError):
            ArbitrageControllerConfig(**self._config_kwargs(min_profitability=Decimal("NaN")))

    def test_zero_min_profitability_allowed(self):
        # Boundary pin: zero (break-even floor) is permissive but not loss-authorizing.
        config = ArbitrageControllerConfig(**self._config_kwargs(min_profitability=Decimal("0")))
        self.assertEqual(Decimal("0"), config.min_profitability)

    def test_zero_max_executors_imbalance_rejected(self):
        # abs(imbalance) >= 0 is always true — 0 blocks all trading forever.
        with self.assertRaises(ValidationError):
            ArbitrageControllerConfig(**self._config_kwargs(max_executors_imbalance=0))

    def test_negative_delay_rejected(self):
        with self.assertRaises(ValidationError):
            ArbitrageControllerConfig(**self._config_kwargs(delay_between_executors=-1))
