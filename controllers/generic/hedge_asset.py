"""
Explanation:

This strategy tracks the spot balance of a single asset on one exchange and maintains a hedge on a perpetual exchange
using a fixed, user-defined hedge ratio. It continuously compares the target hedge size (spot_balance × hedge_ratio)
with the actual short position and adjusts only when the difference exceeds a minimum notional threshold and enough
time has passed since the last order. This prevents overtrading while keeping the exposure appropriately hedged. The
user can manually update the hedge ratio in the config, and the controller will rebalance toward the new target size,
reducing or increasing the short position as needed. This allows safe, controlled management of spot inventory with
minimal noise and predictable hedge behavior.
"""
from decimal import Decimal
from typing import Dict, List

from pydantic import Field, model_validator

from hummingbot.core.data_type.common import MarketDict, PositionAction, PositionMode, TradeType
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction

# CDX-007: spot assets that legitimately hedge via a perp on a different (canonical)
# base symbol. Only 1:1-redeemable wrappers belong here — anything with a unit
# conversion (e.g. 1000SHIB) would reintroduce the mixed-unit subtraction the
# validator exists to prevent.
WRAPPED_TOKEN_ALIASES: Dict[str, str] = {
    "WBTC": "BTC",
    "WETH": "ETH",
    "WBETH": "ETH",
    "STETH": "ETH",
    "WSTETH": "ETH",
    "WSOL": "SOL",
    "WBNB": "BNB",
    "WMATIC": "MATIC",
    "WPOL": "POL",
    "WAVAX": "AVAX",
}


class HedgeAssetConfig(ControllerConfigBase):
    """
    Configuration required to run the GridStrike strategy for one connector and trading pair.
    """
    controller_type: str = "generic"
    controller_name: str = "hedge_asset"
    total_amount_quote: Decimal = Decimal(0)

    # Spot connector
    spot_connector_name: str = "binance"
    asset_to_hedge: str = "SOL"

    # Perpetual connector
    hedge_connector_name: str = "binance_perpetual"
    hedge_trading_pair: str = "SOL-USDT"
    leverage: int = 20
    position_mode: PositionMode = PositionMode.HEDGE

    # Hedge params
    hedge_ratio: Decimal = Field(default=Decimal("0"), ge=0, le=1, json_schema_extra={"is_updatable": True})
    min_notional_size: float = Field(default=10, ge=0)
    cooldown_time: float = Field(default=10.0, ge=0)
    # CLA-403: optional cap on a single hedge order, denominated in the hedge pair's
    # quote asset. 0 disables the cap (legacy sizing). Any positive value bounds the
    # blast radius of one adjustment: a wrong gap can then only be chased one capped
    # step per cooldown window instead of in a single full-size MARKET order.
    max_hedge_order_quote: Decimal = Field(default=Decimal("0"), ge=0, json_schema_extra={"is_updatable": True})

    # GEN-13: quote asset of the spot reference pair registered for the hedged asset.
    # Previously hardcoded to USDC — a nonexistent <asset>-USDC market blocks connector
    # readiness for the whole bot. Default preserves prior behavior.
    spot_reference_quote: str = Field(default="USDC")

    @model_validator(mode="after")
    def validate_asset_matches_hedge_pair(self):
        # CDX-007: `hedge_position_gap` subtracts the perp position (hedge-pair base
        # units) from the spot balance (`asset_to_hedge` units) and submits the result
        # on the hedge pair. If the two assets differ the mixed-unit gap silently
        # trades the wrong asset (BTC balance + SOL-USDT pair -> sells SOL), so the
        # pairing is enforced at config time, modulo the wrapped-token allow-list.
        asset = self.asset_to_hedge.strip().upper()
        hedge_base = self.hedge_trading_pair.split("-")[0].strip().upper()
        canonical_asset = WRAPPED_TOKEN_ALIASES.get(asset, asset)
        if canonical_asset != hedge_base:
            raise ValueError(
                f"asset_to_hedge '{self.asset_to_hedge}' does not match the base asset "
                f"'{hedge_base}' of hedge_trading_pair '{self.hedge_trading_pair}'. "
                f"The hedge gap is computed in {self.asset_to_hedge} units but orders are "
                f"placed in {hedge_base} units — this would hedge the wrong asset. "
                f"Use a hedge pair whose base is {canonical_asset}, or a wrapped alias "
                f"from the allow-list: {sorted(WRAPPED_TOKEN_ALIASES)}")
        return self

    @property
    def spot_reference_pair(self) -> str:
        return f"{self.asset_to_hedge}-{self.spot_reference_quote}"

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets.add_or_update(self.spot_connector_name, self.spot_reference_pair)
        markets.add_or_update(self.hedge_connector_name, self.hedge_trading_pair)
        return markets


