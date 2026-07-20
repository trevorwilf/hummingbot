from decimal import Decimal
from typing import List, Optional, Set

import pandas as pd
from pydantic import field_validator

from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.core.data_type.common import MarketDict
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.arbitrage_executor.data_types import ArbitrageExecutorConfig
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType


class ArbitrageControllerConfig(ControllerConfigBase):
    controller_name: str = "arbitrage_controller"
    exchange_pair_1: ConnectorPair = ConnectorPair(connector_name="binance", trading_pair="SOL-USDT")
    exchange_pair_2: ConnectorPair = ConnectorPair(connector_name="jupiter/router", trading_pair="SOL-USDC")
    min_profitability: Decimal = Decimal("0.01")
    delay_between_executors: int = 10  # in seconds
    max_executors_imbalance: int = 1
    rate_connector: str = "binance"
    quote_conversion_asset: str = "USDT"

    @field_validator("min_profitability", mode="before")
    @classmethod
    def validate_min_profitability(cls, v):
        """CLA-007: a negative min_profitability authorizes guaranteed-loss round-trips."""
        v = Decimal(str(v))
        if not v.is_finite() or v < 0:
            raise ValueError("min_profitability must be a finite, non-negative decimal")
        return v

    @field_validator("delay_between_executors", mode="before")
    @classmethod
    def validate_delay_between_executors(cls, v):
        v = int(v)
        if v < 0:
            raise ValueError("delay_between_executors must be >= 0 seconds")
        return v

    @field_validator("max_executors_imbalance", mode="before")
    @classmethod
    def validate_max_executors_imbalance(cls, v):
        """CLA-007: with 0, abs(imbalance) >= 0 is always true and trading is blocked forever."""
        v = int(v)
        if v < 1:
            raise ValueError("max_executors_imbalance must be >= 1 (0 blocks all trading)")
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return [markets.add_or_update(cp.connector_name, cp.trading_pair) for cp in [self.exchange_pair_1, self.exchange_pair_2]][-1]


