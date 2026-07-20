"""
hbstrat_fix Phase 6 tests — ai_livestream MQTT barrier hardening
(CDX-002 / CLA-008 / CLA-2b-001 / CLA-404 cluster).

The MQTT signal topic is UNAUTHENTICATED INPUT: any client the broker admits
can publish. The payload's `target_pct` multiplies stop-loss/take-profit/
trailing via `new_instance_with_adjusted_volatility`, so before this fix:
- `target_pct=0` zeroed both barriers (the executor's truthiness gates then
  skip them entirely — an unprotected position);
- an ABSENT `target_pct` applied the `0.01` default — a 3% stop became 0.03%
  (100x shrink);
- a huge `target_pct` widened barriers without bound.

Expected values below are derived from the finding/spec (configured barrier x
factor, floor = configured barrier x min_volatility_factor), never captured
from running the implementation.
"""
import asyncio
import math
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from controllers.directional_trading.ai_livestream import (
    NEUTRAL_VOLATILITY_FACTOR,
    AILivestreamController,
    AILivestreamControllerConfig,
)
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop

# Operator-configured barriers used across the tests (spec-derived, explicit).
STOP_LOSS = Decimal("0.03")
TAKE_PROFIT = Decimal("0.02")
TRAILING_ACTIVATION = Decimal("0.015")
TRAILING_DELTA = Decimal("0.003")
MIN_FACTOR = 0.1
MAX_FACTOR = 10.0

LONG_PAYLOAD_PROBS = [0.1, 0.2, 0.7]  # long prob 0.7 > default threshold 0.5 -> signal 1 if accepted


