import math
from decimal import Decimal
from typing import List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field, model_validator

from hummingbot.core.data_type.common import TradeType
from hummingbot.remote_iface.mqtt import ExternalTopicFactory
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TrailingStop,
    TripleBarrierConfig,
)

# Multiplier applied to the triple-barrier config when no (valid) target_pct is
# available: 1 leaves the operator-configured barriers untouched.
NEUTRAL_VOLATILITY_FACTOR = 1.0


class AILivestreamControllerConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "ai_livestream"
    long_threshold: float = Field(default=0.5, json_schema_extra={"is_updatable": True})
    short_threshold: float = Field(default=0.5, json_schema_extra={"is_updatable": True})
    topic: str = "hbot/predictions"
    signal_max_age: int = Field(default=300, gt=0, json_schema_extra={"is_updatable": True})
    # Accepted band for the externally-supplied volatility factor (target_pct).
    # The MQTT topic is UNAUTHENTICATED INPUT (any client the broker admits can
    # publish to it), so a payload factor outside this band is rejected outright
    # rather than clamped into a live order. The band also floors the scaled
    # barriers: no accepted payload can shrink stop-loss/take-profit below
    # configured_barrier * min_volatility_factor. min_volatility_factor must not
    # exceed 1 — the floor may only ever lower barriers back up toward the
    # configured values, never raise the neutral (factor-1) result above them.
    min_volatility_factor: float = Field(default=0.1, json_schema_extra={"is_updatable": True})
    max_volatility_factor: float = Field(default=10.0, json_schema_extra={"is_updatable": True})

    @model_validator(mode="after")
    def validate_volatility_band(self):
        lo = self.min_volatility_factor
        hi = self.max_volatility_factor
        if not (isinstance(lo, (int, float)) and math.isfinite(lo) and 0 < lo <= 1):
            raise ValueError(
                f"min_volatility_factor must be finite and in (0, 1] (it doubles as the barrier "
                f"floor factor, which must never widen neutral barriers), got {lo!r}")
        if not (isinstance(hi, (int, float)) and math.isfinite(hi) and hi >= lo):
            raise ValueError(
                f"max_volatility_factor must be finite and >= min_volatility_factor ({lo}), got {hi!r}")
        return self