class ArbitrageController(ControllerBase):
    def __init__(self, config: ArbitrageControllerConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)
        self._imbalance = 0
        self._last_buy_closed_timestamp = 0
        self._last_sell_closed_timestamp = 0
        self._len_active_buy_arbitrages = 0
        self._len_active_sell_arbitrages = 0
        # GEN-12: imbalance is kept cumulatively in controller state, fill-derived.
        # Ids already counted, so an executor falling out of the archival-coupled
        # executors_info buffer does not change the imbalance.
        self._counted_executor_ids: Set[str] = set()
        self.base_asset = self.config.exchange_pair_1.trading_pair.split("-")[0]
        self._gas_token_cache = {}  # Cache for gas tokens by connector
        # CLA-307: gas-token discovery must COMPLETE before rate sources are
        # registered — otherwise the gas rate pair is silently never registered.
        # _initialize_gas_tokens() calls initialize_rate_sources() after discovery.
        self._initialize_gas_tokens()

    def initialize_rate_sources(self):
        rates_required = []
        for connector_pair in [self.config.exchange_pair_1, self.config.exchange_pair_2]:
            base, quote = connector_pair.trading_pair.split("-")

            # Add rate source for gas token if it's an AMM connector.
            # CLA-407: register the pair create_arbitrage_executor_action actually
            # queries (base-gas), not gas-quote.
            if connector_pair.is_amm_connector():
                gas_token = self.get_gas_token(connector_pair.connector_name)
                if gas_token and gas_token != base:
                    rates_required.append(ConnectorPair(connector_name=self.config.rate_connector,
                                                        trading_pair=f"{base}-{gas_token}"))

            # Add rate source for quote conversion asset
            if quote != self.config.quote_conversion_asset:
                rates_required.append(ConnectorPair(connector_name=self.config.rate_connector,
                                                    trading_pair=f"{quote}-{self.config.quote_conversion_asset}"))

            # Add rate source for trading pairs
            rates_required.append(ConnectorPair(connector_name=connector_pair.connector_name,
                                                trading_pair=connector_pair.trading_pair))
        if len(rates_required) > 0:
            self.market_data_provider.initialize_rate_sources(rates_required)

    def _initialize_gas_tokens(self):
        """Initialize gas tokens for AMM connectors during controller initialization.

        CLA-307: rate sources are registered only AFTER gas-token discovery finishes,
        so the gas conversion pair is registered with the discovered token instead of
        racing a fire-and-forget fetch.
        """
        import asyncio

        async def fetch_gas_tokens():
            for connector_pair in [self.config.exchange_pair_1, self.config.exchange_pair_2]:
                if connector_pair.is_amm_connector():
                    connector_name = connector_pair.connector_name
                    if connector_name not in self._gas_token_cache:
                        try:
                            gateway_client = GatewayHttpClient.get_instance()

                            # Get chain and network for the connector
                            chain, network, error = await gateway_client.get_connector_chain_network(
                                connector_name
                            )

                            if error:
                                self.logger().warning(f"Failed to get chain info for {connector_name}: {error}")
                                continue

                            # Get native currency symbol
                            native_currency = await gateway_client.get_native_currency_symbol(chain, network)

                            if native_currency:
                                self._gas_token_cache[connector_name] = native_currency
                                self.logger().info(f"Gas token for {connector_name}: {native_currency}")
                            else:
                                self.logger().warning(f"Failed to get native currency for {connector_name}")
                        except Exception as e:
                            self.logger().error(f"Error getting gas token for {connector_name}: {e}")

        async def fetch_then_register():
            await fetch_gas_tokens()
            self.initialize_rate_sources()

        # Run the async function to fetch gas tokens, then register rate sources
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(fetch_then_register())
        else:
            loop.run_until_complete(fetch_gas_tokens())
            self.initialize_rate_sources()

    def get_gas_token(self, connector_name: str) -> Optional[str]:
        """Get the cached gas token for a connector."""
        return self._gas_token_cache.get(connector_name)

    async def update_processed_data(self):
        pass

    def determine_executor_actions(self) -> List[ExecutorAction]:
        self.update_arbitrage_stats()
        executor_actions = []
        current_time = self.market_data_provider.time()
        if (abs(self._imbalance) >= self.config.max_executors_imbalance or
                self._last_buy_closed_timestamp + self.config.delay_between_executors > current_time or
                self._last_sell_closed_timestamp + self.config.delay_between_executors > current_time):
            return executor_actions
        if self._len_active_buy_arbitrages == 0:
            executor_actions.append(self.create_arbitrage_executor_action(self.config.exchange_pair_1,
                                                                          self.config.exchange_pair_2))
        if self._len_active_sell_arbitrages == 0:
            executor_actions.append(self.create_arbitrage_executor_action(self.config.exchange_pair_2,
                                                                          self.config.exchange_pair_1))
        return [action for action in executor_actions if action is not None]

    def create_arbitrage_executor_action(self, buying_exchange_pair: ConnectorPair,
                                         selling_exchange_pair: ConnectorPair):
        try:
            # CLA-2b-002 / CLA-307: the executor divides the gas cost (denominated in
            # the gas token) by gas_conversion_price. For an AMM leg the price is
            # REQUIRED — an absent/NaN/zero value wedges the executor at RUNNING with
            # zero trades. Apply the same finite/>0 guard as `rate` and skip creation
            # when it is unavailable (e.g. gas discovery has not completed yet).
            if buying_exchange_pair.is_amm_connector():
                amm_exchange_pair = buying_exchange_pair
            elif selling_exchange_pair.is_amm_connector():
                amm_exchange_pair = selling_exchange_pair
            else:
                amm_exchange_pair = None

            gas_conversion_price = None
            if amm_exchange_pair is not None:
                gas_token = self.get_gas_token(amm_exchange_pair.connector_name)
                if not gas_token:
                    self.logger().warning(
                        f"Gas token for {amm_exchange_pair.connector_name} is not available yet. "
                        f"Skipping executor creation.")
                    return None
                amm_base = amm_exchange_pair.trading_pair.split("-")[0]
                if gas_token == amm_base:
                    # gas cost is already denominated in the base asset — rate is exactly 1
                    gas_conversion_price = Decimal("1")
                else:
                    raw_gas_rate = self.market_data_provider.get_rate(f"{amm_base}-{gas_token}")
                    gas_conversion_price = Decimal(str(raw_gas_rate)) if raw_gas_rate is not None else None
                    if (gas_conversion_price is None or not gas_conversion_price.is_finite()
                            or gas_conversion_price <= 0):
                        self.logger().warning(
                            f"Cannot get a valid gas conversion rate for {amm_base}-{gas_token} "
                            f"(got {gas_conversion_price}). Skipping executor creation.")
                        return None
            rate = self.market_data_provider.get_rate(self.base_asset + "-" + self.config.quote_conversion_asset)
            # GEN-12: `if not rate` let Decimal("NaN") through — require finite and positive
            rate = Decimal(str(rate)) if rate is not None else None
            if rate is None or not rate.is_finite() or rate <= 0:
                self.logger().warning(
                    f"Cannot get a valid conversion rate for {self.base_asset}-{self.config.quote_conversion_asset} "
                    f"(got {rate}). Skipping executor creation.")
                return None
            amount_quantized = self.market_data_provider.quantize_order_amount(
                buying_exchange_pair.connector_name, buying_exchange_pair.trading_pair,
                self.config.total_amount_quote / rate)
            # GEN-12: a zero/negative quantized amount would create a never-trading executor
            # that blocks both directions via the active-executor gates
            if amount_quantized is None or amount_quantized <= 0:
                self.logger().warning(
                    f"Quantized order amount is not positive ({amount_quantized}) for "
                    f"{buying_exchange_pair.connector_name}:{buying_exchange_pair.trading_pair}. "
                    f"Skipping executor creation.")
                return None
            arbitrage_config = ArbitrageExecutorConfig(
                timestamp=self.market_data_provider.time(),
                buying_market=buying_exchange_pair,
                selling_market=selling_exchange_pair,
                order_amount=amount_quantized,
                min_profitability=self.config.min_profitability,
                gas_conversion_price=gas_conversion_price,
            )
            return CreateExecutorAction(
                executor_config=arbitrage_config,
                controller_id=self.config.id)
        except Exception as e:
            self.logger().error(
                f"Error creating executor to buy on {buying_exchange_pair.connector_name} and sell on {selling_exchange_pair.connector_name}, {e}")

    def update_arbitrage_stats(self):
        # GEN-12: only executors that actually traded count toward the imbalance —
        # FAILED / zero-fill executors must not stall a direction. Counting is
        # cumulative (controller state) so archival of the executors_info buffer
        # does not re-derive (and silently reset) the imbalance.
        active_executors = [e for e in self.executors_info if e.status != RunnableStatus.TERMINATED]
        completed_executors = [e for e in self.executors_info if
                               e.status == RunnableStatus.TERMINATED and
                               e.close_type == CloseType.COMPLETED and
                               e.filled_amount_quote > 0]
        for executor in completed_executors:
            if executor.id in self._counted_executor_ids:
                continue
            self._counted_executor_ids.add(executor.id)
            close_timestamp = executor.close_timestamp or 0
            if executor.config.buying_market == self.config.exchange_pair_1:
                self._imbalance += 1
                self._last_buy_closed_timestamp = max(self._last_buy_closed_timestamp, close_timestamp)
            elif executor.config.buying_market == self.config.exchange_pair_2:
                self._imbalance -= 1
                self._last_sell_closed_timestamp = max(self._last_sell_closed_timestamp, close_timestamp)
        # Prune ids that already fell out of the buffer — they can never be re-counted
        current_ids = {e.id for e in self.executors_info}
        self._counted_executor_ids &= current_ids
        self._len_active_buy_arbitrages = len([arbitrage for arbitrage in active_executors if
                                               arbitrage.config.buying_market == self.config.exchange_pair_1])
        self._len_active_sell_arbitrages = len([arbitrage for arbitrage in active_executors if
                                                arbitrage.config.buying_market == self.config.exchange_pair_2])

    def to_format_status(self) -> List[str]:
        all_executors_custom_info = pd.DataFrame(e.custom_info for e in self.executors_info)
        return [format_df_for_printout(all_executors_custom_info, table_format="psql", )]