class AILivestreamBarrierTestBase(IsolatedAsyncioWrapperTestCase):
    def _make_controller(self, **config_overrides):
        params = dict(
            id="test-ai-p6",
            controller_name="ai_livestream",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"),
            stop_loss=STOP_LOSS,
            take_profit=TAKE_PROFIT,
            trailing_stop=TrailingStop(
                activation_price=TRAILING_ACTIVATION, trailing_delta=TRAILING_DELTA),
            min_volatility_factor=MIN_FACTOR,
            max_volatility_factor=MAX_FACTOR,
        )
        params.update(config_overrides)
        config = AILivestreamControllerConfig(**params)
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=1000.0)
        controller = AILivestreamController(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def _barriers(self, controller):
        executor_config = controller.get_executor_config(
            TradeType.BUY, Decimal("100"), Decimal("1"))
        return executor_config.triple_barrier_config


class TestAbsentTargetPctIsNeutral(AILivestreamBarrierTestBase):
    def test_absent_target_pct_leaves_barriers_unchanged(self):
        # Old code used features.get("target_pct", 0.01): a 3% stop became
        # 0.03% (100x shrink). Absent target_pct must be NEUTRAL (factor 1).
        controller = self._make_controller()
        controller._handle_ml_signal({"probabilities": LONG_PAYLOAD_PROBS}, "topic")
        self.assertEqual(1, controller.processed_data["signal"])  # payload accepted
        barriers = self._barriers(controller)
        self.assertEqual(STOP_LOSS, barriers.stop_loss)
        self.assertEqual(TAKE_PROFIT, barriers.take_profit)
        self.assertEqual(TRAILING_ACTIVATION, barriers.trailing_stop.activation_price)
        self.assertEqual(TRAILING_DELTA, barriers.trailing_stop.trailing_delta)

    def test_absent_target_pct_neutral_under_non_default_band(self):
        # CDX-R02 regression guard: with a non-default band whose floor is
        # below 1 (the only kind the validator now accepts), an absent
        # target_pct must still leave the barriers EXACTLY unchanged — the
        # floor may never lift the neutral result above the configured values.
        controller = self._make_controller(
            min_volatility_factor=0.5, max_volatility_factor=3.0)
        controller._handle_ml_signal({"probabilities": LONG_PAYLOAD_PROBS}, "topic")
        self.assertEqual(1, controller.processed_data["signal"])
        barriers = self._barriers(controller)
        self.assertEqual(STOP_LOSS, barriers.stop_loss)
        self.assertEqual(TAKE_PROFIT, barriers.take_profit)
        self.assertEqual(TRAILING_ACTIVATION, barriers.trailing_stop.activation_price)
        self.assertEqual(TRAILING_DELTA, barriers.trailing_stop.trailing_delta)

    def test_neutral_factor_constant_is_one(self):
        # The neutral multiplier is 1, not the old 0.01 default.
        self.assertEqual(1.0, NEUTRAL_VOLATILITY_FACTOR)

    def test_empty_features_yield_neutral_factor(self):
        controller = self._make_controller()
        self.assertEqual(1.0, controller._current_volatility_factor())


class TestZeroTargetPct(AILivestreamBarrierTestBase):
    def test_zero_target_pct_rejected_at_boundary(self):
        # target_pct=0 would scale both barriers to Decimal("0"), which the
        # executor truthiness gates skip entirely. Must reject-and-zero-signal.
        controller = self._make_controller()
        payload = {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 0}
        controller._handle_ml_signal(payload, "topic")
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertEqual({}, controller.processed_data["features"])  # garbage not stored
        self.assertIsNone(controller._last_signal_ts)

    def test_zero_target_pct_cannot_zero_barriers_even_if_injected(self):
        # Defense in depth: even if a zero factor bypasses the boundary and
        # lands in processed_data, the barriers must hold (neutral fallback +
        # floor), never Decimal("0").
        controller = self._make_controller()
        controller.processed_data["signal"] = 1
        controller.processed_data["features"] = {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 0}
        barriers = self._barriers(controller)
        self.assertGreater(barriers.stop_loss, Decimal("0"))
        self.assertGreater(barriers.take_profit, Decimal("0"))
        self.assertEqual(STOP_LOSS, barriers.stop_loss)  # neutral fallback
        self.assertEqual(TAKE_PROFIT, barriers.take_profit)

    def test_barrier_floor_backstop_lifts_zeroed_barriers(self):
        # Exercise the floor arithmetic directly: a factor-0 scale must be
        # lifted to configured_barrier * min_volatility_factor, never 0.
        controller = self._make_controller()
        zeroed = controller.config.triple_barrier_config.new_instance_with_adjusted_volatility(0.0)
        self.assertEqual(Decimal("0"), zeroed.stop_loss)  # precondition: scale really zeroes
        floored = controller._apply_barrier_floors(zeroed)
        self.assertEqual(STOP_LOSS * Decimal(str(MIN_FACTOR)), floored.stop_loss)
        self.assertEqual(TAKE_PROFIT * Decimal(str(MIN_FACTOR)), floored.take_profit)
        self.assertEqual(TRAILING_ACTIVATION * Decimal(str(MIN_FACTOR)),
                         floored.trailing_stop.activation_price)
        self.assertEqual(TRAILING_DELTA * Decimal(str(MIN_FACTOR)),
                         floored.trailing_stop.trailing_delta)

    def test_floor_does_not_touch_in_band_barriers(self):
        # An in-band scale (>= min factor) must pass through untouched.
        controller = self._make_controller()
        scaled = controller.config.triple_barrier_config.new_instance_with_adjusted_volatility(2.0)
        floored = controller._apply_barrier_floors(scaled)
        self.assertEqual(STOP_LOSS * Decimal("2"), floored.stop_loss)
        self.assertEqual(TAKE_PROFIT * Decimal("2"), floored.take_profit)

    def test_floor_preserves_none_barriers(self):
        # A barrier the operator disabled (None) stays None — the floor only
        # protects configured barriers from being scaled away.
        controller = self._make_controller(take_profit=None, trailing_stop=None)
        zeroed = controller.config.triple_barrier_config.new_instance_with_adjusted_volatility(0.0)
        floored = controller._apply_barrier_floors(zeroed)
        self.assertIsNone(floored.take_profit)
        self.assertIsNone(floored.trailing_stop)
        self.assertEqual(STOP_LOSS * Decimal(str(MIN_FACTOR)), floored.stop_loss)


class TestOutOfBandTargetPct(AILivestreamBarrierTestBase):
    def test_huge_target_pct_rejected_at_boundary(self):
        controller = self._make_controller()
        payload = {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 1000.0}  # > max 10
        controller._handle_ml_signal(payload, "topic")
        self.assertEqual(0, controller.processed_data["signal"])
        self.assertEqual({}, controller.processed_data["features"])

    def test_below_band_target_pct_rejected_at_boundary(self):
        controller = self._make_controller()
        payload = {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 0.01}  # < min 0.1
        controller._handle_ml_signal(payload, "topic")
        self.assertEqual(0, controller.processed_data["signal"])

    def test_injected_huge_target_pct_falls_back_to_neutral(self):
        # Not silently clamped to the band edge: an out-of-band factor that
        # somehow reaches sizing uses the neutral factor (configured barriers).
        controller = self._make_controller()
        controller.processed_data["features"] = {"target_pct": 1000.0}
        barriers = self._barriers(controller)
        self.assertEqual(STOP_LOSS, barriers.stop_loss)
        self.assertEqual(TAKE_PROFIT, barriers.take_profit)

    def test_band_edges_are_accepted(self):
        controller = self._make_controller()
        self.assertEqual(MIN_FACTOR, controller._validated_volatility_factor(MIN_FACTOR))
        self.assertEqual(MAX_FACTOR, controller._validated_volatility_factor(MAX_FACTOR))


class TestValidPayloadScalesBarriers(AILivestreamBarrierTestBase):
    def test_in_band_target_pct_scales_all_barriers(self):
        controller = self._make_controller()
        payload = {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 2.0}
        controller._handle_ml_signal(payload, "topic")
        self.assertEqual(1, controller.processed_data["signal"])
        self.assertEqual(1000.0, controller._last_signal_ts)
        barriers = self._barriers(controller)
        self.assertEqual(STOP_LOSS * Decimal("2"), barriers.stop_loss)
        self.assertEqual(TAKE_PROFIT * Decimal("2"), barriers.take_profit)
        self.assertEqual(TRAILING_ACTIVATION * Decimal("2"), barriers.trailing_stop.activation_price)
        self.assertEqual(TRAILING_DELTA * Decimal("2"), barriers.trailing_stop.trailing_delta)

    def test_short_signal_still_works(self):
        controller = self._make_controller()
        controller._handle_ml_signal({"probabilities": [0.8, 0.1, 0.1], "target_pct": 1.0}, "topic")
        self.assertEqual(-1, controller.processed_data["signal"])

    def test_neutral_probabilities_zero_signal(self):
        controller = self._make_controller()
        controller._handle_ml_signal({"probabilities": [0.3, 0.4, 0.3]}, "topic")
        self.assertEqual(0, controller.processed_data["signal"])


class TestMalformedPayloadZeroesSignal(AILivestreamBarrierTestBase):
    MALFORMED_PAYLOADS = [
        None,
        "not-a-dict",
        [0.1, 0.2, 0.7],
        {},
        {"target_pct": 1.0},                                        # no probabilities
        {"probabilities": None},
        {"probabilities": [0.1, 0.9]},                              # wrong length
        {"probabilities": [0.1, 0.2, 0.3, 0.4]},                    # wrong length
        {"probabilities": [0.1, 0.2, float("nan")]},                # non-finite prob
        {"probabilities": [0.1, 0.2, float("inf")]},
        {"probabilities": [0.1, 0.2, 5.0]},                         # prob out of [0, 1]
        {"probabilities": [0.1, 0.2, -0.1]},
        {"probabilities": [0.1, 0.2, "0.7"]},                       # non-numeric prob
        {"probabilities": [0.1, 0.2, True]},                        # bool is not a probability
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": float("nan")},
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": float("inf")},
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": -1.0},
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": "2.0"},
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": True},
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": None},
        # Oversized JSON integers: float() raises OverflowError past ~1e308,
        # which must not escape validation (CDX-R01).
        {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 10 ** 400},
        {"probabilities": [0.1, 0.2, 10 ** 400]},
    ]

    def test_each_malformed_payload_zeroes_signal_without_raising(self):
        for payload in self.MALFORMED_PAYLOADS:
            with self.subTest(payload=payload):
                controller = self._make_controller()
                controller.processed_data["signal"] = 1  # pre-armed live signal
                controller._handle_ml_signal(payload, "topic")
                self.assertEqual(0, controller.processed_data["signal"])
                self.assertEqual({}, controller.processed_data["features"])

    def test_malformed_payload_produces_no_create_action(self):
        # End to end: a rejected payload must not size an order.
        controller = self._make_controller()
        controller.processed_data["signal"] = 1
        controller._handle_ml_signal({"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 0}, "topic")
        self.assertEqual([], controller.create_actions_proposal())

    def test_oversized_target_pct_zeroes_previously_armed_signal(self):
        # CDX-R01 reproduction: arm a valid long signal, then send a payload
        # whose target_pct is a JSON integer too large for float(). The old
        # code raised OverflowError inside validation, so the reject-and-zero
        # branch never ran and the stale armed signal stayed live.
        controller = self._make_controller()
        controller._handle_ml_signal(
            {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 2.0}, "topic")
        self.assertEqual(1, controller.processed_data["signal"])  # precondition: armed
        controller._handle_ml_signal(
            {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 10 ** 400}, "topic")
        self.assertEqual(0, controller.processed_data["signal"])

    def test_oversized_probability_zeroes_previously_armed_signal(self):
        # Same CDX-R01 mechanism via the probability path.
        controller = self._make_controller()
        controller._handle_ml_signal(
            {"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 2.0}, "topic")
        self.assertEqual(1, controller.processed_data["signal"])
        controller._handle_ml_signal({"probabilities": [0.1, 0.2, 10 ** 400]}, "topic")
        self.assertEqual(0, controller.processed_data["signal"])

    def test_valid_payload_after_rejection_recovers(self):
        # A reject must not wedge the controller: the next valid payload works.
        controller = self._make_controller()
        controller._handle_ml_signal({"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 0}, "topic")
        self.assertEqual(0, controller.processed_data["signal"])
        controller._handle_ml_signal({"probabilities": LONG_PAYLOAD_PROBS, "target_pct": 2.0}, "topic")
        self.assertEqual(1, controller.processed_data["signal"])
        self.assertEqual(2.0, controller.processed_data["features"]["target_pct"])


class TestVolatilityBandConfigValidation(AILivestreamBarrierTestBase):
    def _config_params(self, **overrides):
        params = dict(
            id="test-ai-p6-cfg",
            controller_name="ai_livestream",
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT",
            total_amount_quote=Decimal("100"),
        )
        params.update(overrides)
        return params

    def test_zero_min_factor_rejected(self):
        with self.assertRaises(ValidationError):
            AILivestreamControllerConfig(**self._config_params(min_volatility_factor=0.0))

    def test_negative_min_factor_rejected(self):
        with self.assertRaises(ValidationError):
            AILivestreamControllerConfig(**self._config_params(min_volatility_factor=-0.5))

    def test_nan_min_factor_rejected(self):
        with self.assertRaises(ValidationError):
            AILivestreamControllerConfig(**self._config_params(min_volatility_factor=float("nan")))

    def test_infinite_max_factor_rejected(self):
        with self.assertRaises(ValidationError):
            AILivestreamControllerConfig(**self._config_params(max_volatility_factor=float("inf")))

    def test_min_factor_above_one_rejected(self):
        # CDX-R02: min_volatility_factor doubles as the barrier floor factor.
        # A lower bound above 1 would make the floor RAISE the neutral
        # (absent-target_pct) barriers — e.g. min=2.0 turns a 0.03 stop into
        # 0.06 — so it must be rejected at config time.
        with self.assertRaises(ValidationError):
            AILivestreamControllerConfig(**self._config_params(
                min_volatility_factor=2.0, max_volatility_factor=10.0))

    def test_min_factor_of_exactly_one_accepted(self):
        # Boundary: min=1 means "no shrink allowed", which never raises the
        # neutral barriers. Must remain a valid configuration.
        config = AILivestreamControllerConfig(**self._config_params(
            min_volatility_factor=1.0, max_volatility_factor=10.0))
        self.assertEqual(1.0, config.min_volatility_factor)

    def test_max_below_min_rejected(self):
        with self.assertRaises(ValidationError):
            AILivestreamControllerConfig(**self._config_params(
                min_volatility_factor=1.0, max_volatility_factor=0.5))

    def test_defaults_form_a_valid_band(self):
        config = AILivestreamControllerConfig(**self._config_params())
        self.assertGreater(config.min_volatility_factor, 0.0)
        self.assertTrue(math.isfinite(config.max_volatility_factor))
        self.assertLessEqual(config.min_volatility_factor, config.max_volatility_factor)
        # The neutral factor must sit inside the default band so an absent
        # target_pct is always representable.
        self.assertLessEqual(config.min_volatility_factor, NEUTRAL_VOLATILITY_FACTOR)
        self.assertGreaterEqual(NEUTRAL_VOLATILITY_FACTOR, config.min_volatility_factor)
        self.assertLessEqual(NEUTRAL_VOLATILITY_FACTOR, config.max_volatility_factor)
