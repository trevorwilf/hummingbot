import time as _time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Union

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.utils.common import parse_comma_separated_list, parse_enum_value


class MarketMakingControllerConfigBase(ControllerConfigBase):
    """
    This class represents the base configuration for a market making controller.
    """
    controller_type: str = "market_making"
    connector_name: str = Field(
        default="binance_perpetual",
        json_schema_extra={
            "prompt": "Enter the connector name (e.g., binance_perpetual): ",
            "prompt_on_new": True}
    )
    trading_pair: str = Field(
        default="WLD-USDT",
        json_schema_extra={
            "prompt": "Enter the trading pair to trade on (e.g., WLD-USDT): ",
            "prompt_on_new": True}
    )
    buy_spreads: List[float] = Field(
        default="0.01,0.02",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy spreads (e.g., '0.01, 0.02'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_spreads: List[float] = Field(
        default="0.01,0.02",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell spreads (e.g., '0.01, 0.02'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    buy_amounts_pct: Union[List[Decimal], None] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy amounts as percentages (e.g., '50, 50'), or leave blank to distribute equally: ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_amounts_pct: Union[List[Decimal], None] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell amounts as percentages (e.g., '50, 50'), or leave blank to distribute equally: ",
            "prompt_on_new": True, "is_updatable": True}
    )
    executor_refresh_time: int = Field(
        default=60 * 5,
        json_schema_extra={
            "prompt": "Enter the refresh time in seconds for executors (e.g., 300 for 5 minutes): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    cooldown_time: int = Field(
        default=15,
        json_schema_extra={
            "prompt": "Enter the cooldown time in seconds between replacing an executor that traded (e.g., 15): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    leverage: int = Field(
        default=1,
        json_schema_extra={
            "prompt": "Enter the leverage to use for trading (e.g., 1 for spot trading, higher for margin): ",
            "prompt_on_new": True}
    )
    position_mode: PositionMode = Field(
        default="HEDGE",
        json_schema_extra={"prompt": "Enter the position mode (HEDGE/ONEWAY): "}
    )
    # Triple Barrier Configuration
    stop_loss: Optional[Decimal] = Field(
        default=Decimal("0.03"), gt=0,
        json_schema_extra={
            "prompt": "Enter the stop loss (as a decimal, e.g., 0.03 for 3%): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    take_profit: Optional[Decimal] = Field(
        default=Decimal("0.02"), gt=0,
        json_schema_extra={
            "prompt": "Enter the take profit (as a decimal, e.g., 0.02 for 2%): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    time_limit: Optional[int] = Field(
        default=60 * 45, gt=0,
        json_schema_extra={
            "prompt": "Enter the time limit in seconds (e.g., 2700 for 45 minutes): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    take_profit_order_type: OrderType = Field(
        default=OrderType.LIMIT,
        json_schema_extra={
            "prompt": "Enter the order type for take profit (LIMIT/MARKET): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    trailing_stop: Optional[TrailingStop] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trailing stop as activation_price,trailing_delta (e.g., 0.015,0.003): ",
            "prompt_on_new": True, "is_updatable": True},
    )
    # Position Management Configuration
    position_rebalance_threshold_pct: Decimal = Field(
        default=Decimal("0.05"),
        json_schema_extra={
            "prompt": "Enter the position rebalance threshold percentage (e.g., 0.05 for 5%): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    rebalance_cooldown_time: int = Field(
        default=60,
        json_schema_extra={
            "prompt": "Enter the cooldown time in seconds after a rebalance attempt (e.g., 60): ",
            "is_updatable": True}
    )
    skip_rebalance: bool = Field(default=False)
    use_wallet_balance: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": "Seed sell-side inventory from existing wallet balance? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": False
        }
    )
    max_market_data_stale_seconds: int = Field(
        default=120,  # Live testing shows quiet markets can have 45-90+ second gaps
                      # between data frames on healthy connections.
        json_schema_extra={
            "prompt": "Seconds of no market data before soft stale action: ",
            "is_updatable": True
        }
    )
    hard_market_data_stale_seconds: int = Field(
        default=300,  # Must be substantially above soft threshold to avoid
                      # premature hard stops on quiet venues.
        json_schema_extra={
            "prompt": "Seconds of no market data before hard stop of all executors: ",
            "is_updatable": True
        }
    )
    stale_data_action: str = Field(
        default="pause_new_orders",  # Cancelling passive orders destroys queue position
                                     # and creates churn. Pausing new orders is safer.
        json_schema_extra={
            "prompt": "Action on stale market data (warn_only, pause_new_orders, cancel_passive_orders): ",
            "is_updatable": True
        }
    )

    @field_validator("trailing_stop", mode="before")
    @classmethod
    def parse_trailing_stop(cls, v):
        if isinstance(v, str):
            if v == "":
                return None
            activation_price, trailing_delta = v.split(",")
            return TrailingStop(activation_price=Decimal(activation_price), trailing_delta=Decimal(trailing_delta))
        return v

    @field_validator("time_limit", "stop_loss", "take_profit", "position_rebalance_threshold_pct", mode="before")
    @classmethod
    def validate_target(cls, v):
        if isinstance(v, str):
            if v == "":
                return None
            return Decimal(v)
        return v

    @field_validator('take_profit_order_type', mode="before")
    @classmethod
    def validate_order_type(cls, v) -> OrderType:
        if v is None:
            return OrderType.MARKET
        if isinstance(v, str):
            v = v.replace("OrderType.", "")
        return parse_enum_value(OrderType, v, "take_profit_order_type")

    @field_validator('position_mode', mode="before")
    @classmethod
    def validate_position_mode(cls, v: str) -> PositionMode:
        return parse_enum_value(PositionMode, v, "position_mode")

    @field_validator('buy_spreads', 'sell_spreads', mode="before")
    @classmethod
    def parse_spreads(cls, v):
        return parse_comma_separated_list(v)

    @field_validator('buy_amounts_pct', 'sell_amounts_pct', mode="before")
    @classmethod
    def parse_and_validate_amounts(cls, v, validation_info: ValidationInfo):
        field_name = validation_info.field_name
        if v is None or v == "":
            spread_field = field_name.replace('amounts_pct', 'spreads')
            return [1 for _ in validation_info.data[spread_field]]
        parsed = parse_comma_separated_list(v)
        if isinstance(parsed, list) and len(parsed) != len(validation_info.data[field_name.replace('amounts_pct', 'spreads')]):
            raise ValueError(
                f"The number of {field_name} must match the number of {field_name.replace('amounts_pct', 'spreads')}.")
        return parsed

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            time_limit=self.time_limit,
            trailing_stop=self.trailing_stop,
            open_order_type=OrderType.LIMIT,  # Defaulting to LIMIT as is a Maker Controller
            take_profit_order_type=self.take_profit_order_type,
            stop_loss_order_type=OrderType.MARKET,  # Defaulting to MARKET as per requirement
            time_limit_order_type=OrderType.MARKET  # Defaulting to MARKET as per requirement
        )

    def get_spreads_and_amounts_in_quote(self, trade_type: TradeType) -> Tuple[List[float], List[float]]:
        buy_amounts_pct = getattr(self, 'buy_amounts_pct')
        sell_amounts_pct = getattr(self, 'sell_amounts_pct')

        # Calculate total percentages across buys and sells
        total_pct = sum(buy_amounts_pct) + sum(sell_amounts_pct)

        # Normalize amounts_pct based on total percentages
        if trade_type == TradeType.BUY:
            normalized_amounts_pct = [amt_pct / total_pct for amt_pct in buy_amounts_pct]
        else:  # TradeType.SELL
            normalized_amounts_pct = [amt_pct / total_pct for amt_pct in sell_amounts_pct]

        spreads = getattr(self, f'{trade_type.name.lower()}_spreads')
        return spreads, [amt_pct * self.total_amount_quote for amt_pct in normalized_amounts_pct]

    def get_required_base_amount(self, reference_price: Decimal) -> Decimal:
        """
        Get the required base asset amount for sell orders.
        """
        _, sell_amounts_quote = self.get_spreads_and_amounts_in_quote(TradeType.SELL)
        total_sell_amount_quote = sum(sell_amounts_quote)
        return total_sell_amount_quote / reference_price

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class MarketMakingControllerBase(ControllerBase):
    """
    This class represents the base class for a market making controller.
    """

    def __init__(self, config: MarketMakingControllerConfigBase, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_rebalance_attempt_timestamp: float = 0.0
        self._wallet_balance_seeded: bool = False
        self._startup_logged: bool = False
        self._last_ob_snapshot_uid: Optional[int] = None  # None = never seen
        self._last_ob_diff_uid: Optional[int] = None      # None = never seen
        self._last_ob_event_time: Optional[float] = None   # None = no market data yet
        self._stale_state: str = "unknown"                 # "unknown" | "healthy" | "stale"
        self._last_stale_log_time: float = 0.0             # Rate-limit logging
        self._stale_transition_time: Optional[float] = None  # When stale state began
        self._stale_suppression_logged: bool = False
        self.market_data_provider.initialize_rate_sources([ConnectorPair(
            connector_name=config.connector_name, trading_pair=config.trading_pair)])
        self.logger().info(
            f"Controller initialized: id={self.config.id} "
            f"connector={self.config.connector_name} pair={self.config.trading_pair} "
            f"buy_levels={len(self.config.buy_spreads)} sell_levels={len(self.config.sell_spreads)} "
            f"total_amount_quote={getattr(self.config, 'total_amount_quote', 'N/A')} "
            f"use_wallet_balance={getattr(self.config, 'use_wallet_balance', 'N/A')} "
            f"skip_rebalance={getattr(self.config, 'skip_rebalance', 'N/A')} "
            f"refresh_time={getattr(self.config, 'executor_refresh_time', 'N/A')}s"
        )
        self.logger().info(
            f"Market data staleness config: controller={self.config.id} "
            f"max_stale_seconds={self.config.max_market_data_stale_seconds} "
            f"hard_stale_seconds={self.config.hard_market_data_stale_seconds} "
            f"stale_action={self.config.stale_data_action}"
        )

    def compute_wallet_seed_amount(self, reference_price: Decimal) -> Decimal:
        """
        Compute how much base asset to seed from the wallet into the controller's inventory.

        When use_wallet_balance is True, queries the exchange for the available balance
        of the base asset and returns min(available_balance, required_base_for_sell_side).
        Returns Decimal("0") if the flag is False or the base asset has no available balance.

        :param reference_price: Current market price for the trading pair
        :return: Amount of base asset to seed as initial inventory
        """
        if not self.config.use_wallet_balance:
            return Decimal("0")

        # Extract base asset from trading pair (e.g., "ARRR-USDT" -> "ARRR")
        base_asset = self.config.trading_pair.split("-")[0]

        try:
            available_balance = self.market_data_provider.get_available_balance(
                self.config.connector_name, base_asset
            )
        except Exception as e:
            self.logger().warning(
                f"Wallet seed for {self.config.trading_pair}: "
                f"failed to query balance for {base_asset} — {e}"
            )
            return Decimal("0")

        if available_balance is None or available_balance <= Decimal("0"):
            self.logger().info(
                f"Wallet seed for {self.config.trading_pair}: "
                f"no available {base_asset} balance to seed."
            )
            return Decimal("0")

        # Compute how much base the sell side needs
        required_base = self.config.get_required_base_amount(reference_price)

        # Take the lesser of what's available and what's needed
        seed_amount = min(available_balance, required_base)

        self.logger().info(
            f"Wallet seed for {self.config.trading_pair}: "
            f"available={available_balance}, required={required_base}, "
            f"seeding={seed_amount} {base_asset}"
        )
        return seed_amount

    def _check_market_data_freshness(self):
        """
        Check order book freshness using UID mutations AND WebSocket connection state.

        Design rationale (validated by live WS testing against NonKYC):
        - Quiet markets (e.g., ARRR-USDT) can have 45-90+ second gaps between
          ANY WebSocket data frames, even with ticker+orderbook+trades subscribed.
        - The WS connection stays alive via invisible protocol-level pings that
          do NOT update any application-level timestamps.
        - Therefore we CANNOT use message timestamps for transport liveness.
        - Instead, we check the WSConnection.connected state, which reflects
          protocol-level connectivity. If connected=True, the transport is alive
          and the market is simply quiet — NOT stale.
        """
        try:
            connectors = getattr(self.market_data_provider, 'connectors', None)
            if not connectors or self.config.connector_name not in connectors:
                return
            connector = connectors[self.config.connector_name]
            ob = connector.get_order_book(self.config.trading_pair)
            if ob is None:
                return

            now = _time.time()
            changed = False

            # Check snapshot UID
            snapshot_uid = getattr(ob, 'snapshot_uid', None)
            if snapshot_uid is not None and snapshot_uid != 0:
                if self._last_ob_snapshot_uid != snapshot_uid:
                    self._last_ob_snapshot_uid = snapshot_uid
                    changed = True

            # Check diff UID
            diff_uid = getattr(ob, 'last_diff_uid', None)
            if diff_uid is not None and diff_uid != 0:
                if self._last_ob_diff_uid != diff_uid:
                    self._last_ob_diff_uid = diff_uid
                    changed = True

            if changed:
                self._last_ob_event_time = now
                old_state = self._stale_state
                self._stale_state = "healthy"
                self._stale_suppression_logged = False
                if old_state in ("stale", "quiet") and self._stale_transition_time is not None:
                    duration = now - self._stale_transition_time
                    self.logger().info(
                        f"MARKET DATA RECOVERED: controller={self.config.id} "
                        f"stale duration={duration:.0f}s"
                    )
                self._stale_transition_time = None
                return

            # No UID change — evaluate staleness
            if self._last_ob_event_time is None:
                return

            age = now - self._last_ob_event_time
            threshold = self.config.max_market_data_stale_seconds

            if age > threshold:
                # Before declaring stale, check if the WS transport is still alive
                ws_connected = False
                try:
                    ws_connected = getattr(connector, 'is_public_ws_connected', False)
                except Exception:
                    pass

                if ws_connected:
                    # Transport alive but book unchanged — quiet market, NOT a dead feed
                    if self._stale_state != "quiet":
                        self._stale_state = "quiet"
                        self._stale_transition_time = self._stale_transition_time or now
                        self.logger().info(
                            f"QUIET MARKET: controller={self.config.id} "
                            f"orderbook unchanged for {age:.0f}s but WS connected. "
                            f"Not escalating to stale."
                        )
                        self._last_stale_log_time = now
                    elif now - (self._last_stale_log_time or 0) >= 120.0:
                        self.logger().info(
                            f"QUIET MARKET (ongoing): controller={self.config.id} "
                            f"orderbook unchanged for {age:.0f}s, WS still connected"
                        )
                        self._last_stale_log_time = now
                    # Do NOT escalate — market is quiet but transport is alive
                    return

                # WS is disconnected — genuine staleness
                old_state = self._stale_state
                self._stale_state = "stale"

                if old_state != "stale":
                    self._stale_transition_time = self._stale_transition_time or now
                    self.logger().warning(
                        f"STALE MARKET DATA: controller={self.config.id} "
                        f"orderbook last updated {age:.0f}s ago "
                        f"(threshold: {threshold}s). WS disconnected. "
                        f"Escalating per policy: {self.config.stale_data_action}"
                    )
                    self._last_stale_log_time = now
                elif now - (self._last_stale_log_time or 0) >= 60.0:
                    self.logger().info(
                        f"STALE MARKET DATA (ongoing): controller={self.config.id} "
                        f"orderbook last updated {age:.0f}s ago, WS disconnected"
                    )
                    self._last_stale_log_time = now
            else:
                self._stale_state = "healthy"

        except Exception as e:
            self.logger().debug(
                f"Market data freshness check error: {repr(e)}", exc_info=True
            )

    def _get_active_order_price_bounds(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Returns (highest_active_buy_price, lowest_active_sell_price) from
        all active executors with a known quote price — including those with
        unfilled resting orders (is_trading=False).
        """
        highest_buy = None
        lowest_sell = None
        for executor in self.executors_info:
            if not executor.is_active:
                continue
            # Include all active executors with a known quote price,
            # not just those with fills (is_trading). An unfilled resting
            # order still occupies a price level.
            level_id = executor.custom_info.get("level_id", "")
            avg_price = executor.custom_info.get("current_position_average_price")
            if avg_price is None:
                continue
            avg_price = Decimal(str(avg_price))
            if avg_price <= Decimal("0"):
                continue
            if level_id.startswith("buy"):
                if highest_buy is None or avg_price > highest_buy:
                    highest_buy = avg_price
            elif level_id.startswith("sell"):
                if lowest_sell is None or avg_price < lowest_sell:
                    lowest_sell = avg_price
        return highest_buy, lowest_sell

    def get_spendable_sell_base_inventory(self) -> Decimal:
        """
        Compute how much base asset is available for new sell entry orders.

        available_balances already represents FREE balance (exchange subtracts
        held collateral for open orders). We only need to additionally reserve
        base for buy-side executors that have filled base inventory but do NOT
        yet have a resting close-side sell order on the exchange — that base
        appears in 'available' but is logically committed to a future TP/SL sell.

        We do NOT subtract:
        - Active sell executor amounts (exchange already holds that base)
        - Buy executor amounts with an open TP sell order (exchange already holds that base)
        """
        base_asset = self.config.trading_pair.split("-")[0]
        available_base = Decimal("0")

        try:
            connectors = getattr(self.market_data_provider, 'connectors', None)
            if connectors and isinstance(connectors, dict) and self.config.connector_name in connectors:
                connector = connectors[self.config.connector_name]
                raw = connector.available_balances.get(base_asset, Decimal("0"))
                available_base = Decimal(str(raw))
            elif hasattr(self.market_data_provider, 'get_connector'):
                connector = self.market_data_provider.get_connector(self.config.connector_name)
                raw = connector.available_balances.get(base_asset, Decimal("0"))
                available_base = Decimal(str(raw))
        except Exception as e:
            self.logger().debug(f"Could not query {base_asset} balance: {e}")
            return Decimal("0")

        # Only reserve base for buy-side executors with filled inventory
        # that don't yet have a resting close-side sell order
        additional_reserve = Decimal("0")
        for executor in self.executors_info:
            if not (executor.is_active and executor.is_trading):
                continue
            level_id = executor.custom_info.get("level_id", "")
            if not level_id.startswith("buy"):
                continue

            has_open_close_order = executor.custom_info.get("has_open_close_order", False)
            close_order_side = executor.custom_info.get("close_order_side")

            # Only reserve if: this is a buy executor, its close side is SELL,
            # it has filled base, and there's no resting TP sell on the exchange
            if close_order_side == TradeType.SELL and not has_open_close_order:
                amount_to_close = Decimal(str(executor.custom_info.get("amount_to_close", "0")))
                if amount_to_close > Decimal("0"):
                    additional_reserve += amount_to_close

        spendable = available_base - additional_reserve
        log_msg = (
            f"Spendable sell inventory: controller={self.config.id} "
            f"available_base={available_base:.8f} "
            f"additional_reserve={additional_reserve:.8f} "
            f"spendable={max(Decimal('0'), spendable):.8f} {base_asset}"
        )
        if spendable <= Decimal("0"):
            self.logger().warning(log_msg + " [EXHAUSTED]")
        elif available_base > 0 and spendable < available_base * Decimal("0.5"):
            self.logger().info(log_msg + " [CONSTRAINED]")
        else:
            self.logger().debug(log_msg)
        return max(Decimal("0"), spendable)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Determine actions based on the provided executor handler report.
        Stop actions are emitted before create actions so that outgoing executors
        release their collateral before new ones attempt budget validation.
        """
        actions = []
        actions.extend(self.stop_actions_proposal())
        actions.extend(self.create_actions_proposal())
        return actions

    def create_actions_proposal(self) -> List[ExecutorAction]:
        """
        Create actions proposal based on the current state of the controller.
        Sell-side orders are clipped to available base inventory to prevent
        silent drops by the budget preflight.
        """
        create_actions = []

        # First-cycle startup balance log
        if not self._startup_logged:
            self._startup_logged = True
            try:
                base, quote = self.config.trading_pair.split("-")
                connectors = getattr(self.market_data_provider, 'connectors', None)
                if connectors and self.config.connector_name in connectors:
                    connector = connectors[self.config.connector_name]
                    self.logger().info(
                        f"First cycle for {self.config.id}: "
                        f"{base} avail={connector.available_balances.get(base, 0):.8f} "
                        f"total={connector.get_balance(base):.8f} | "
                        f"{quote} avail={connector.available_balances.get(quote, 0):.8f} "
                        f"total={connector.get_balance(quote):.8f}"
                    )
            except Exception:
                pass

        self.logger().debug(
            f"Action proposal: controller={self.config.id} pair={self.config.trading_pair} "
            f"ref_price={self.processed_data.get('reference_price', 'N/A')} "
            f"spread_mult={self.processed_data.get('spread_multiplier', 'N/A')}"
        )

        # Market data freshness check (LOG 12)
        self._check_market_data_freshness()

        # Suppress new order creation when stale (FIX 3)
        if self._stale_state == "stale" and self.config.stale_data_action != "warn_only":
            stale_age = _time.time() - (self._last_ob_event_time or 0)
            if stale_age > self.config.max_market_data_stale_seconds:
                if not self._stale_suppression_logged:
                    self.logger().warning(
                        f"STALE SUPPRESSION: controller={self.config.id} "
                        f"suppressing new orders (stale {stale_age:.0f}s, "
                        f"action={self.config.stale_data_action})"
                    )
                    self._stale_suppression_logged = True
                return self.stop_actions_proposal()

        # Per-controller executor inventory summary
        try:
            active_buys = []
            active_sells = []
            for executor in self.executors_info:
                if not executor.is_active:
                    continue
                level_id = executor.custom_info.get("level_id", "")
                if level_id.startswith("buy"):
                    active_buys.append(executor)
                elif level_id.startswith("sell"):
                    active_sells.append(executor)

            if active_buys or active_sells:
                buy_total = sum(
                    getattr(e.config, 'amount', Decimal("0"))
                    for e in active_buys
                    if hasattr(e, 'config') and e.config is not None
                )
                sell_total = sum(
                    getattr(e.config, 'amount', Decimal("0"))
                    for e in active_sells
                    if hasattr(e, 'config') and e.config is not None
                )
                buy_trading = sum(1 for e in active_buys if e.is_trading)
                sell_trading = sum(1 for e in active_sells if e.is_trading)
                self.logger().debug(
                    f"Executor inventory: controller={self.config.id} "
                    f"buys={len(active_buys)}({buy_trading} trading, ~{buy_total:.4f} qty) "
                    f"sells={len(active_sells)}({sell_trading} trading, ~{sell_total:.4f} qty)"
                )
        except Exception:
            pass  # Logging must never break proposal flow

        # Check if we need to rebalance position first
        position_rebalance_action = self.check_position_rebalance()
        if position_rebalance_action is not None:
            return [position_rebalance_action]

        highest_buy, lowest_sell = self._get_active_order_price_bounds()
        levels_to_execute = self.get_levels_to_execute()

        # Pre-compute spendable sell-side base inventory for clip logic (spot only)
        is_spot = "_perpetual" not in self.config.connector_name
        spendable_sell_base = self.get_spendable_sell_base_inventory() if is_spot else Decimal("0")
        initial_spendable = spendable_sell_base

        for level_id in levels_to_execute:
            price, amount = self.get_price_and_amount(level_id)
            trade_type = self.get_trade_type_from_level_id(level_id)

            # Cross-order prevention (deduplicated — warn once per level)
            if trade_type == TradeType.SELL and highest_buy is not None and price <= highest_buy:
                if not getattr(self, '_cross_order_warned', {}).get(level_id):
                    self.logger().warning(
                        f"Skipping {level_id}: controller={self.config.id} "
                        f"sell price {price:.6f} <= highest active buy {highest_buy:.6f}"
                    )
                    if not hasattr(self, '_cross_order_warned'):
                        self._cross_order_warned = {}
                    self._cross_order_warned[level_id] = True
                continue
            elif trade_type == TradeType.BUY and lowest_sell is not None and price >= lowest_sell:
                if not getattr(self, '_cross_order_warned', {}).get(level_id):
                    self.logger().warning(
                        f"Skipping {level_id}: controller={self.config.id} "
                        f"buy price {price:.6f} >= lowest active sell {lowest_sell:.6f}"
                    )
                    if not hasattr(self, '_cross_order_warned'):
                        self._cross_order_warned = {}
                    self._cross_order_warned[level_id] = True
                continue
            else:
                if hasattr(self, '_cross_order_warned') and level_id in self._cross_order_warned:
                    del self._cross_order_warned[level_id]

            # Sell-side inventory clipping (spot only)
            if trade_type == TradeType.SELL and "_perpetual" not in self.config.connector_name:
                if spendable_sell_base <= Decimal("0"):
                    if not getattr(self, '_clip_exhausted_warned', {}).get(level_id):
                        self.logger().warning(
                            f"Skipping {level_id}: controller={self.config.id} "
                            f"no spendable base remaining for sell-side quoting. "
                            f"Initial spendable was {initial_spendable:.8f} {self.config.trading_pair.split('-')[0]}"
                        )
                        if not hasattr(self, '_clip_exhausted_warned'):
                            self._clip_exhausted_warned = {}
                        self._clip_exhausted_warned[level_id] = True
                    continue

                clipped_amount = min(amount, spendable_sell_base)
                if clipped_amount < amount:
                    # Deduplicate: only warn if the clipping state changed
                    last_clip = getattr(self, '_last_clip_state', {}).get(level_id)
                    clip_key = (str(amount), str(clipped_amount), str(spendable_sell_base))
                    if last_clip != clip_key:
                        self.logger().warning(
                            f"Clipping {level_id}: controller={self.config.id} "
                            f"sell amount {amount:.8f} -> {clipped_amount:.8f} "
                            f"(spendable base: {spendable_sell_base:.8f} "
                            f"{self.config.trading_pair.split('-')[0]})"
                        )
                        if not hasattr(self, '_last_clip_state'):
                            self._last_clip_state = {}
                        self._last_clip_state[level_id] = clip_key
                else:
                    # Clipping resolved — clear state so it can warn again if it recurs
                    if hasattr(self, '_last_clip_state') and level_id in self._last_clip_state:
                        del self._last_clip_state[level_id]
                    if hasattr(self, '_clip_exhausted_warned') and level_id in self._clip_exhausted_warned:
                        del self._clip_exhausted_warned[level_id]
                amount = clipped_amount
                spendable_sell_base -= amount

            # Min-notional / min-order-size pre-validation
            trading_rules = self._get_trading_rules()
            if trading_rules is not None:
                connectors = getattr(self.market_data_provider, 'connectors', None)
                if connectors and self.config.connector_name in connectors:
                    connector = connectors[self.config.connector_name]
                    q_amount = connector.quantize_order_amount(self.config.trading_pair, amount)
                    q_price = connector.quantize_order_price(self.config.trading_pair, price)
                else:
                    q_amount = amount
                    q_price = price

                notional = q_amount * q_price
                if q_amount < trading_rules.min_order_size:
                    if not getattr(self, '_min_size_warned', {}).get(level_id):
                        self.logger().warning(
                            f"Skipping {level_id}: amount {q_amount} < min_order_size "
                            f"{trading_rules.min_order_size} for {self.config.trading_pair}. "
                            f"Increase total_amount_quote or adjust level percentages."
                        )
                        if not hasattr(self, '_min_size_warned'):
                            self._min_size_warned = {}
                        self._min_size_warned[level_id] = True
                    continue
                elif trading_rules.min_notional_size > 0 and notional < trading_rules.min_notional_size:
                    if not getattr(self, '_min_notional_warned', {}).get(level_id):
                        self.logger().warning(
                            f"Skipping {level_id}: notional {notional:.8f} < min_notional_size "
                            f"{trading_rules.min_notional_size} for {self.config.trading_pair}. "
                            f"Increase total_amount_quote or adjust level percentages."
                        )
                        if not hasattr(self, '_min_notional_warned'):
                            self._min_notional_warned = {}
                        self._min_notional_warned[level_id] = True
                    continue
                else:
                    # Clear warning flags if the level becomes valid (price moved)
                    if hasattr(self, '_min_size_warned') and level_id in self._min_size_warned:
                        del self._min_size_warned[level_id]
                    if hasattr(self, '_min_notional_warned') and level_id in self._min_notional_warned:
                        del self._min_notional_warned[level_id]

            executor_config = self.get_executor_config(level_id, price, amount)
            if executor_config is not None:
                create_actions.append(CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=executor_config
                ))

        buy_count = sum(1 for a in create_actions if hasattr(a, 'executor_config') and a.executor_config.side == TradeType.BUY)
        sell_count = sum(1 for a in create_actions if hasattr(a, 'executor_config') and a.executor_config.side == TradeType.SELL)
        self.logger().info(
            f"Action proposal result: controller={self.config.id} "
            f"total_actions={len(create_actions)} buys={buy_count} sells={sell_count}"
        )
        return create_actions

    def _get_trading_rules(self):
        """Retrieve TradingRule for the configured pair, or None if unavailable."""
        try:
            connectors = getattr(self.market_data_provider, 'connectors', None)
            if connectors and isinstance(connectors, dict) and self.config.connector_name in connectors:
                connector = connectors[self.config.connector_name]
                rules = getattr(connector, 'trading_rules', None)
                if rules is not None and isinstance(rules, dict):
                    rule = rules.get(self.config.trading_pair)
                    # Verify it has the expected attributes (not a mock)
                    if rule is not None and hasattr(rule, 'min_order_size') and hasattr(rule, 'min_notional_size'):
                        try:
                            # Quick sanity check — these must be Decimal-like
                            _ = rule.min_order_size >= 0
                            return rule
                        except (TypeError, AttributeError):
                            pass
            elif hasattr(self.market_data_provider, 'get_connector'):
                connector = self.market_data_provider.get_connector(self.config.connector_name)
                rules = getattr(connector, 'trading_rules', None)
                if rules is not None and isinstance(rules, dict):
                    return rules.get(self.config.trading_pair)
        except Exception:
            pass
        return None

    def get_levels_to_execute(self) -> List[str]:
        working_levels = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active or (x.close_type == CloseType.STOP_LOSS and self.market_data_provider.time() - x.close_timestamp < self.config.cooldown_time)
        )
        working_levels_ids = [executor.custom_info["level_id"] for executor in working_levels]

        # Suppress levels where the most recent executor had a terminal failure
        # (will be retried after executor_refresh_time when price may have changed)
        recently_failed = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: not x.is_active and x.close_type == CloseType.FAILED
                and x.custom_info.get("terminal_failure_reason")
                and self.market_data_provider.time() - x.close_timestamp < self.config.executor_refresh_time
        )
        for executor in recently_failed:
            level_id = executor.custom_info.get("level_id")
            if level_id and level_id not in working_levels_ids:
                working_levels_ids.append(level_id)

        return self.get_not_active_levels_ids(working_levels_ids)

    def stop_actions_proposal(self) -> List[ExecutorAction]:
        """
        Create a list of actions to stop the executors based on order refresh and early stop conditions.
        """
        stop_actions = []
        stop_actions.extend(self.executors_to_refresh())
        stop_actions.extend(self.executors_to_early_stop())
        return stop_actions

    def executors_to_refresh(self) -> List[ExecutorAction]:
        refresh_time = self.config.executor_refresh_time
        now = self.market_data_provider.time()

        def is_refresh_eligible(x) -> bool:
            if not x.is_active:
                return False
            age = now - x.timestamp
            if age <= refresh_time:
                return False
            # Original behavior: unfilled executors are always refresh-eligible
            if not x.is_trading:
                return True
            # Partially filled / trading executors are refresh-eligible
            # if they've exceeded the refresh time. The position executor's
            # shutdown logic will handle canceling open orders and closing
            # the position appropriately.
            return True

        executors_to_refresh = self.filter_executors(
            executors=self.executors_info,
            filter_func=is_refresh_eligible)

        return [StopExecutorAction(
            controller_id=self.config.id,
            executor_id=executor.id) for executor in executors_to_refresh]

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        """
        Stop executors when market data is stale, according to configured policy.
        Hard threshold stops ALL active executors; soft threshold stops passive only.
        """
        if self._stale_state != "stale" or self._last_ob_event_time is None:
            return []

        stale_age = _time.time() - self._last_ob_event_time

        # Hard threshold: stop ALL active executors regardless of action mode
        if stale_age > self.config.hard_market_data_stale_seconds:
            active = [e for e in self.executors_info if e.is_active]
            if active:
                self.logger().warning(
                    f"STALE HARD STOP: controller={self.config.id} "
                    f"stopping {len(active)} executors (stale {stale_age:.0f}s > "
                    f"hard threshold {self.config.hard_market_data_stale_seconds}s)"
                )
            return [StopExecutorAction(
                controller_id=self.config.id,
                executor_id=executor.id
            ) for executor in active]

        # Soft threshold with cancel_passive_orders
        if (self.config.stale_data_action == "cancel_passive_orders"
                and stale_age > self.config.max_market_data_stale_seconds):
            passive = [e for e in self.executors_info
                       if e.is_active and not e.is_trading]
            if passive:
                self.logger().warning(
                    f"STALE CANCEL PASSIVE: controller={self.config.id} "
                    f"stopping {len(passive)} passive executors (stale {stale_age:.0f}s)"
                )
            return [StopExecutorAction(
                controller_id=self.config.id,
                executor_id=executor.id
            ) for executor in passive]

        return []

    async def update_processed_data(self):
        """
        Update the processed data for the controller. This method should be reimplemented to modify the reference price
        and spread multiplier based on the market data. By default, it will update the reference price as mid price and
        the spread multiplier as 1.
        """
        reference_price = self.market_data_provider.get_price_by_type(self.config.connector_name,
                                                                      self.config.trading_pair, PriceType.MidPrice)
        self.processed_data = {"reference_price": Decimal(reference_price), "spread_multiplier": Decimal("1")}

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        """
        Get the executor config for a given level id.
        """
        raise NotImplementedError

    def get_price_and_amount(self, level_id: str) -> Tuple[Decimal, Decimal]:
        """
        Get the spread and amount in quote for a given level id.
        """
        level = self.get_level_from_level_id(level_id)
        trade_type = self.get_trade_type_from_level_id(level_id)
        spreads, amounts_quote = self.config.get_spreads_and_amounts_in_quote(trade_type)
        reference_price = Decimal(self.processed_data["reference_price"])
        spread_in_pct = Decimal(spreads[int(level)]) * Decimal(self.processed_data["spread_multiplier"])
        side_multiplier = Decimal("-1") if trade_type == TradeType.BUY else Decimal("1")
        order_price = reference_price * (1 + side_multiplier * spread_in_pct)
        return order_price, Decimal(amounts_quote[int(level)]) / order_price

    def get_level_id_from_side(self, trade_type: TradeType, level: int) -> str:
        """
        Get the level id based on the trade type and the level.
        """
        return f"{trade_type.name.lower()}_{level}"

    def get_trade_type_from_level_id(self, level_id: str) -> TradeType:
        return TradeType.BUY if level_id.startswith("buy") else TradeType.SELL

    def get_level_from_level_id(self, level_id: str) -> int:
        return int(level_id.split('_')[1])

    def get_not_active_levels_ids(self, active_levels_ids: List[str]) -> List[str]:
        """
        Get the levels to execute based on the current state of the controller.
        """
        buy_ids_missing = [self.get_level_id_from_side(TradeType.BUY, level) for level in range(len(self.config.buy_spreads))
                           if self.get_level_id_from_side(TradeType.BUY, level) not in active_levels_ids]
        sell_ids_missing = [self.get_level_id_from_side(TradeType.SELL, level) for level in range(len(self.config.sell_spreads))
                            if self.get_level_id_from_side(TradeType.SELL, level) not in active_levels_ids]
        return buy_ids_missing + sell_ids_missing

    def _check_sell_side_inventory(self):
        """Emit a one-time warning if base inventory is insufficient for sell-side quoting."""
        try:
            reference_price = Decimal(self.processed_data.get("reference_price", "0"))
            if reference_price <= 0:
                return

            _, sell_amounts_quote = self.config.get_spreads_and_amounts_in_quote(TradeType.SELL)
            if not sell_amounts_quote:
                return

            required_base = sum(
                Decimal(str(a)) / reference_price for a in sell_amounts_quote
            )

            base_asset = self.config.trading_pair.split("-")[0]
            available = Decimal("0")

            connectors = getattr(self.market_data_provider, 'connectors', None)
            if connectors and self.config.connector_name in connectors:
                connector = connectors[self.config.connector_name]
                available = connector.available_balances.get(base_asset, Decimal("0"))
            elif hasattr(self.market_data_provider, 'get_connector'):
                connector = self.market_data_provider.get_connector(self.config.connector_name)
                available = connector.available_balances.get(base_asset, Decimal("0"))

            if required_base > 0 and available < required_base:
                deficit_pct = ((required_base - available) / required_base * Decimal("100")).quantize(Decimal("0.1"))
                self.logger().warning(
                    f"SELL-SIDE INVENTORY SHORTFALL: {self.config.trading_pair} has "
                    f"{available:.8f} {base_asset} available but needs {required_base:.8f} "
                    f"for configured sell-side quoting (deficit: {deficit_pct}%). "
                    f"skip_rebalance={self.config.skip_rebalance}. "
                    f"Sell orders may be clipped or skipped. "
                    f"Consider reducing total_amount_quote, pre-funding {base_asset}, "
                    f"or enabling rebalance."
                )
            elif required_base > 0:
                self.logger().info(
                    f"Sell-side inventory OK: {self.config.trading_pair} has "
                    f"{available:.8f} {base_asset} available, needs {required_base:.8f}"
                )
        except Exception as e:
            self.logger().debug(f"Could not check sell-side inventory: {e}")

    def check_position_rebalance(self) -> Optional[CreateExecutorAction]:
        """
        Check if position needs rebalancing and create OrderExecutor to acquire missing base asset.
        Only applies to spot trading (not perpetual contracts).
        """
        if "_perpetual" in self.config.connector_name or "reference_price" not in self.processed_data:
            return None

        # Startup health check: warn if under-seeded for sell side with rebalance disabled
        if self.config.skip_rebalance:
            if not getattr(self, '_under_seeded_warning_emitted', False):
                self._check_sell_side_inventory()
                self._under_seeded_warning_emitted = True
            return None

        active_rebalance = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active and x.custom_info.get("level_id") == "position_rebalance"
        )
        if len(active_rebalance) > 0:
            self.logger().debug(
                f"Rebalance check for {self.config.trading_pair}: "
                f"skipped — active rebalance executor exists (id={active_rebalance[0].id})"
            )
            return None

        # Check cooldown — suppress rebalance if last attempt was too recent
        current_time = self.market_data_provider.time()
        time_since_last = current_time - self._last_rebalance_attempt_timestamp
        if time_since_last < self.config.rebalance_cooldown_time:
            return None

        required_base_amount = self.config.get_required_base_amount(Decimal(self.processed_data["reference_price"]))
        current_base_amount = self.get_current_base_position()

        # Calculate the difference
        base_amount_diff = required_base_amount - current_base_amount

        # Account for in-flight buy orders already working to replenish
        inflight_buy = Decimal("0")
        if base_amount_diff > 0:
            inflight_buy = self.get_inflight_buy_base_amount()
            base_amount_diff = max(Decimal("0"), base_amount_diff - inflight_buy)

        # Check if difference exceeds threshold
        threshold_amount = required_base_amount * self.config.position_rebalance_threshold_pct

        # Structured debug log for every rebalance evaluation
        self.logger().debug(
            f"Rebalance check for {self.config.trading_pair}: "
            f"required_base={required_base_amount:.6f}, "
            f"held_base={current_base_amount:.6f}, "
            f"inflight_buy={inflight_buy:.6f}, "
            f"adjusted_diff={base_amount_diff:.6f}, "
            f"threshold={threshold_amount:.6f}, "
            f"will_rebalance={abs(base_amount_diff) > threshold_amount}, "
            f"active_executors_count={len(self.executors_info)}, "
            f"positions_held_count={len(self.positions_held)}"
        )

        if abs(base_amount_diff) > threshold_amount:
            # We need to rebalance
            self._last_rebalance_attempt_timestamp = current_time
            if base_amount_diff > 0:
                # Need to buy more base asset
                return self.create_position_rebalance_order(TradeType.BUY, abs(base_amount_diff))
            else:
                # Need to sell base asset (unlikely for market making but possible)
                return self.create_position_rebalance_order(TradeType.SELL, abs(base_amount_diff))

        return None

    def get_inflight_buy_base_amount(self) -> Decimal:
        """
        Get the total base amount of active buy-side executors that are working to
        replenish inventory (includes both PMM buy levels and pending rebalance buys).
        Uses defensive attribute access to handle different executor config types.
        """
        inflight_buy_amount = Decimal("0")
        matched_executors = []

        for executor in self.executors_info:
            if not executor.is_active:
                continue

            config = executor.config
            # Defensive attribute access — different config types may have different shapes
            try:
                exec_connector = config.connector_name
                exec_pair = config.trading_pair
                exec_side = config.side
                exec_amount = config.amount
            except AttributeError:
                continue

            if (exec_connector == self.config.connector_name and
                    exec_pair == self.config.trading_pair and
                    exec_side == TradeType.BUY):
                inflight_buy_amount += exec_amount
                matched_executors.append({
                    "id": executor.id,
                    "type": executor.type,
                    "amount": str(exec_amount),
                    "level_id": executor.custom_info.get("level_id", "unknown"),
                })

        if matched_executors:
            self.logger().debug(
                f"Inflight buy detection for {self.config.trading_pair}: "
                f"found {len(matched_executors)} active BUY executor(s), "
                f"total_inflight={inflight_buy_amount}. "
                f"Details: {matched_executors}"
            )

        return inflight_buy_amount

    def get_current_base_position(self) -> Decimal:
        """
        Get current base asset position from positions held.
        """
        total_base_amount = Decimal("0")

        for position in self.positions_held:
            if (position.connector_name == self.config.connector_name and
                    position.trading_pair == self.config.trading_pair):
                # Calculate net base position
                if position.side == TradeType.BUY:
                    total_base_amount += position.amount
                else:  # SELL position
                    total_base_amount -= position.amount

        return total_base_amount

    def get_effective_base_inventory(self) -> dict:
        """
        Compute a full breakdown of effective base inventory for rebalance decisions.
        Returns a dict with all components for debugging and testing.
        """
        held = self.get_current_base_position()
        inflight_buy = self.get_inflight_buy_base_amount()
        required = self.config.get_required_base_amount(
            Decimal(self.processed_data.get("reference_price", "0"))
        ) if "reference_price" in self.processed_data else Decimal("0")

        raw_diff = required - held
        adjusted_diff = max(Decimal("0"), raw_diff - inflight_buy) if raw_diff > 0 else raw_diff
        threshold = required * self.config.position_rebalance_threshold_pct

        return {
            "required_base": required,
            "held_base": held,
            "inflight_buy_base": inflight_buy,
            "raw_shortage": raw_diff,
            "adjusted_shortage": adjusted_diff,
            "threshold": threshold,
            "needs_rebalance": abs(adjusted_diff) > threshold,
        }

    def create_position_rebalance_order(self, side: TradeType, amount: Decimal) -> CreateExecutorAction:
        """
        Create an OrderExecutor to rebalance position.
        """
        reference_price = self.processed_data["reference_price"]

        # Use market price for quick execution
        order_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            execution_strategy=ExecutionStrategy.MARKET,
            side=side,
            amount=amount,
            price=reference_price,  # Will be ignored for market orders
            level_id="position_rebalance",
        )

        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=order_config
        )