class HedgeAssetController(ControllerBase):
    _WARNING_INTERVAL = 30.0

    def __init__(self, config: HedgeAssetConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.perp_collateral_asset = self.config.hedge_trading_pair.split("-")[1]
        # CLA-403: rate-limit state for the fail-closed balance-read warnings
        self._last_suspect_balance_warning_ts: float = 0.0
        self._last_no_collateral_warning_ts: float = 0.0
        self.set_leverage_and_position_mode()

    def set_leverage_and_position_mode(self):
        connector = self.market_data_provider.get_connector(self.config.hedge_connector_name)
        connector.set_leverage(leverage=self.config.leverage, trading_pair=self.config.hedge_trading_pair)
        connector.set_position_mode(self.config.position_mode)

    @property
    def hedge_position_size(self) -> Decimal:
        hedge_positions = [position for position in self.positions_held if
                           position.connector_name == self.config.hedge_connector_name and
                           position.trading_pair == self.config.hedge_trading_pair and
                           position.side == TradeType.SELL]
        if len(hedge_positions) > 0:
            hedge_position = hedge_positions[0]
            hedge_position_size = hedge_position.amount
        else:
            hedge_position_size = Decimal("0")
        return hedge_position_size

    @property
    def last_hedge_timestamp(self) -> float:
        if len(self.executors_info) > 0:
            return self.executors_info[-1].timestamp
        return 0

    @property
    def in_flight_hedge_amount(self) -> Decimal:
        """
        GEN-13: signed base amount of active hedge order executors on the hedge pair.
        SELL adds to the short when it fills, BUY reduces it — the gap must be computed
        as if those orders were already filled, otherwise every tick inside the fill-settle
        window re-hedges the same gap (cooldown was the only guard).
        """
        in_flight = Decimal("0")
        for executor in self.executors_info:
            if not executor.is_active or executor.type != "order_executor":
                continue
            if (executor.config.connector_name != self.config.hedge_connector_name or
                    executor.config.trading_pair != self.config.hedge_trading_pair):
                continue
            if executor.config.side == TradeType.SELL:
                in_flight += executor.config.amount
            else:
                in_flight -= executor.config.amount
        return in_flight

    @staticmethod
    def _normalize_balance_read(raw) -> Decimal:
        """CLA-403: collapse absent/None/non-finite/negative balance reads to 0."""
        if isinstance(raw, Decimal) and raw.is_finite() and raw > 0:
            return raw
        return Decimal("0")

    async def update_processed_data(self):
        """
        Compute current spot balance, hedge position size, current hedge ratio, last hedge time, current hedge gap quote
        """
        current_price = self.market_data_provider.get_price_by_type(self.config.hedge_connector_name, self.config.hedge_trading_pair)
        spot_balance = self._normalize_balance_read(
            self.market_data_provider.get_balance(self.config.spot_connector_name, self.config.asset_to_hedge))
        perp_available_balance = self._normalize_balance_read(
            self.market_data_provider.get_available_balance(self.config.hedge_connector_name, self.perp_collateral_asset))
        hedge_position_size = self.hedge_position_size
        in_flight_hedge_amount = self.in_flight_hedge_amount
        # CLA-403: `get_balance` returns 0 for a missing key, so a transient
        # zero/absent read while a short hedge is held is indistinguishable from a
        # genuinely flat spot position — but acting on it fires a full-size MARKET
        # unwind (and a mirror re-hedge when the read recovers). Fail closed: keep
        # the hedge, warn, and let the next reliable read drive the adjustment.
        spot_balance_suspect = spot_balance <= 0 and hedge_position_size > 0
        if spot_balance_suspect:
            now = self.market_data_provider.time()
            if now - self._last_suspect_balance_warning_ts >= self._WARNING_INTERVAL:
                self._last_suspect_balance_warning_ts = now
                self.logger().warning(
                    f"Spot balance read for {self.config.asset_to_hedge} on "
                    f"{self.config.spot_connector_name} is zero/absent while a hedge of "
                    f"{hedge_position_size} is held. Treating the read as unreliable and "
                    f"skipping hedge adjustment (no unwind will be placed on this read).")
        # Deduct in-flight hedge orders from the gap (GEN-13)
        hedge_position_gap = spot_balance * self.config.hedge_ratio - hedge_position_size - in_flight_hedge_amount
        hedge_position_gap_quote = hedge_position_gap * current_price
        last_hedge_timestamp = self.last_hedge_timestamp

        # if these conditions are true we are allowed to execute a trade
        cool_down_time_condition = last_hedge_timestamp + self.config.cooldown_time < self.market_data_provider.time()
        min_notional_size_condition = abs(hedge_position_gap_quote) >= self.config.min_notional_size
        self.processed_data.update({
            "current_price": current_price,
            "spot_balance": spot_balance,
            "spot_balance_suspect": spot_balance_suspect,
            "perp_available_balance": perp_available_balance,
            "hedge_position_size": hedge_position_size,
            "in_flight_hedge_amount": in_flight_hedge_amount,
            "hedge_position_gap": hedge_position_gap,
            "hedge_position_gap_quote": hedge_position_gap_quote,
            "last_hedge_timestamp": last_hedge_timestamp,
            "cool_down_time_condition": cool_down_time_condition,
            "min_notional_size_condition": min_notional_size_condition,
        })

    def determine_executor_actions(self) -> List[ExecutorAction]:
        # CLA-403: a zero/absent spot read while a hedge is held is not evidence of a
        # flat position — the unwind is skipped until the balance read is reliable.
        if self.processed_data.get("spot_balance_suspect", False):
            return []
        if self.processed_data["cool_down_time_condition"] and self.processed_data["min_notional_size_condition"]:
            side = TradeType.SELL if self.processed_data["hedge_position_gap"] >= 0 else TradeType.BUY
            current_price = self.processed_data["current_price"]
            price_valid = isinstance(current_price, Decimal) and current_price.is_finite() and current_price > 0
            amount = abs(self.processed_data["hedge_position_gap"])
            # CLA-403: cap the per-order hedge step (0 = disabled)
            if self.config.max_hedge_order_quote > 0 and price_valid:
                amount = min(amount, self.config.max_hedge_order_quote / current_price)
            if side == TradeType.SELL:
                # CLA-403: opening/extending the short requires collateral — use the
                # (previously read-but-unused) perp available balance to bound the
                # order instead of submitting a MARKET order the margin cannot back.
                perp_available_balance = self.processed_data["perp_available_balance"]
                if perp_available_balance <= 0:
                    now = self.market_data_provider.time()
                    if now - self._last_no_collateral_warning_ts >= self._WARNING_INTERVAL:
                        self._last_no_collateral_warning_ts = now
                        self.logger().warning(
                            f"No available {self.perp_collateral_asset} collateral read on "
                            f"{self.config.hedge_connector_name}; skipping hedge SELL of {amount}.")
                    return []
                if price_valid:
                    amount = min(amount, perp_available_balance * self.config.leverage / current_price)
            if amount <= 0:
                return []
            order_executor_config = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.hedge_connector_name,
                trading_pair=self.config.hedge_trading_pair,
                side=side,
                amount=amount,
                price=current_price,
                leverage=self.config.leverage,
                position_action=PositionAction.CLOSE if side == TradeType.BUY else PositionAction.OPEN,
                execution_strategy=ExecutionStrategy.MARKET
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=order_executor_config)]
        return []

    def to_format_status(self) -> List[str]:
        """
        These report will be showing the metrics that are important to determine the state of the hedge.
        """
        lines = []

        # Get data
        spot_balance = self.processed_data.get("spot_balance", Decimal("0"))
        hedge_position = self.processed_data.get("hedge_position_size", Decimal("0"))
        perp_balance = self.processed_data.get("perp_available_balance", Decimal("0"))
        current_price = self.processed_data.get("current_price", Decimal("0"))
        gap = self.processed_data.get("hedge_position_gap", Decimal("0"))
        gap_quote = self.processed_data.get("hedge_position_gap_quote", Decimal("0"))
        cooldown_ok = self.processed_data.get("cool_down_time_condition", False)
        notional_ok = self.processed_data.get("min_notional_size_condition", False)

        # Calculate theoretical hedge
        theoretical_hedge = spot_balance * self.config.hedge_ratio

        # Status indicators
        cooldown_status = "✓" if cooldown_ok else "✗"
        notional_status = "✓" if notional_ok else "✗"

        # Header
        lines.append(f"\n{'=' * 65}")
        lines.append(f"  HEDGE ASSET CONTROLLER: {self.config.asset_to_hedge} @ {current_price:.4f} {self.perp_collateral_asset}")
        lines.append(f"{'=' * 65}")

        # Calculation flow
        lines.append(f"  Spot Balance:      {spot_balance:>10.4f} {self.config.asset_to_hedge}")
        lines.append(f"  × Hedge Ratio:     {self.config.hedge_ratio:>10.1%}")
        lines.append(f"  {'─' * 61}")
        lines.append(f"  = Target Hedge:    {theoretical_hedge:>10.4f} {self.config.asset_to_hedge}")
        lines.append(f"  - Current Hedge:   {hedge_position:>10.4f} {self.config.asset_to_hedge}")
        lines.append(f"  {'─' * 61}")
        lines.append(f"  = Gap:             {gap:>10.4f} {self.config.asset_to_hedge}  ({gap_quote:>8.2f} {self.perp_collateral_asset})")
        lines.append("")
        lines.append(f"  Perp Balance:      {perp_balance:>10.2f} {self.perp_collateral_asset}")
        lines.append("")

        # Trading conditions
        lines.append("  Trading Conditions:")
        lines.append(f"    Cooldown ({self.config.cooldown_time:.0f}s):      {cooldown_status}")
        lines.append(f"    Min Notional (≥{self.config.min_notional_size:.0f} {self.perp_collateral_asset}): {notional_status}")

        lines.append(f"{'=' * 65}\n")

        return lines