class AILivestreamController(DirectionalTradingControllerBase):
    """
    Directional controller driven by ML signals published on an MQTT topic.

    SECURITY NOTE: the signal topic is UNAUTHENTICATED INPUT (authentication is
    whatever the MQTT broker enforces, nothing more). Every payload is treated
    as untrusted at the boundary: shape and values are validated in
    `_handle_ml_signal`, and the barrier multiplier is re-validated and floored
    before it can size a live order.
    """

    def __init__(self, config: AILivestreamControllerConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)
        self.processed_data = {"signal": 0, "features": {}}
        self._last_signal_ts: Optional[float] = None
        # Start ML signal listener
        self._init_ml_signal_listener()

    def _init_ml_signal_listener(self):
        """Initialize a listener for ML signals from the MQTT broker"""
        try:
            normalized_pair = self.config.trading_pair.replace("-", "_").lower()
            topic = f"{self.config.topic}/{normalized_pair}/ML_SIGNALS"
            self._ml_signal_listener = ExternalTopicFactory.create_async(
                topic=topic,
                callback=self._handle_ml_signal,
                use_bot_prefix=False,
            )
            self.logger().info("ML signal listener initialized successfully")
        except Exception as e:
            self.logger().error(f"Failed to initialize ML signal listener: {str(e)}")
            self._ml_signal_listener = None

    @staticmethod
    def _as_finite_float(raw) -> Optional[float]:
        """
        Total conversion of an untrusted numeric value: returns a finite float
        or None. Never raises — a JSON integer can be arbitrarily large and
        `float()` raises OverflowError past ~1e308.
        """
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        try:
            value = float(raw)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return value

    def _validated_volatility_factor(self, raw) -> Optional[float]:
        """
        Validate an externally-supplied barrier multiplier. Returns the factor
        as a float when it is a finite positive real number inside the
        configured [min_volatility_factor, max_volatility_factor] band, else
        None.
        """
        value = self._as_finite_float(raw)
        if value is None or value <= 0.0:
            return None
        if not (self.config.min_volatility_factor <= value <= self.config.max_volatility_factor):
            return None
        return value

    def _is_valid_ml_payload(self, payload) -> bool:
        """
        Shape/value validation for an incoming MQTT payload. The topic is
        unauthenticated input, so nothing in the payload is trusted:
        `probabilities` must be exactly three finite numbers in [0, 1], and
        `target_pct` (when present) must pass `_validated_volatility_factor`.
        """
        if not isinstance(payload, dict):
            return False
        probabilities = payload.get("probabilities")
        if not isinstance(probabilities, (list, tuple)) or len(probabilities) != 3:
            return False
        for p in probabilities:
            value = self._as_finite_float(p)
            if value is None or not (0.0 <= value <= 1.0):
                return False
        if "target_pct" in payload and self._validated_volatility_factor(payload["target_pct"]) is None:
            return False
        return True

    def _handle_ml_signal(self, signal: dict, topic: str):
        """
        Handle an incoming ML signal from the UNAUTHENTICATED MQTT topic.
        A malformed or out-of-band payload zeroes the signal (fail closed)
        instead of being clamped into a live order.
        """
        try:
            payload_ok = self._is_valid_ml_payload(signal)
        except Exception as e:
            # Defense in depth: validation of untrusted input must itself be
            # total. Any escape here would leave a previously armed signal
            # live (the MQTT dispatcher only logs callback exceptions).
            payload_ok = False
            self.logger().warning(
                f"ML payload validation raised {e!r}; treating payload as invalid.")
        if not payload_ok:
            self.processed_data["signal"] = 0
            self.logger().warning(
                f"Rejected invalid ML payload on unauthenticated topic {topic}; "
                f"signal zeroed. Payload: {signal!r}")
            return
        short, neutral, long = signal["probabilities"]
        if short > self.config.short_threshold:
            self.processed_data["signal"] = -1
        elif long > self.config.long_threshold:
            self.processed_data["signal"] = 1
        else:
            self.processed_data["signal"] = 0
        self.processed_data["features"] = signal
        self._last_signal_ts = self.market_data_provider.time()

    async def update_processed_data(self):
        if self._ml_signal_listener is None:
            self._init_ml_signal_listener()
        if self.processed_data.get("signal", 0) != 0:
            now = self.market_data_provider.time()
            if self._last_signal_ts is None or now - self._last_signal_ts > self.config.signal_max_age:
                self.logger().warning(
                    f"ML signal older than {self.config.signal_max_age}s — zeroing stale signal.")
                self.processed_data["signal"] = 0

    def stop(self):
        listener = getattr(self, "_ml_signal_listener", None)
        if listener is not None:
            try:
                ExternalTopicFactory.remove_listener(listener)
            except Exception as e:
                self.logger().warning(f"Failed to remove ML signal listener on stop: {str(e)}")
            self._ml_signal_listener = None
        super().stop()

    def _current_volatility_factor(self) -> float:
        """
        Barrier multiplier derived from the last accepted payload. An absent
        `target_pct` is neutral (1 — barriers unchanged); anything invalid or
        out-of-band falls back to neutral with a WARNING (defense in depth —
        the boundary validation should already have rejected it).
        """
        features = self.processed_data.get("features") or {}
        if "target_pct" not in features:
            return NEUTRAL_VOLATILITY_FACTOR
        factor = self._validated_volatility_factor(features["target_pct"])
        if factor is None:
            self.logger().warning(
                f"Invalid target_pct {features['target_pct']!r} reached order sizing; "
                f"using neutral volatility factor {NEUTRAL_VOLATILITY_FACTOR}.")
            return NEUTRAL_VOLATILITY_FACTOR
        return factor

    def _apply_barrier_floors(self, adjusted: TripleBarrierConfig) -> TripleBarrierConfig:
        """
        Floor the scaled barriers at configured_barrier * min_volatility_factor
        so no payload can scale stop-loss/take-profit/trailing to zero — a zero
        barrier is skipped by the executor's truthiness gates, silently removing
        the protection.
        """
        base = self.config.triple_barrier_config
        floor_factor = Decimal(str(self.config.min_volatility_factor))

        def floored(value: Optional[Decimal], base_value: Optional[Decimal]) -> Optional[Decimal]:
            if value is None or base_value is None:
                return value
            floor = base_value * floor_factor
            return value if value >= floor else floor

        trailing = adjusted.trailing_stop
        if trailing is not None and base.trailing_stop is not None:
            trailing = TrailingStop(
                activation_price=floored(trailing.activation_price, base.trailing_stop.activation_price),
                trailing_delta=floored(trailing.trailing_delta, base.trailing_stop.trailing_delta),
            )
        return TripleBarrierConfig(
            stop_loss=floored(adjusted.stop_loss, base.stop_loss),
            take_profit=floored(adjusted.take_profit, base.take_profit),
            time_limit=adjusted.time_limit,
            trailing_stop=trailing,
            open_order_type=adjusted.open_order_type,
            take_profit_order_type=adjusted.take_profit_order_type,
            stop_loss_order_type=adjusted.stop_loss_order_type,
            time_limit_order_type=adjusted.time_limit_order_type,
        )

    def get_executor_config(self, trade_type: TradeType, price: Decimal, amount: Decimal):
        """
        Get the executor config based on the trade_type, price and amount. This method can be overridden by the
        subclasses if required.
        """
        factor = self._current_volatility_factor()
        barriers = self._apply_barrier_floors(
            self.config.triple_barrier_config.new_instance_with_adjusted_volatility(volatility_factor=factor))
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=trade_type,
            entry_price=price,
            amount=amount,
            triple_barrier_config=barriers,
            leverage=self.config.leverage,
        )

    def to_format_status(self) -> List[str]:
        lines = []
        features = self.processed_data.get("features", {})
        lines.append(f"Signal: {self.processed_data.get('signal', 'N/A')}")
        lines.append(f"Timestamp: {features.get('timestamp', 'N/A')}")
        lines.append(f"Probabilities: {features.get('probabilities', 'N/A')}")
        lines.append(f"Target Pct: {features.get('target_pct', 'N/A')}")
        return lines
