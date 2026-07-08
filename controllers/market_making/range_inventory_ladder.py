
import asyncio
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import MarketDict, OrderType, PriceType, TradeType
from hummingbot.logger.structured_event_logger import get_structured_logger
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


def _parse_comma_list(value):
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return []
        return [item.strip() for item in value.split(",") if item.strip() != ""]
    if value is None:
        return []
    return value


def _safe_decimal(value, field_name: str = "value", default: Optional[str] = None) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return Decimal(default)
        raise ValueError(f"{field_name} cannot be blank")

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal value for {field_name}: {value}") from exc

    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal value")

    return parsed


class RangeInventoryLadderConfig(ControllerConfigBase):
    """
    Spot-only fixed price inventory ladder with managed-fund accounting.

    Key behaviors:
    - Uses wallet balances minus persisted reserve balances to infer the strategy-managed fund.
    - Allows an initial quote allocation cap plus an optional claimed base inventory slice.
    - Lets profits remain in the managed fund up to max_fund_value_quote.
    - Places fixed buy and sell levels as OrderExecutors so your repo's executor/orchestrator logging remains active.
    """
    controller_name: str = "range_inventory_ladder"
    controller_type: str = "market_making"

    connector_name: str = Field(
        default="binance",
        json_schema_extra={
            "prompt": "Enter the connector name (spot only, e.g. mexc, binance): ",
            "prompt_on_new": True,
        },
    )
    trading_pair: str = Field(
        default="ETH-USDT",
        json_schema_extra={
            "prompt": "Enter the trading pair (e.g. ETH-USDT): ",
            "prompt_on_new": True,
        },
    )

    total_amount_quote: Decimal = Field(
        default=Decimal("80"),
        json_schema_extra={
            "prompt": "Enter the initial managed quote amount (e.g. 80): ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )
    max_fund_value_quote: Decimal = Field(
        default=Decimal("5000"),
        json_schema_extra={
            "prompt": "Enter the hard cap for total deployable fund value in quote (e.g. 5000): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    shared_account_quote_quota: Optional[Decimal] = Field(
        default=None,
        description=(
            "Maximum quote asset this controller is allowed to assume is available "
            "when multiple controllers share the same connector account. When set, "
            "free_buy_budget_quote is clamped to min(available_quote_balance, quota). "
            "Leave unset (None) to use the current behavior (raw account balance, "
            "which can be reduced by other controllers on the same account)."
        ),
        json_schema_extra={
            "prompt": (
                "Optional cap on quote-asset availability for shared-account runs "
                "(blank = no cap): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    ledger_funded_budgets: bool = Field(
        default=True,
        description=(
            "Fund new buy/sell orders from THIS controller's own managed-fund ledger "
            "(owned_quote/owned_base minus its resting reservations), bounded by the live wallet "
            "as a safety floor, instead of from the raw shared wallet balance. Lets multiple "
            "controllers share one exchange account without `shared_account_quote_quota`. Set "
            "False for legacy wallet-funded behavior."
        ),
        json_schema_extra={
            "prompt": (
                "Fund new orders from this controller's own managed-fund ledger (lets multiple "
                "controllers share one account without quotas)? (True/False): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    use_wallet_balance: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": "Claim starting base inventory from the wallet? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )
    claimed_base_value_quote: Decimal = Field(
        default=Decimal("0"),
        json_schema_extra={
            "prompt": "Enter quote value of base inventory to claim on first start (e.g. 40). Use 0 for none: ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )
    claimed_base_amount: Optional[Decimal] = Field(
        default=None,
        description=(
            "Explicit base amount to claim from wallet on startup. "
            "If set, this overrides claimed_base_value_quote for base-asset seeding. "
            "Use this when you want precise control over the sell-side inventory "
            "without quote-to-base conversion ambiguity."
        ),
        json_schema_extra={"is_updatable": False},
    )

    # NOTE: buy_prices / sell_prices must remain defined before buy_amounts_pct / sell_amounts_pct.
    # The validators below derive default equal weights from the already-validated price lists.
    buy_prices: List[Decimal] = Field(
        default_factory=lambda: [
            Decimal("320"), Decimal("315"), Decimal("310"), Decimal("305"), Decimal("300"),
            Decimal("295"), Decimal("290"), Decimal("285"), Decimal("280"),
        ],
        json_schema_extra={
            "prompt": "Enter comma-separated fixed buy prices, highest to lowest: ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    buy_amounts_pct: Optional[List[Decimal]] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter comma-separated buy level weights (blank = equal sizing): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    sell_prices: List[Decimal] = Field(
        default_factory=lambda: [
            Decimal("340"), Decimal("345"), Decimal("350"), Decimal("355"), Decimal("360"),
        ],
        json_schema_extra={
            "prompt": "Enter comma-separated fixed sell prices, lowest to highest: ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    sell_amounts_pct: Optional[List[Decimal]] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter comma-separated sell level weights (blank = equal sizing): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    executor_refresh_time: int = Field(
        default=60 * 10,
        json_schema_extra={
            # Per-side-refresh model (event_refresh_enabled=True): this is now a GLOBAL periodic
            # timer. It fires every executor_refresh_time seconds from session start / last global
            # fire -- independent of fills and the per-side cooldowns -- and refreshes BOTH ladders
            # (re-pricing to current config and redeploying idle budget). Legacy mode
            # (event_refresh_enabled=False) keeps the original per-executor-age refresh.
            "prompt": "Global ladder refresh interval in seconds (refreshes both sides)? ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    event_refresh_enabled: bool = Field(
        default=True,
        description=(
            "Master toggle for the per-side cooldown + immediate cross-side refresh model. "
            "True (default): a fill on one side immediately refreshes the OPPOSITE ladder "
            "(deploying the new proceeds) and resets that side's cooldown; each side re-centers "
            "only after its own cooldown lapses; executor_refresh_time is a global periodic "
            "refresh of both sides. False: revert to the legacy per-executor-age refresh + "
            "per-level cooldown + directional recycle window."
        ),
        json_schema_extra={
            "prompt": "Enable per-side cooldown + immediate cross-side refresh? (True/False): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    post_refresh_settle_seconds: int = Field(
        default=0,
        json_schema_extra={
            "prompt": (
                "After a refresh cancel wave, pause both cancels and creates for "
                "N seconds to let exchange balances settle. 0 = disabled (default): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # Zero-level deadlock watchdog (event_refresh_enabled=True): if a side has NO live order
    # executors while its effective budget could fund a rebuild (the plan is non-empty), force
    # that side dirty after this many seconds so an empty ladder can never sit idle until the
    # global timer. An armed per-side cooldown is respected (its lapse re-centers the side
    # anyway); cooldowns only arm on fills, so they can never hold an empty side indefinitely.
    empty_side_watchdog_seconds: int = Field(
        default=60,
        json_schema_extra={
            "prompt": (
                "Seconds an empty-but-fundable ladder side may sit idle before the watchdog "
                "forces a re-place (default 60): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # Preflight-retry fix: if the orchestrator's budget preflight would RESIZE an order below
    # this fraction of its intended amount, the order is DROPPED instead and retried at full
    # size once balances settle (a dust-sized order burns its level and defeats the
    # under-placement detection). 0 disables the rule (legacy resize behavior).
    preflight_min_fill_ratio: Decimal = Field(
        default=Decimal("0.25"),
        json_schema_extra={
            "prompt": (
                "Minimum fraction of the intended amount a preflight resize may keep before "
                "the order is dropped for full-size retry instead (default 0.25, 0=disabled): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # Preflight-retry fix: pacing for the intended-vs-live reconciliation. The first heal
    # attempt runs on the next tick; subsequent attempts every reconcile_retry_seconds, up to
    # reconcile_max_attempts per refresh generation, then a WARNING (manual attention). A
    # newer refresh supersedes and resets the retry state.
    reconcile_retry_seconds: int = Field(
        default=15,
        json_schema_extra={
            "prompt": "Seconds between intended-vs-live ladder heal retries (default 15): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    reconcile_max_attempts: int = Field(
        default=10,
        json_schema_extra={
            "prompt": "Maximum heal retries per refresh generation before warning (default 10): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # Preflight-retry fix: after a refresh wave's cancels finish closing, the controller
    # forces one connector balance refresh and holds that side's creates until the cached
    # available balance shows the freed collateral, or this timeout elapses (then placement
    # is attempted anyway and the reconciliation retries any residual preflight drops).
    # Makes post_refresh_settle_seconds usually unnecessary. 0 disables the gate (the forced
    # refresh still fires).
    post_cancel_balance_timeout_seconds: int = Field(
        default=5,
        json_schema_extra={
            "prompt": (
                "Seconds to hold post-cancel placement waiting for the freed balance to appear "
                "in the connector cache (default 5, 0=no gate): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    cooldown_time: int = Field(
        default=30,
        json_schema_extra={
            # Legacy per-level cooldown (event_refresh_enabled=False only). Also the BACKWARD-COMPAT
            # default for buy_cooldown_time / sell_cooldown_time when those are left blank, so an
            # existing config that only sets cooldown_time keeps working under the per-side model.
            "prompt": "Per-level cooldown / default per-side cooldown after a level closes (seconds): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    # Per-side cooldown timers (event_refresh_enabled=True). After a fill on a side, that side
    # waits its own cooldown with no further fill before re-centering ONCE; a cross-side refresh
    # from the opposite side's fill is immediate and never waits on these. Left blank (None) they
    # default to cooldown_time (see effective_buy_cooldown_time / effective_sell_cooldown_time).
    buy_cooldown_time: Optional[int] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Buy-side cooldown before a quiet buy ladder re-centers (blank = cooldown_time): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    sell_cooldown_time: Optional[int] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Sell-side cooldown before a quiet sell ladder re-centers (blank = cooldown_time): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    min_order_quote: Decimal = Field(
        default=Decimal("5"),
        json_schema_extra={
            "prompt": "Minimum order notional in quote to place any level: ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    allow_partial_levels: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Allow the last remaining budget/inventory to partially fill a level? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    passive_order_placement: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Only place passive maker orders already outside the spread? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    state_file_name: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Optional state file name (blank = auto-generated): ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )

    allow_initialize_with_unavailable_wallet_funds: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": "Allow first-time initialization while some wallet funds are already unavailable/reserved? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )

    max_session_duration_hours: Decimal = Field(
        default=Decimal("192"),
        json_schema_extra={
            "prompt": "Maximum unattended session duration in hours before the controller winds down (default 192 = 8 days): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    cancel_orders_on_session_end: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Cancel active ladder orders when the maximum session duration is reached? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    max_market_data_unavailable_seconds: int = Field(
        default=900,
        json_schema_extra={
            "prompt": "Maximum seconds of unavailable market data before the controller hard-pauses and cancels active ladder orders (default 900): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    cancel_orders_on_market_data_hard_pause: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Cancel active ladder orders when the controller hard-pauses due to prolonged market-data unavailability? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    diagnostic_log_enabled: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Write a dedicated JSONL diagnostic log for later review? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )
    diagnostic_log_file_name: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Optional diagnostic JSONL log file name (blank = auto-generated): ",
            "prompt_on_new": True,
            "is_updatable": False,
        },
    )
    diagnostic_heartbeat_interval_seconds: int = Field(
        default=300,
        json_schema_extra={
            "prompt": "How often to emit a detailed diagnostic heartbeat event in seconds (default 300): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    # Rotation for the diagnostic JSONL: without a cap, multi-week sessions on the same
    # file grow unbounded (synchronous appends in the async loop get slower with size).
    diagnostic_log_max_bytes: int = Field(
        default=52_428_800,  # 50 MB
        description=(
            "Rotate the diagnostic JSONL when it exceeds this many bytes "
            "(name.jsonl -> name.jsonl.1, shifting older backups up)."
        ),
        json_schema_extra={
            "prompt": "Maximum diagnostic JSONL size in bytes before rotation (default 52428800 = 50 MB): ",
            "prompt_on_new": False,
            "is_updatable": False,
        },
    )
    diagnostic_log_backup_count: int = Field(
        default=3,
        description="How many rotated diagnostic JSONL backups (.1, .2, ...) to keep.",
        json_schema_extra={
            "prompt": "How many rotated diagnostic JSONL backups to keep (default 3): ",
            "prompt_on_new": False,
            "is_updatable": False,
        },
    )

    # v12 Part B: fast directional recycle window. While open, the targeted side's
    # per-level cooldowns are bypassed so the offsetting order from the proceeds of a
    # fill on the OTHER side is created within this many seconds of that fill.
    recycle_max_latency_seconds: int = Field(
        default=60,
        json_schema_extra={
            "prompt": (
                "Maximum seconds to wait before recycling fill proceeds into the opposite side "
                "(directional cooldown bypass window, default 60): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # v12 Issue 1: grace period an over-claim must persist before the fills-only ledger
    # is self-healed (re-anchored DOWN) to wallet truth.
    ledger_overclaim_reanchor_seconds: int = Field(
        default=120,
        json_schema_extra={
            "prompt": (
                "Grace period (seconds) an over-claim must persist before the ledger is "
                "re-anchored down to the wallet (default 120): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # v15: settle grace after a booked fill. A freshly booked fill credits the ledger
    # instantly while the wallet snapshot lags one balance poll (up to ~120s on connectors
    # without push balances, e.g. Kraken). Treat that window like balance settling so the
    # over-claim re-anchor/warnings and the wallet-floor warning do not false-positive.
    fill_settle_grace_seconds: int = Field(
        default=90,
        json_schema_extra={
            "prompt": (
                "Grace period (seconds) after a booked fill during which ledger/wallet "
                "over-claim checks are deferred (default 90): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    # v12 Issue 1: over-claim magnitude (in quote) above which re-anchoring/warning applies.
    ledger_reconcile_threshold_quote: Decimal = Field(
        default=Decimal("0.5"),
        json_schema_extra={
            "prompt": (
                "Over-claim magnitude in quote above which ledger reconciliation/re-anchoring "
                "applies (default 0.5): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )

    # v13 Part A: fallback per-fill fee rate (fraction of filled quote) used ONLY when the
    # connector does not report an actual fee for an order. NonKYC's order object carries no
    # fee field; a silent 0 would slowly overstate the fund. Ledger-accuracy only -- this
    # NEVER affects order sizing or prices.
    fee_rate: Decimal = Field(
        default=Decimal("0.002"),
        json_schema_extra={
            "prompt": (
                "Fallback per-fill fee rate (fraction of filled quote, e.g. 0.002 = 0.2%) used "
                "only when the connector does not report an actual fee: "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )

    # v13 Part C: guarded one-shot managed-fund re-seed. When True AND this exact reseed token
    # (reseed_generation + reseed_fund_target_quote) has not already been applied, the fund is
    # re-seeded from the CURRENT wallet and the booking progress is cleared. Idempotent: it
    # never repeats for the same token even if left True. Bump reseed_generation to re-arm.
    reseed_fund_from_wallet_once: bool = Field(
        default=False,
        description=(
            "One-shot managed-fund re-seed from the current wallet, idempotent per "
            "(reseed_generation, reseed_fund_target_quote) token. The re-seed WAITS FOR A "
            "FLAT BOOK: while any of this controller's order executors are active or "
            "shutting down it is deferred (funds held in resting orders would be excluded "
            "from the wallet-based claim and stranded), and it applies automatically on the "
            "first cycle with no live order executors."
        ),
        json_schema_extra={
            "prompt": (
                "Re-seed the managed fund from the current wallet once on next start? "
                "(True/False, idempotent per reseed_generation): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    reseed_fund_target_quote: Optional[Decimal] = Field(
        default=None,
        description=(
            "Optional quote value to target when reseed_fund_from_wallet_once runs. If set, the "
            "re-seed claims up to this quote value from the wallet (mirroring total_amount_quote "
            "seed logic). If unset, it re-runs the normal seed claim against total_amount_quote."
        ),
        json_schema_extra={
            "prompt": (
                "Optional quote value to target on re-seed (blank = use total_amount_quote): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    reseed_generation: int = Field(
        default=0,
        json_schema_extra={
            "prompt": (
                "Re-seed generation counter -- bump this integer to re-arm a one-shot re-seed "
                "with reseed_fund_from_wallet_once left True (default 0): "
            ),
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )

    @field_validator("buy_prices", "sell_prices", mode="before")
    @classmethod
    def parse_price_lists(cls, value, validation_info: ValidationInfo):
        parsed = _parse_comma_list(value)
        field_name = validation_info.field_name
        return [_safe_decimal(item, f"{field_name} item") for item in parsed]

    @field_validator("buy_amounts_pct", "sell_amounts_pct", mode="before")
    @classmethod
    def parse_weight_lists(cls, value, validation_info: ValidationInfo):
        related_prices_field = validation_info.field_name.replace("amounts_pct", "prices")
        prices = validation_info.data.get(related_prices_field)

        if prices is None:
            raise ValueError(
                f"Cannot resolve {related_prices_field} while validating {validation_info.field_name}. "
                f"Ensure {related_prices_field} is defined before {validation_info.field_name} in the config class."
            )

        if value is None or value == "":
            return [Decimal("1") for _ in prices]

        parsed = [_safe_decimal(item, f"{validation_info.field_name} item") for item in _parse_comma_list(value)]
        if len(parsed) != len(prices):
            raise ValueError(
                f"{validation_info.field_name} must have the same length as {related_prices_field}."
            )
        return parsed

    @field_validator(
        "claimed_base_value_quote",
        "total_amount_quote",
        "max_fund_value_quote",
        "min_order_quote",
        "max_session_duration_hours",
        "ledger_reconcile_threshold_quote",
        "fee_rate",
        mode="before",
    )
    @classmethod
    def parse_decimals(cls, value, validation_info: ValidationInfo):
        if isinstance(value, str):
            value = value.strip()
        return _safe_decimal(value, validation_info.field_name, default="0")

    @field_validator("reseed_fund_target_quote", mode="before")
    @classmethod
    def parse_optional_reseed_target(cls, value):
        # Blank / unset -> None (re-seed falls back to total_amount_quote).
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
        return _safe_decimal(value, "reseed_fund_target_quote")

    @field_validator("buy_cooldown_time", "sell_cooldown_time", mode="before")
    @classmethod
    def parse_optional_cooldown(cls, value):
        # Blank / unset -> None (falls back to cooldown_time via the effective_* properties).
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
            return int(value)
        return value

    @field_validator(
        "executor_refresh_time",
        "cooldown_time",
        "max_market_data_unavailable_seconds",
        "diagnostic_heartbeat_interval_seconds",
        "recycle_max_latency_seconds",
        "ledger_overclaim_reanchor_seconds",
        "fill_settle_grace_seconds",
        "reseed_generation",
        mode="before",
    )
    @classmethod
    def parse_int_fields(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                raise ValueError("Integer field cannot be blank")
            return int(value)
        return value

    @field_validator("state_file_name", "diagnostic_log_file_name", mode="before")
    @classmethod
    def normalize_state_file_name(cls, value):
        if value == "":
            return None
        return value

    @field_validator("total_amount_quote")
    @classmethod
    def validate_total_amount_quote(cls, value: Decimal):
        if not value.is_finite() or value < Decimal("0"):
            raise ValueError("total_amount_quote must be a finite non-negative value")
        return value

    @field_validator("max_fund_value_quote")
    @classmethod
    def validate_max_fund_value_quote(cls, value: Decimal, validation_info: ValidationInfo):
        if not value.is_finite() or value <= Decimal("0"):
            raise ValueError("max_fund_value_quote must be a finite value greater than zero")
        total_amount_quote = validation_info.data.get("total_amount_quote")
        if total_amount_quote is not None and value < total_amount_quote:
            raise ValueError("max_fund_value_quote must be greater than or equal to total_amount_quote")
        return value

    @field_validator("claimed_base_value_quote")
    @classmethod
    def validate_claimed_base_value_quote(cls, value: Decimal):
        if not value.is_finite() or value < Decimal("0"):
            raise ValueError("claimed_base_value_quote must be a finite non-negative value")
        return value

    @field_validator("claimed_base_value_quote")
    @classmethod
    def validate_claimed_base_vs_total_amount_quote(cls, value: Decimal, validation_info: ValidationInfo):
        total_amount_quote = validation_info.data.get("total_amount_quote")
        if total_amount_quote is not None and value > total_amount_quote:
            raise ValueError("claimed_base_value_quote cannot be greater than total_amount_quote")
        return value

    @field_validator("claimed_base_amount")
    @classmethod
    def validate_claimed_base_amount(cls, value):
        if value is not None:
            if not value.is_finite() or value < Decimal("0"):
                raise ValueError("claimed_base_amount must be a finite non-negative value")
        return value

    @field_validator("shared_account_quote_quota")
    @classmethod
    def _validate_shared_account_quote_quota(cls, value):
        if value is None:
            return value
        if not value.is_finite() or value < Decimal("0"):
            raise ValueError("shared_account_quote_quota must be a non-negative finite decimal or null")
        return value

    @field_validator("min_order_quote")
    @classmethod
    def validate_min_order_quote(cls, value: Decimal):
        if not value.is_finite() or value <= Decimal("0"):
            raise ValueError("min_order_quote must be a finite value greater than zero")
        return value

    @field_validator("executor_refresh_time")
    @classmethod
    def validate_executor_refresh_time(cls, value: int):
        if value <= 0:
            raise ValueError("executor_refresh_time must be greater than zero")
        return value

    @field_validator("post_refresh_settle_seconds")
    @classmethod
    def validate_post_refresh_settle_seconds(cls, value: int):
        if value is None or value < 0:
            raise ValueError("post_refresh_settle_seconds must be >= 0")
        return value

    @field_validator("cooldown_time")
    @classmethod
    def validate_cooldown_time(cls, value: int):
        if value < 0:
            raise ValueError("cooldown_time cannot be negative")
        return value

    @field_validator("buy_cooldown_time", "sell_cooldown_time")
    @classmethod
    def validate_optional_cooldown(cls, value, validation_info: ValidationInfo):
        if value is not None and value < 0:
            raise ValueError(f"{validation_info.field_name} cannot be negative")
        return value

    @field_validator("empty_side_watchdog_seconds")
    @classmethod
    def validate_empty_side_watchdog_seconds(cls, value: int):
        if value <= 0:
            raise ValueError("empty_side_watchdog_seconds must be greater than zero")
        return value

    @field_validator("preflight_min_fill_ratio")
    @classmethod
    def validate_preflight_min_fill_ratio(cls, value: Decimal):
        if not value.is_finite() or value < Decimal("0") or value >= Decimal("1"):
            raise ValueError("preflight_min_fill_ratio must be a finite fraction in [0, 1)")
        return value

    @field_validator("reconcile_retry_seconds", "reconcile_max_attempts")
    @classmethod
    def validate_reconcile_settings(cls, value: int, info: ValidationInfo):
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return value

    @field_validator("post_cancel_balance_timeout_seconds")
    @classmethod
    def validate_post_cancel_balance_timeout_seconds(cls, value: int):
        if value < 0:
            raise ValueError("post_cancel_balance_timeout_seconds cannot be negative")
        return value

    @field_validator("max_session_duration_hours")
    @classmethod
    def validate_max_session_duration_hours(cls, value: Decimal):
        if not value.is_finite() or value <= Decimal("0"):
            raise ValueError("max_session_duration_hours must be a finite value greater than zero")
        return value

    @field_validator("max_market_data_unavailable_seconds")
    @classmethod
    def validate_max_market_data_unavailable_seconds(cls, value: int):
        if value <= 0:
            raise ValueError("max_market_data_unavailable_seconds must be greater than zero")
        return value

    @field_validator("diagnostic_heartbeat_interval_seconds")
    @classmethod
    def validate_diagnostic_heartbeat_interval_seconds(cls, value: int):
        if value <= 0:
            raise ValueError("diagnostic_heartbeat_interval_seconds must be greater than zero")
        return value

    @field_validator("recycle_max_latency_seconds")
    @classmethod
    def validate_recycle_max_latency_seconds(cls, value: int):
        if value <= 0:
            raise ValueError("recycle_max_latency_seconds must be greater than zero")
        return value

    @field_validator("diagnostic_log_max_bytes", "diagnostic_log_backup_count")
    @classmethod
    def validate_diagnostic_log_rotation(cls, value: int, info: ValidationInfo):
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return value

    @field_validator("ledger_overclaim_reanchor_seconds")
    @classmethod
    def validate_ledger_overclaim_reanchor_seconds(cls, value: int):
        if value < 0:
            raise ValueError("ledger_overclaim_reanchor_seconds cannot be negative")
        return value

    @field_validator("fill_settle_grace_seconds")
    @classmethod
    def validate_fill_settle_grace_seconds(cls, value: int):
        if value < 0:
            raise ValueError("fill_settle_grace_seconds cannot be negative")
        return value

    @field_validator("ledger_reconcile_threshold_quote")
    @classmethod
    def validate_ledger_reconcile_threshold_quote(cls, value: Decimal):
        if not value.is_finite() or value <= Decimal("0"):
            raise ValueError("ledger_reconcile_threshold_quote must be a finite value greater than zero")
        return value

    @field_validator("fee_rate")
    @classmethod
    def validate_fee_rate(cls, value: Decimal):
        if not value.is_finite() or value < Decimal("0") or value >= Decimal("1"):
            raise ValueError("fee_rate must be a finite fraction in [0, 1)")
        return value

    @field_validator("reseed_fund_target_quote")
    @classmethod
    def validate_reseed_fund_target_quote(cls, value):
        if value is None:
            return value
        if not value.is_finite() or value < Decimal("0"):
            raise ValueError("reseed_fund_target_quote must be a non-negative finite decimal or null")
        return value

    @field_validator("reseed_generation")
    @classmethod
    def validate_reseed_generation(cls, value: int):
        if value < 0:
            raise ValueError("reseed_generation cannot be negative")
        return value

    @field_validator("buy_prices")
    @classmethod
    def validate_buy_prices(cls, value: List[Decimal]):
        if len(value) == 0:
            raise ValueError("buy_prices cannot be empty")
        if any((not price.is_finite()) or price <= Decimal("0") for price in value):
            raise ValueError("buy_prices must contain only finite positive prices")
        if any(value[i] <= value[i + 1] for i in range(len(value) - 1)):
            raise ValueError("buy_prices must be strictly ordered highest to lowest (no duplicates)")
        return value

    @field_validator("sell_prices")
    @classmethod
    def validate_sell_prices(cls, value: List[Decimal]):
        if len(value) == 0:
            raise ValueError("sell_prices cannot be empty")
        if any((not price.is_finite()) or price <= Decimal("0") for price in value):
            raise ValueError("sell_prices must contain only finite positive prices")
        if any(value[i] >= value[i + 1] for i in range(len(value) - 1)):
            raise ValueError("sell_prices must be strictly ordered lowest to highest (no duplicates)")
        return value

    @field_validator("buy_amounts_pct", "sell_amounts_pct")
    @classmethod
    def validate_weight_lists(cls, value: List[Decimal], validation_info: ValidationInfo):
        field_name = validation_info.field_name
        if value is None or len(value) == 0:
            raise ValueError(f"{field_name} cannot be empty")
        if any((not weight.is_finite()) or weight <= Decimal("0") for weight in value):
            raise ValueError(f"{field_name} must contain only finite positive weights")
        if sum(value) <= Decimal("0"):
            raise ValueError(f"{field_name} must have a positive sum")
        return value

    @model_validator(mode="after")
    def validate_no_cross_side_overlap(self):
        if self.buy_prices and self.sell_prices:
            highest_buy = self.buy_prices[0]
            lowest_sell = self.sell_prices[0]
            if highest_buy >= lowest_sell:
                raise ValueError(
                    f"Highest buy price ({highest_buy}) must be below lowest sell price ({lowest_sell}) "
                    "to avoid overlap and potential self-trading."
                )
        return self

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)

    @property
    def effective_buy_cooldown_time(self) -> int:
        """Buy-side cooldown actually in force: the explicit buy_cooldown_time when set,
        else the legacy cooldown_time default (backward compatible)."""
        return int(self.buy_cooldown_time if self.buy_cooldown_time is not None else self.cooldown_time)

    @property
    def effective_sell_cooldown_time(self) -> int:
        """Sell-side cooldown actually in force: the explicit sell_cooldown_time when set,
        else the legacy cooldown_time default (backward compatible)."""
        return int(self.sell_cooldown_time if self.sell_cooldown_time is not None else self.cooldown_time)

    @property
    def normalized_buy_weights(self) -> List[Decimal]:
        total = sum(self.buy_amounts_pct)
        if total <= Decimal("0"):
            return [Decimal("0") for _ in self.buy_amounts_pct]
        return [Decimal(x) / Decimal(total) for x in self.buy_amounts_pct]

    @property
    def normalized_sell_weights(self) -> List[Decimal]:
        total = sum(self.sell_amounts_pct)
        if total <= Decimal("0"):
            return [Decimal("0") for _ in self.sell_amounts_pct]
        return [Decimal(x) / Decimal(total) for x in self.sell_amounts_pct]


class RangeInventoryLadderController(ControllerBase):
    """
    Fixed-price accumulation/distribution ladder for spot inventory management.

    Repo-specific compatibility notes:
    - Uses OrderExecutor so your fork's executor/orchestrator structured logging stays active.
    - Emits additional structured events from the controller itself for easier reconciliation in your custom logs.
    - Surfaces both managed-fund equity metrics and native inventory PnL metrics in status/custom_info.

    V9 hardening:
    - Fixes LIMIT_MAKER capability detection for controller mode and falls back safely on connectors like NonKYC.
    - Sizes new orders from free remaining budgets instead of total deployable balances.
    - Adds sequential redistribution so quantization/min-notional skips do not strand capital for a full cycle.
    - Adds session-duration and prolonged-market-data-outage safety pauses for short unattended runs.
    - Writes a dedicated JSONL diagnostic log with heartbeat snapshots for post-run review.

    V12 changes (ledger reconciliation + fast directional recycle):
    - Issue 1: self-healing ledger re-anchor. A persistent over-claim (the fills-only
      ledger believing it holds more than the wallet physically does) is corrected DOWN to
      wallet truth after `ledger_overclaim_reanchor_seconds`. Only ever corrects downward;
      a wallet that exceeds the ledger stays a no-op (legitimate under the self-balance model).
    - Issue 2: a refresh cancel now bypass-marks its level, so re-pricing an order never
      parks the level on cooldown (only genuine fills/closes do).
    - Issue 3: create deferral after a stop is now side-specific -- a stop on one side no
      longer blocks creates on the other.
    - Issue 4: status eligible-price lists index levels via enumerate (no O(n^2) value lookup).
    - Issue 5: `owned_quote`/`owned_base`/`seed_value_quote` and `tracked_fill_executor_ids`
      are validated/coerced on state load (corrupt values quarantine instead of loading).
    - Part B: fast directional recycle. A fill on one side raises the TOTAL balance of the
      asset received (immune to our own refresh cancels, which only move reserved->available);
      that opens a short `recycle_max_latency_seconds` window during which the OPPOSITE side's
      per-level cooldowns are bypassed, so freed funds redeploy within ~1 minute. A one-sided
      wobble with no offsetting fill opens no window, so its cooldown (over-accumulation
      protection) stays fully intact. STATE_SCHEMA_VERSION is intentionally unchanged (10).

    V13 changes (robust fill booking + self-growing managed fund):
    - Part A: fills are now booked per-order from each of OUR orders' cumulative executed
      amounts (sampled every cycle via _book_fills_from_orders), not from transient custom_info
      at close. Partial fills accrue; external transfers are ignored by construction (a deposit
      is not an order); per-order progress lives in the new optional state key
      "booked_fill_progress" (no schema bump). Base comes from the in-flight order's
      executed_amount_base; quote prefers executed_amount_quote and else derives
      filled_base*price; fee prefers the connector's actual fee and else falls back to the new
      `fee_rate` (NonKYC orders carry no fee field). Fees in a third asset (alternateFeeAsset)
      are recorded but never subtracted from the two-asset ledger. Monotonic guards prevent a
      momentary 0 read from un-booking. Booking now runs AFTER the balance reads and BEFORE
      managed_* so the deploy ceiling reflects fills the same cycle.
    - Part B: the fund grows with realized success off the now-accurate ledger
      (managed_fund_value_quote -> deploy_ceiling, capped at max_fund_value_quote). Deposits do
      NOT inflate it; a withdrawal of fund money shrinks it via the v12 re-anchor. The recycle
      windows are now driven by BOOKED fills (sell fill -> buy window, buy fill -> sell window),
      with only a two-sided wallet backstop -- a pure one-sided deposit/withdrawal opens NO
      window (replacing the v12 one-sided-total heuristic).
    - Part C: a guarded, one-shot `reseed_fund_from_wallet_once` (+ optional
      `reseed_fund_target_quote`, re-armable via `reseed_generation`) re-seeds the managed
      baseline from the CURRENT wallet and clears the booking progress. Idempotent per token.

    Per-side cooldown + immediate cross-side refresh (event_refresh_enabled=True, the default):
    - A "refresh" is a per-side operation: cancel the side's resting orders (bypass-marked so no
      residual cooldown) and recreate that side from its CURRENT managed budget (free + the budget
      its own cancelled orders return), distributed by the YAML weights welded to their configured
      price rungs, with min-notional compression dropping the FARTHEST rung from price first (so a
      small budget lands nearest the market -- the 305->312 re-center).
    - Five triggers: (1) a booked BUY fill immediately refreshes the SELL ladder (deploy the new
      base) and arms the buy cooldown; (2) a booked SELL fill immediately refreshes the BUY ladder
      (deploy the new quote) and arms the sell cooldown; (3) buy_cooldown_time lapsing with no buy
      fill re-centers the BUY ladder once; (4) sell_cooldown_time lapsing re-centers the SELL
      ladder once; (5) executor_refresh_time is a GLOBAL periodic timer that refreshes BOTH sides
      independent of fills/cooldowns. A fill NEVER refreshes its own side, and a placement is not a
      fill, so the loop cannot self-trigger. Two guards apply to every refresh: a no-op guard (skip
      the cancel/recreate when the rebuild would reproduce the resting book within tolerance) and a
      dust guard (a sub-min_order_quote freed amount changes nothing -> skip), preventing churn.
    - Setting event_refresh_enabled=False reverts cleanly to the legacy per-executor-age refresh +
      per-level cooldown + directional recycle window.

    Ledger-funded budgets (ledger_funded_budgets=True, the default): new buy/sell orders are sized
    from THIS controller's own managed-fund ledger -- its un-reserved owned quote/base (owned minus
    its own resting reservations) -- bounded by the live wallet as a safety floor, instead of from
    the raw shared wallet balance. This lets MULTIPLE CONTROLLERS SHARE ONE EXCHANGE ACCOUNT WITHOUT
    `shared_account_quote_quota` (each spends only what it owns), and lets a controller redeploy its
    own proceeds (e.g. re-buying with the USDT it just sold into) with no quota change because its
    own ledger grew. `shared_account_quote_quota` remains available as an optional additional hard
    cap. Deposits still raise only the wallet floor, never owned, so deposit-exclusion is preserved.
    Set ledger_funded_budgets=False for the legacy raw-wallet-funded behavior (byte-for-byte).

    OPERATOR NOTE: the fund deliberately ignores deposits, so simply transferring money in will
    NOT raise the managed baseline. To change the baseline: deposit to the intended amount, then
    either set reseed_fund_from_wallet_once=True (bump reseed_generation to re-arm), or stop the
    bot, fund the wallet, quarantine/remove the state file, and restart (re-init from wallet).

    REFRESH BUDGET FIX (2026-07, after the 2026-07-07 live Kraken XMR-USD deadlock):
    - Fix 1 (deterministic refresh budget): every refresh cancel wave records the cancelled
      executors' reservations in a per-side "refresh wave" record. While the wave is
      unresolved, the deploy budgets credit back (a) the reservations of the cancelled
      executors still live/shutting down (exact, from the controller's own reservation
      ledger) and (b) up to the recorded release against the CACHED wallet hold
      (min(released, held)) -- so the rebuild is sized from the funds the cancels return,
      never waiting on the exchange balance poll. Both credits shrink automatically as the
      cancels complete and the wallet cache refreshes, so nothing is ever double-counted.
      Logged at INFO as `refresh budget: free=X + released_reservations=Y = effective=Z`.
    - Fix 2 (`post_refresh_settle_seconds`, default 0): the already-wired settle window
      (cancel wave first, quiet period, then re-place) is verified and covered by tests;
      the primary fix works with this at 0.
    - Fix 3 (zero-level watchdog, `empty_side_watchdog_seconds`, default 60): a side with
      zero live order executors whose effective budget could fund a rebuild is forced
      dirty after the watchdog interval (WARNING + structured event). An armed per-side
      cooldown is respected at most once -- cooldowns only arm on fills, so they can never
      hold an empty ladder empty indefinitely.
    - Fix 4 (deferred creates re-proposed): issued rebuilds are tracked on the wave record;
      if the orchestrator drops the creates (stop/create conflict deferral), the side is
      re-marked dirty and the ladder re-proposed on the next cycle once the conflicting
      stops have cleared -- newest refresh always supersedes older pending proposals.
    - Fix 5 (compression guard): a refresh whose candidate set is EMPTY while live orders
      exist and the effective budget could fund at least one level ABORTS (keeps the
      resting orders, WARNING) instead of cancelling into nothing.

    PREFLIGHT RETRY FIX (2026-07, after the 2026-07-07 19:18 Kraken dust-sell failure --
    the orchestrator's budget preflight consulted the connector's stale cached balance
    right after a confirmed cancel wave, resized the first sell to dust and dropped the
    other 8 terminally; the dust order defeated the zero-level watchdog):
    - Intended-vs-live reconciliation (primary): each issued rebuild records its intended
      ladder (level -> amount) on the wave record. A level is satisfied once any executor
      materialized for it (a fill is success, never re-placed); levels never observed live
      are re-proposed with backoff (`reconcile_retry_seconds`, default 15) up to
      `reconcile_max_attempts` (default 10) per refresh generation, then a WARNING (manual
      attention). Healing marks the side dirty with reason "ladder_reconcile": survivors are
      NEVER cancelled -- the create path fills only the missing rungs, sized from the
      CURRENT effective budget. A newer refresh supersedes and resets the retry state. This
      subsumes the deferred-create re-propose and generalizes the zero-level watchdog: a
      "1 dust order out of 9" state heals exactly like "0 orders".
    - Preflight feedback loop: create actions carry `min_fill_ratio`
      (`preflight_min_fill_ratio`, default 0.25); the orchestrator's budget preflight DROPS
      a resize below that fraction instead of placing dust, and notifies the originating
      controller via on_budget_preflight_result() (best-effort) so the reconciliation
      retries immediately. A live dust order that slips through is stopped and its level
      retried at full size (controller-side backstop).
    - Post-cancel balance refresh: once a wave's cancels finish closing, the controller
      fires one connector._update_balances() (connector-agnostic, best-effort) and holds
      that side's creates until the cached available balance shows the freed collateral or
      `post_cancel_balance_timeout_seconds` (default 5) elapses -- then places anyway and
      the reconciliation heals any residual preflight drops. Makes
      `post_refresh_settle_seconds` usually unnecessary (both remain available).
    - Plan/budget invariant (2026-07-08 addendum): every refresh plan and every issued
      create batch is checked against sum(amounts) <= effective side budget + epsilon
      (quote notional for buys, base for sells). Expected to always hold -- the planner
      sizes FROM the budget -- so a violation is a WARNING + structured event only
      (rate-limited, never blocks placement): a tripwire for future sizing bugs, added
      after the retracted "plans one rung more than owned" analysis of the 2026-07-08
      session logs.
    """

    STATE_SCHEMA_VERSION = 10
    SUPPORTED_STATE_SCHEMA_VERSIONS = {6, 7, 8, 9, 10}
    STATE_MAX_FUTURE_SKEW_SECONDS = Decimal("86400")  # 1 day
    INITIALIZATION_UNAVAILABLE_BALANCE_TOLERANCE = Decimal("0.00000001")
    # A refresh-wave record older than this is dropped regardless of state -- a backstop so
    # a wave that never resolves (e.g. plan permanently empty) cannot credit budgets forever.
    REFRESH_WAVE_TTL_SECONDS = 900.0
    # How long a wallet-over-ledger surplus must persist (not settling) before the
    # understatement diagnostic warns. Diagnostic only -- the ledger is never raised.
    LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS = 1800.0


    def __init__(self, config: RangeInventoryLadderConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._state: Dict = {}
        self._state_loaded = False
        self._startup_logged = False
        self._cycles_seen = 0
        self._last_drift_warning_time: float = 0.0
        self._drift_warning_interval: float = 300.0  # warn at most every 5 minutes
        self._last_drift_above_threshold: bool = False
        self._positions_empty_warning_emitted = False
        self._state_recovery_reason: Optional[str] = None
        self._state_recovery_backup_path: Optional[Path] = None
        self._state_migrated_from_version: Optional[int] = None

        self._config_rebuild_pending: bool = False
        self._config_rebuild_reason: str = ""
        self._pending_runtime_config_signature: Optional[tuple] = None
        self._applied_runtime_config_signature: Optional[tuple] = None

        self._last_buy_compression_signature: Optional[tuple] = None
        self._last_sell_compression_signature: Optional[tuple] = None
        self._last_price_regime: Optional[str] = None
        self._cooldown_bypass_until_by_level: Dict[str, float] = {}

        # Per-level last filter reason (None = eligible, "blocked", "not_passive").
        # Filter events are emitted only on reason TRANSITIONS, not every cycle --
        # per-cycle emission produced ~271 MB/day of diagnostic JSONL.
        self._buy_level_filter_reasons: Dict[str, Optional[str]] = {}
        self._sell_level_filter_reasons: Dict[str, Optional[str]] = {}

        self._initialization_blocked_reason: Optional[str] = None
        self._initialization_blocked_logged: bool = False
        self._last_market_data_error: Optional[str] = None
        self._limit_maker_fallback_warned: bool = False
        self._supports_limit_maker_cache: Optional[bool] = None

        self._buy_reservation_sources: Dict[str, int] = {}
        self._sell_reservation_sources: Dict[str, int] = {}

        self._market_data_unavailable_since: Optional[float] = None
        self._market_data_hard_pause: bool = False

        self._session_started_ts: Optional[float] = None
        self._session_expired: bool = False
        self._session_expired_reason: str = ""
        self._session_expired_logged: bool = False

        self._last_diagnostic_heartbeat_ts: float = 0.0
        self._refresh_quiet_until: float = 0.0
        self._last_live_runtime_settings_signature: Optional[tuple] = None

        # v12 Issue 1: timestamp at which a persistent ledger over-claim was first
        # observed above threshold. Reset to None whenever the over-claim clears or is
        # re-anchored; used to enforce the ledger_overclaim_reanchor_seconds grace period.
        self._overclaim_since: Optional[float] = None

        # v12 Issue 3: per-cycle, side-specific create-deferral flags. Set when a stop is
        # proposed on that side this cycle so creates on the SAME side are skipped while
        # the unaffected side is still free to place. Reset every determine_executor_actions.
        self._defer_buy_creates_this_cycle: bool = False
        self._defer_sell_creates_this_cycle: bool = False

        # v12 Part B: previous-cycle TOTAL balances + directional recycle windows.
        # A rise in TOTAL balance (immune to our own refresh cancels, which only move
        # reserved->available with no total change) signals a fill/deposit and opens a
        # short window during which the targeted side's level cooldowns are bypassed.
        self._prev_total_quote_balance: Optional[Decimal] = None
        self._prev_total_base_balance: Optional[Decimal] = None
        self._recycle_bypass_buy_until: float = 0.0
        self._recycle_bypass_sell_until: float = 0.0

        # v13 Part A: which side(s) had a fill BOOKED this cycle (set by _book_fills_from_orders).
        # These drive the recycle windows (a booked sell fill funds buys; a booked buy fill funds
        # sells) -- a deposit is not an order, so it never sets these and opens no window.
        self._booked_buy_fill_this_cycle: bool = False
        self._booked_sell_fill_this_cycle: bool = False
        # v15: time of the most recent BOOKED fill. The wallet snapshot lags a fill by up to one
        # balance poll (~120s on poll-only connectors), so over-claim checks defer within
        # fill_settle_grace_seconds of this timestamp (see _within_fill_settle_grace).
        self._last_fill_booked_ts: Optional[float] = None
        # v13 Part C: set for one cycle right after a guarded re-seed so booking re-baselines
        # open orders to their current cumulative executed amount instead of retroactively
        # re-booking already-realized fills.
        self._reseed_just_applied: bool = False

        # Per-side cooldown + immediate cross-side refresh model (event_refresh_enabled=True).
        # A booked fill on one side immediately marks the OPPOSITE side dirty (deploy the new
        # proceeds) and arms its OWN cooldown; that side re-centers only after its cooldown
        # lapses with no further fill; executor_refresh_time is a global periodic refresh of both
        # sides. A placement is NOT a fill -- only booked fills touch the *_fill_ts / *_armed
        # state, so recreating orders can never self-trigger a loop.
        self._last_buy_fill_ts: float = 0.0
        self._last_sell_fill_ts: float = 0.0
        self._buy_cooldown_armed: bool = False
        self._sell_cooldown_armed: bool = False
        self._last_global_refresh_ts: float = 0.0
        self._buy_side_dirty: bool = False
        self._sell_side_dirty: bool = False
        # Why each side is currently dirty (surfaced on the range_ladder_side_refresh event).
        self._buy_dirty_reason: str = ""
        self._sell_dirty_reason: str = ""
        # First fully-processed cycle seeds the global timer and places both ladders.
        self._refresh_timers_initialized: bool = False

        # Ledger-funded budgets: per-side latch for the wallet-floor-binding diagnostic so it
        # warns once per binding episode (transition-based), not every cycle.
        self._buy_wallet_floor_bound: bool = False
        self._sell_wallet_floor_bound: bool = False

        # Buy-side fee headroom reserved by the most recent _compute_deploy_budgets call
        # (quote withheld so the exchange's notional + fee hold fits the budget).
        self._last_buy_fee_headroom_quote: Decimal = Decimal("0")

        # Re-seed deferral latch: the "deferred while orders rest" warning/event fires once
        # per reseed token, not per cycle.
        self._reseed_deferred_warned_token: Optional[str] = None

        # Ledger-understatement diagnostic (the inverse of the over-claim): timestamp the
        # wallet-over-ledger surplus first exceeded the threshold, plus its own warning
        # rate-limit timestamp. Deliberately NOT shared with the over-claim warning state.
        self._understatement_since: Optional[float] = None
        self._last_understatement_warning_time: float = 0.0

        # Diagnostic JSONL rotation: last time the file size was stat()ed (throttled to
        # once per _DIAG_SIZE_CHECK_INTERVAL_S so the stat doesn't run on every event).
        self._diag_last_size_check_ts: float = 0.0

        # State-path anchoring: the relative Path("data") default resolved to an absolute
        # path once at first use (logged so an unexpected CWD is visible post-mortem), and
        # a once-per-process flag for the <state>.owner contention-marker check.
        self._state_path_abs: Optional[Path] = None
        self._state_owner_checked: bool = False

        # Refresh-wave records (refresh budget fix 1 + deferred-create re-propose fix 4).
        # Per side: None, or a dict with
        #   released       Decimal -- sum of the cancelled executors' reservations at cancel
        #                  time (quote for buys, base for sells); fixed for the wave's life.
        #   cancelled_ids  set     -- executor ids cancelled by this wave.
        #   started_ts     float   -- wave start (cancel emission), for the TTL backstop.
        #   issued_ts      float|None -- when the rebuild's creates were last emitted.
        #   issued_count   int     -- how many creates the last emission carried.
        #   attempts       int     -- re-propose attempts after the orchestrator dropped them.
        #   last_log       tuple|None -- latch for the INFO budget log (log on change only).
        self._refresh_wave: Dict[str, Optional[dict]] = {"buy": None, "sell": None}
        # Wallet/ledger credits applied by the CURRENT cycle's budget computation (read by
        # _side_rebuild_budget_* so the planner never double-counts the wave's reservations).
        self._wave_ledger_credit_quote: Decimal = Decimal("0")
        self._wave_ledger_credit_base: Decimal = Decimal("0")

        # Zero-level deadlock watchdog (fix 3): per side, when the side first became
        # empty-but-fundable (None while healthy).
        self._side_empty_since: Dict[str, Optional[float]] = {"buy": None, "sell": None}

        # Intended-vs-live reconciliation (preflight-retry fix): executor ids of live
        # dust-sized orders (preflight-resized below preflight_min_fill_ratio of their
        # intended amount) queued for a targeted stop so their levels retry at full size.
        self._reconcile_stop_ids: Set[str] = set()

        # Plan/budget invariant (2026-07-08 addendum): per-side rate-limit latch for the
        # sum(planned) <= effective budget safety-net warning.
        self._plan_invariant_last_warn: Dict[str, float] = {"buy": 0.0, "sell": 0.0}


    @property
    def state_path(self) -> Path:
        file_name = self.config.state_file_name or f"range_inventory_ladder_{self.config.id}.json"
        return Path("data") / file_name

    @property
    def state_path_abs(self) -> Path:
        """state_path resolved to an absolute path ONCE at first use, so post-mortems can
        tell which file a controller actually wrote when the CWD was not the expected one."""
        if self._state_path_abs is None:
            try:
                self._state_path_abs = self.state_path.resolve()
            except OSError:
                self._state_path_abs = self.state_path.absolute()
        return self._state_path_abs

    @property
    def diagnostic_log_path(self) -> Path:
        file_name = self.config.diagnostic_log_file_name or f"range_inventory_ladder_{self.config.id}.diagnostic.jsonl"
        return Path("data") / file_name

    @staticmethod
    def _json_safe(value: Any):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(k): RangeInventoryLadderController._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [RangeInventoryLadderController._json_safe(v) for v in value]
        return value

    _DIAG_SIZE_CHECK_INTERVAL_S = 60.0

    def _maybe_rotate_diagnostic_log(self):
        """Rotate the diagnostic JSONL when it exceeds diagnostic_log_max_bytes:
        name.jsonl -> .1, shifting .1 -> .2 etc., deleting beyond
        diagnostic_log_backup_count. The size stat is throttled to once per
        _DIAG_SIZE_CHECK_INTERVAL_S. Exception-safe by contract: any failure is swallowed
        and the append proceeds against the current file."""
        try:
            now = self.market_data_provider.time()
            if (now - self._diag_last_size_check_ts) < self._DIAG_SIZE_CHECK_INTERVAL_S:
                return
            self._diag_last_size_check_ts = now
            path = self.diagnostic_log_path
            if not path.exists() or path.stat().st_size <= int(self.config.diagnostic_log_max_bytes):
                return
            keep = int(self.config.diagnostic_log_backup_count)
            oldest = Path(f"{path}.{keep}")
            if oldest.exists():
                oldest.unlink()
            for n in range(keep - 1, 0, -1):
                rotated = Path(f"{path}.{n}")
                if rotated.exists():
                    os.replace(str(rotated), f"{path}.{n + 1}")
            os.replace(str(path), f"{path}.1")
        except Exception:
            pass

    def _write_diagnostic_event(self, event_type: str, **payload):
        if not self.config.diagnostic_log_enabled:
            return
        try:
            self.diagnostic_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate_diagnostic_log()
            record = {
                "ts_ms": int(self.market_data_provider.time() * 1e3),
                "event_type": event_type,
                "controller_id": self.config.id,
                "controller_name": self.config.controller_name,
                "connector": self.config.connector_name,
                "trading_pair": self.config.trading_pair,
                **self._json_safe(payload),
            }
            with self.diagnostic_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception:
            pass

    _DIAGNOSTIC_SKIP_EVENTS = frozenset({
        "range_ladder_cycle",
        "range_ladder_noop_cycle",
    })

    def _emit_structured(self, event_type: str, **payload):
        safe_payload = self._json_safe(payload)
        try:
            get_structured_logger().emit(event_type, controller_id=self.config.id, **safe_payload)
        except Exception:
            pass
        if event_type not in self._DIAGNOSTIC_SKIP_EVENTS:
            self._write_diagnostic_event(event_type, **safe_payload)

    def _safe_get_balance(self, asset: str) -> Decimal:
        try:
            return _safe_decimal(
                self.market_data_provider.get_balance(self.config.connector_name, asset),
                f"balance_{asset}",
                default="0",
            )
        except Exception:
            return Decimal("0")

    def _safe_get_available_balance(self, asset: str) -> Decimal:
        try:
            return _safe_decimal(
                self.market_data_provider.get_available_balance(self.config.connector_name, asset),
                f"available_balance_{asset}",
                default="0",
            )
        except Exception:
            return Decimal("0")

    def _emit_diagnostic_heartbeat_if_due(self):
        if not self.config.diagnostic_log_enabled or not self.processed_data:
            return
        now = self.market_data_provider.time()
        if now - self._last_diagnostic_heartbeat_ts < self.config.diagnostic_heartbeat_interval_seconds:
            return
        self._last_diagnostic_heartbeat_ts = now
        p = self.processed_data
        session_elapsed_s = 0.0 if self._session_started_ts is None else max(0.0, now - self._session_started_ts)
        self._emit_structured(
            "range_ladder_diagnostic_heartbeat",
            session_elapsed_s=round(session_elapsed_s, 3),
            session_expired=self._session_expired,
            session_expired_reason=self._session_expired_reason,
            market_data_hard_pause=self._market_data_hard_pause,
            market_data_error=p.get("market_data_error", ""),
            market_data_ready=p.get("market_data_ready", True),
            initialization_ready=p.get("initialization_ready", True),
            initialization_blocked_reason=p.get("initialization_blocked_reason", ""),
            reference_price=p.get("reference_price", Decimal("0")),
            best_bid=p.get("best_bid", Decimal("0")),
            best_ask=p.get("best_ask", Decimal("0")),
            price_regime=p.get("price_regime", ""),
            managed_quote_total=p.get("managed_quote_total", Decimal("0")),
            managed_base_total=p.get("managed_base_total", Decimal("0")),
            managed_fund_value_quote=p.get("managed_fund_value_quote", Decimal("0")),
            free_buy_budget_quote=p.get("free_buy_budget_quote", Decimal("0")),
            free_sell_budget_base=p.get("free_sell_budget_base", Decimal("0")),
            buy_fee_headroom_quote=p.get("buy_fee_headroom_quote", Decimal("0")),
            ledger_surplus_quote=p.get("ledger_surplus_quote", Decimal("0")),
            refresh_wave_release_buy_quote=p.get("refresh_wave_release_buy_quote", Decimal("0")),
            refresh_wave_release_sell_base=p.get("refresh_wave_release_sell_base", Decimal("0")),
            ledger_funded_budgets=p.get("ledger_funded_budgets", bool(self.config.ledger_funded_budgets)),
            owned_quote_free=p.get("owned_quote_free", Decimal("0")),
            owned_base_free=p.get("owned_base_free", Decimal("0")),
            available_quote_balance=p.get("available_quote_balance", Decimal("0")),
            available_base_balance=p.get("available_base_balance", Decimal("0")),
            active_buy_reserved_quote=p.get("active_buy_reserved_quote", Decimal("0")),
            active_sell_reserved_base=p.get("active_sell_reserved_base", Decimal("0")),
            active_order_executors=len(self._active_order_executors()),
            tracked_positions=len(self.positions_held or []),
            blocked_level_ids=sorted(list(p.get("blocked_level_ids", set()))),
            not_passive_buy_level_ids=sorted(
                lid for lid, reason in self._buy_level_filter_reasons.items() if reason == "not_passive"
            ),
            not_passive_sell_level_ids=sorted(
                lid for lid, reason in self._sell_level_filter_reasons.items() if reason == "not_passive"
            ),
            reconciliation_gap_quote=p.get("reconciliation_gap_quote", Decimal("0")),
            inventory_global_pnl_quote=p.get("inventory_global_pnl_quote", Decimal("0")),
            reservation_sources_buy=self._buy_reservation_sources,
            reservation_sources_sell=self._sell_reservation_sources,
            diagnostic_log_path=self.diagnostic_log_path,
        )

    def _runtime_config_signature(self) -> tuple:
        return (
            tuple(str(price) for price in self.config.buy_prices),
            tuple(str(weight) for weight in self.config.buy_amounts_pct),
            tuple(str(price) for price in self.config.sell_prices),
            tuple(str(weight) for weight in self.config.sell_amounts_pct),
            str(self.config.max_fund_value_quote),
            str(self.config.min_order_quote),
            bool(self.config.allow_partial_levels),
            bool(self.config.passive_order_placement),
        )

    def _live_runtime_settings_signature(self) -> tuple:
        return (
            int(self.config.executor_refresh_time),
            int(self.config.cooldown_time),
            str(self.config.max_session_duration_hours),
            int(self.config.max_market_data_unavailable_seconds),
            int(self.config.recycle_max_latency_seconds),
        )

    def _handle_live_runtime_settings_update(self):
        current_signature = self._live_runtime_settings_signature()
        if self._last_live_runtime_settings_signature is None:
            self._last_live_runtime_settings_signature = current_signature
            return
        if current_signature == self._last_live_runtime_settings_signature:
            return

        previous_signature = self._last_live_runtime_settings_signature
        self._last_live_runtime_settings_signature = current_signature
        self.logger().warning(
            f"{self.config.id}: detected hot-updated live runtime settings. "
            f"executor_refresh_time={self.config.executor_refresh_time}, "
            f"cooldown_time={self.config.cooldown_time}, "
            f"max_session_duration_hours={self.config.max_session_duration_hours}, "
            f"max_market_data_unavailable_seconds={self.config.max_market_data_unavailable_seconds}, "
            f"recycle_max_latency_seconds={self.config.recycle_max_latency_seconds}"
        )
        self._emit_structured(
            "range_ladder_live_runtime_settings_updated",
            previous_signature=list(previous_signature),
            current_signature=list(current_signature),
            executor_refresh_time=self.config.executor_refresh_time,
            cooldown_time=self.config.cooldown_time,
            max_session_duration_hours=self.config.max_session_duration_hours,
            max_market_data_unavailable_seconds=self.config.max_market_data_unavailable_seconds,
            recycle_max_latency_seconds=self.config.recycle_max_latency_seconds,
        )

    def _cleanup_cooldown_bypass(self):
        now = self.market_data_provider.time()
        self._cooldown_bypass_until_by_level = {
            level_id: expiry_ts
            for level_id, expiry_ts in self._cooldown_bypass_until_by_level.items()
            if expiry_ts > now
        }

    def _mark_bypass_cooldown_for_level(self, level_id: Optional[str]):
        if not level_id:
            return
        # The bypass must outlive the SIDE's real cooldown: with a per-side cooldown
        # configured longer than the legacy cooldown_time, a flat cooldown_time+1 bypass
        # would expire before the side's cooldown lapsed. Derive from the level's side;
        # an unknown prefix conservatively takes the longer of the two.
        if level_id.startswith("buy_"):
            side_cooldown = self.config.effective_buy_cooldown_time
        elif level_id.startswith("sell_"):
            side_cooldown = self.config.effective_sell_cooldown_time
        else:
            side_cooldown = max(
                self.config.effective_buy_cooldown_time,
                self.config.effective_sell_cooldown_time,
            )
        self._cooldown_bypass_until_by_level[level_id] = (
            self.market_data_provider.time() + max(1, side_cooldown + 1)
        )

    def _should_bypass_level_cooldown(self, level_id: Optional[str]) -> bool:
        if not level_id:
            return False
        self._cleanup_cooldown_bypass()
        expiry_ts = self._cooldown_bypass_until_by_level.get(level_id)
        return expiry_ts is not None and expiry_ts > self.market_data_provider.time()

    def _desired_level_ids(self) -> Set[str]:
        return {
            *(self._buy_level_id(idx) for idx in range(len(self.config.buy_prices))),
            *(self._sell_level_id(idx) for idx in range(len(self.config.sell_prices))),
        }

    def _current_price_regime(self, reference_price: Decimal) -> str:
        highest_buy = self.config.buy_prices[0]
        lowest_buy = self.config.buy_prices[-1]
        lowest_sell = self.config.sell_prices[0]
        highest_sell = self.config.sell_prices[-1]

        if reference_price < lowest_buy:
            return "below_buy_range"
        if reference_price > highest_sell:
            return "above_sell_range"
        if reference_price > highest_buy and reference_price < lowest_sell:
            return "between_ladders"
        if lowest_buy <= reference_price <= highest_buy:
            return "inside_buy_band"
        if lowest_sell <= reference_price <= highest_sell:
            return "inside_sell_band"
        return "between_bands"

    def _build_state_backup_path(self, reason: str) -> Path:
        timestamp_ms = int(self.market_data_provider.time() * 1e3)
        return self.state_path.with_name(
            f"{self.state_path.stem}.{reason}.{timestamp_ms}{self.state_path.suffix}"
        )

    def _quarantine_state_file(self, reason: str, error: str):
        backup_path = self._build_state_backup_path(reason)
        self._state_recovery_reason = reason
        self._state_recovery_backup_path = backup_path

        self.logger().warning(
            f"Rejecting state file {self.state_path} ({reason}), moving it to {backup_path}: {error}"
        )
        self._emit_structured(
            "range_ladder_state_rejected",
            state_file=str(self.state_path),
            backup_file=str(backup_path),
            reason=reason,
            error=str(error),
        )
        if reason == "corrupt_state":
            self._emit_structured(
                "range_ladder_corrupt_state_recovered",
                state_file=str(self.state_path),
                backup_file=str(backup_path),
                error=str(error),
            )
        try:
            os.replace(self.state_path, backup_path)
        except OSError:
            pass

    @staticmethod
    def _d(value, default: str = "0") -> Decimal:
        return _safe_decimal(value, "numeric value", default=default)


    def _validate_loaded_state(self, raw: Dict) -> Dict:
        if not isinstance(raw, dict):
            raise ValueError("State payload must be a JSON object")

        base_asset, quote_asset = split_hb_trading_pair(self.config.trading_pair)
        required_keys = {
            "schema_version",
            "controller_name",
            "controller_type",
            "controller_id",
            "connector_name",
            "trading_pair",
            "base_asset",
            "quote_asset",
            "initialized",
            "reserve_quote_balance",
            "reserve_base_balance",
            "initial_managed_quote",
            "initial_claimed_base_amount",
            "initial_reference_price",
            "initialized_timestamp",
        }
        missing_keys = sorted(required_keys - set(raw.keys()))
        if missing_keys:
            if "schema_version" in missing_keys:
                self.logger().warning(
                    f"State file {self.state_path} appears to be from a pre-v6 controller version "
                    "(missing schema_version). It will be quarantined and the controller will re-initialize "
                    "from current wallet balances."
                )
            raise ValueError(f"Missing required state keys: {', '.join(missing_keys)}")

        try:
            schema_version = int(raw.get("schema_version"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid schema_version: {raw.get('schema_version')}") from exc

        if schema_version not in self.SUPPORTED_STATE_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported state schema_version {schema_version}; supported versions: "
                f"{sorted(self.SUPPORTED_STATE_SCHEMA_VERSIONS)}"
            )

        if raw.get("controller_name") != self.config.controller_name:
            raise ValueError(
                f"State controller_name {raw.get('controller_name')} does not match {self.config.controller_name}"
            )
        if raw.get("controller_type") != self.config.controller_type:
            raise ValueError(
                f"State controller_type {raw.get('controller_type')} does not match {self.config.controller_type}"
            )
        if raw.get("controller_id") != self.config.id:
            raise ValueError(
                f"State controller_id {raw.get('controller_id')} does not match {self.config.id}"
            )
        if raw.get("connector_name") != self.config.connector_name:
            raise ValueError(
                f"State connector_name {raw.get('connector_name')} does not match {self.config.connector_name}"
            )
        if raw.get("trading_pair") != self.config.trading_pair:
            raise ValueError(
                f"State trading_pair {raw.get('trading_pair')} does not match {self.config.trading_pair}"
            )
        if raw.get("base_asset") != base_asset or raw.get("quote_asset") != quote_asset:
            raise ValueError(
                f"State assets {raw.get('base_asset')}-{raw.get('quote_asset')} do not match {base_asset}-{quote_asset}"
            )
        if raw.get("initialized") is not True:
            raise ValueError("State initialized flag must be true")

        validated = dict(raw)
        for key in [
            "reserve_quote_balance",
            "reserve_base_balance",
            "initial_managed_quote",
            "initial_claimed_base_amount",
            "initial_reference_price",
            "initialized_timestamp",
        ]:
            try:
                parsed = _safe_decimal(raw.get(key), f"state field '{key}'")
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            if parsed < Decimal("0"):
                raise ValueError(f"State field '{key}' must be non-negative")
            validated[key] = str(parsed)

        initialized_timestamp = Decimal(validated["initialized_timestamp"])
        now_ts = Decimal(str(self.market_data_provider.time()))
        if initialized_timestamp > (now_ts + self.STATE_MAX_FUTURE_SKEW_SECONDS):
            raise ValueError(
                f"State initialized_timestamp {initialized_timestamp} is unreasonably far in the future"
            )

        # Migration from v9 → v10: derive owned_quote/owned_base from existing fields
        if "owned_quote" not in validated:
            initial_managed = Decimal(validated.get("initial_managed_quote", "0"))
            validated["owned_quote"] = str(initial_managed)
        if "owned_base" not in validated:
            initial_base = Decimal(validated.get("initial_claimed_base_amount", "0"))
            validated["owned_base"] = str(initial_base)
        # Self-balance hardening: derive the seed value (deploy-ceiling floor) for
        # states written before seed_value_quote existed. The realized seed value is
        # the initial managed quote plus the initial claimed base valued at the
        # initial reference price.
        if "seed_value_quote" not in validated:
            initial_managed = Decimal(validated.get("initial_managed_quote", "0"))
            initial_base = Decimal(validated.get("initial_claimed_base_amount", "0"))
            initial_ref = Decimal(validated.get("initial_reference_price", "0"))
            validated["seed_value_quote"] = str(initial_managed + initial_base * initial_ref)

        # v12 Issue 5: validate the ledger fields now that the v9->v10 migration defaults
        # guarantee the keys exist. A corrupt (negative / NaN / non-numeric) ledger value
        # must trip the quarantine path instead of loading silently and corrupting every
        # reported figure (managed fund value, PnL, deploy ceiling, reconciliation gap).
        for key in ["owned_quote", "owned_base", "seed_value_quote"]:
            try:
                parsed = _safe_decimal(validated.get(key), f"state field '{key}'")
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            if parsed < Decimal("0"):
                raise ValueError(f"State field '{key}' must be non-negative")
            validated[key] = str(parsed)

        # v12 Issue 5: tracked_fill_executor_ids must be a list; coerce anything else
        # (missing, null, scalar, dict) to an empty list so the fills-loop never crashes.
        if not isinstance(validated.get("tracked_fill_executor_ids"), list):
            validated["tracked_fill_executor_ids"] = []

        validated["schema_version"] = self.STATE_SCHEMA_VERSION
        if schema_version != self.STATE_SCHEMA_VERSION:
            validated["migrated_from_schema_version"] = schema_version
        return validated


    def _load_state(self):
        if self._state_loaded:
            return

        self._state_recovery_reason = None
        self._state_recovery_backup_path = None
        self._state_migrated_from_version = None

        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                self._quarantine_state_file("corrupt_state", str(e))
                self._state = {}
            else:
                try:
                    validated_state = self._validate_loaded_state(raw)
                except ValueError as e:
                    self._quarantine_state_file("invalid_state", str(e))
                    self._state = {}
                else:
                    self._state = validated_state
                    migrated_from = validated_state.get("migrated_from_schema_version")
                    if migrated_from is not None:
                        self._state_migrated_from_version = int(migrated_from)
                        self.logger().warning(
                            f"Migrating state file {self.state_path} from schema v{migrated_from} "
                            f"to v{self.STATE_SCHEMA_VERSION}."
                        )
                        self._emit_structured(
                            "range_ladder_state_migrated",
                            state_file=str(self.state_path),
                            from_schema_version=str(migrated_from),
                            to_schema_version=str(self.STATE_SCHEMA_VERSION),
                        )
                        self._state.pop("migrated_from_schema_version", None)
                        self._save_state()
        else:
            self._state = {}

        self._state_loaded = True

    def _ensure_state_owner_marker(self):
        """First-save sidecar `<state>.owner` marker (controller id + PID + start timestamp).
        A marker already written by a DIFFERENT controller id means two controllers are
        pointed at ONE state file and are corrupting each other's ledger -- warn loudly and
        emit range_ladder_state_file_contention, but do NOT block (warn-only by design).
        Exception-safe: marker problems never block a state save."""
        if self._state_owner_checked:
            return
        self._state_owner_checked = True
        try:
            marker = Path(f"{self.state_path}.owner")
            if marker.exists():
                try:
                    existing = json.loads(marker.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
                existing_id = existing.get("controller_id")
                if existing_id and existing_id != self.config.id:
                    self.logger().warning(
                        f"{self.config.id}: STATE FILE CONTENTION -- {self.state_path_abs} is marked "
                        f"as owned by controller '{existing_id}' (pid={existing.get('pid')}, "
                        f"started_at={existing.get('started_at')}). Two controllers sharing one "
                        "state file corrupt each other's ledger. Continuing anyway (warn-only)."
                    )
                    self._emit_structured(
                        "range_ladder_state_file_contention",
                        state_file=str(self.state_path),
                        state_file_abs=str(self.state_path_abs),
                        marker_controller_id=existing_id,
                        marker_pid=existing.get("pid"),
                        marker_started_at=existing.get("started_at"),
                    )
            marker.write_text(
                json.dumps(
                    {
                        "controller_id": self.config.id,
                        "pid": os.getpid(),
                        "started_at": self.market_data_provider.time(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_state_owner_marker()
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, sort_keys=True)
                # Flush + fsync BEFORE the atomic replace: without it, a host power loss can
                # leave a torn/zero-length state file, and the quarantine path would then
                # re-initialize from the current wallet -- silently resetting seed_value_quote
                # and the ledger baseline.
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self.state_path))
            # Also fsync the parent directory so the rename itself is durable (POSIX). On
            # platforms where directories cannot be opened/fsynced (e.g. Windows), this is a
            # silent no-op -- the file-content fsync above is the load-bearing part.
            try:
                dir_fd = os.open(str(self.state_path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise


    def _compute_seed_claim(self, *, reference_price: Decimal, available_quote_balance: Decimal,
                            available_base_balance: Decimal, target_quote: Decimal):
        """Compute the one-time seed claim as a SUBSET of target_quote. The base sleeve is
        carved OUT of target_quote (not additive); claimed_base_value_quote / claimed_base_amount
        is a one-time STARTING seed only -- after seeding, deployment tracks the live wallet.

            base_seed_value  = min(available_base * ref, claimed_base_value_quote)
            base_seed_amount = base_seed_value / ref   (quantized)
            quote_seed       = min(available_quote, target_quote - base_seed_value)
            total seed value = quote_seed + base_seed_value  <=  target_quote

        Shared by first-init (_ensure_initialized) and the v13 guarded re-seed (_maybe_reseed_fund).
        Returns (managed_quote_claim, claimed_base_amount, base_seed_value, quote_seed,
        seed_value_quote, claim_source).
        """
        base_seed_value = Decimal("0")
        claimed_base_amount = Decimal("0")
        claim_source = "none"
        if self.config.use_wallet_balance and reference_price > Decimal("0"):
            available_base_value = available_base_balance * reference_price
            if self.config.claimed_base_amount is not None and self.config.claimed_base_amount > Decimal("0"):
                # Explicit base amount claim, still bounded as a subset of target_quote.
                desired_base_value = min(available_base_balance, self.config.claimed_base_amount) * reference_price
                base_seed_value = min(desired_base_value, Decimal(target_quote))
                claim_source = "claimed_base_amount"
            elif self.config.claimed_base_value_quote > Decimal("0"):
                base_seed_value = min(available_base_value, Decimal(self.config.claimed_base_value_quote))
                claim_source = "claimed_base_value_quote"
            base_seed_value = max(Decimal("0"), base_seed_value)
            if base_seed_value > Decimal("0"):
                claimed_base_amount = Decimal(
                    self.market_data_provider.quantize_order_amount(
                        self.config.connector_name, self.config.trading_pair, base_seed_value / reference_price
                    )
                )
                # Re-derive value from the quantized amount so seed_value stays consistent.
                base_seed_value = claimed_base_amount * reference_price

        quote_seed = min(
            available_quote_balance,
            max(Decimal("0"), Decimal(target_quote) - base_seed_value),
        )
        managed_quote_claim = quote_seed
        seed_value_quote = quote_seed + base_seed_value
        return managed_quote_claim, claimed_base_amount, base_seed_value, quote_seed, seed_value_quote, claim_source

    def _ensure_initialized(self, reference_price: Decimal) -> bool:
        self._load_state()
        if self._state.get("initialized"):
            self._initialization_blocked_reason = None
            self._initialization_blocked_logged = False
            return True

        base_asset, quote_asset = split_hb_trading_pair(self.config.trading_pair)
        total_quote_balance = Decimal(self.market_data_provider.get_balance(self.config.connector_name, quote_asset))
        total_base_balance = Decimal(self.market_data_provider.get_balance(self.config.connector_name, base_asset))
        available_quote_balance = Decimal(
            self.market_data_provider.get_available_balance(self.config.connector_name, quote_asset)
        )
        available_base_balance = Decimal(
            self.market_data_provider.get_available_balance(self.config.connector_name, base_asset)
        )

        unavailable_quote_balance = max(Decimal("0"), total_quote_balance - available_quote_balance)
        unavailable_base_balance = max(Decimal("0"), total_base_balance - available_base_balance)

        if (
            not self.config.allow_initialize_with_unavailable_wallet_funds
            and (
                unavailable_quote_balance > self.INITIALIZATION_UNAVAILABLE_BALANCE_TOLERANCE
                or unavailable_base_balance > self.INITIALIZATION_UNAVAILABLE_BALANCE_TOLERANCE
            )
        ):
            self._initialization_blocked_reason = "unavailable_wallet_funds_at_init"
            if not self._initialization_blocked_logged:
                self._initialization_blocked_logged = True
                self.logger().warning(
                    f"{self.config.id}: refusing first-time initialization because some wallet funds are already "
                    f"unavailable/reserved on the exchange. available_quote={available_quote_balance} "
                    f"total_quote={total_quote_balance} | available_base={available_base_balance} "
                    f"total_base={total_base_balance}. Clear the external reservations first or explicitly "
                    "set allow_initialize_with_unavailable_wallet_funds=true if you accept the accounting risk."
                )
                self._emit_structured(
                    "range_ladder_initialization_blocked_unavailable_funds",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    available_quote=str(available_quote_balance),
                    total_quote=str(total_quote_balance),
                    unavailable_quote=str(unavailable_quote_balance),
                    available_base=str(available_base_balance),
                    total_base=str(total_base_balance),
                    unavailable_base=str(unavailable_base_balance),
                )
            return False

        self._initialization_blocked_reason = None
        self._initialization_blocked_logged = False

        # Seed claim (one-time, first init only). The base sleeve is carved OUT of
        # total_amount_quote -- it is a SUBSET, not additive (see _compute_seed_claim).
        (managed_quote_claim, claimed_base_amount, base_seed_value,
         quote_seed, seed_value_quote, claim_source) = self._compute_seed_claim(
            reference_price=reference_price,
            available_quote_balance=available_quote_balance,
            available_base_balance=available_base_balance,
            target_quote=Decimal(self.config.total_amount_quote),
        )

        self._state = {
            "schema_version": self.STATE_SCHEMA_VERSION,
            "controller_name": self.config.controller_name,
            "controller_type": self.config.controller_type,
            "controller_id": self.config.id,
            "connector_name": self.config.connector_name,
            "trading_pair": self.config.trading_pair,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "initialized": True,
            "reserve_quote_balance": str(max(Decimal("0"), total_quote_balance - managed_quote_claim)),
            "reserve_base_balance": str(max(Decimal("0"), total_base_balance - claimed_base_amount)),
            "initial_managed_quote": str(managed_quote_claim),
            "initial_claimed_base_amount": str(claimed_base_amount),
            "initial_reference_price": str(reference_price),
            "initialized_timestamp": str(self.market_data_provider.time()),
            "allow_initialized_with_unavailable_wallet_funds": str(
                bool(self.config.allow_initialize_with_unavailable_wallet_funds)
            ),
            "owned_quote": str(managed_quote_claim),
            "owned_base": str(claimed_base_amount),
            "seed_value_quote": str(seed_value_quote),
            "tracked_fill_executor_ids": [],
        }
        self._save_state()

        if self._state_recovery_reason:
            self.logger().warning(
                f"Re-initialized {self.config.id} after {self._state_recovery_reason}. "
                "Reserve balances are based on current wallet state, not the original initialization."
            )
            self._emit_structured(
                "range_ladder_reinitialized_after_state_recovery",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                recovery_reason=self._state_recovery_reason,
                backup_file=str(self._state_recovery_backup_path) if self._state_recovery_backup_path else "",
                reserve_quote=str(self._state["reserve_quote_balance"]),
                reserve_base=str(self._state["reserve_base_balance"]),
                state_file=str(self.state_path),
            )
            if self._state_recovery_reason == "corrupt_state":
                self._emit_structured(
                    "range_ladder_reinitialized_after_corrupt_recovery",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    reserve_quote=str(self._state["reserve_quote_balance"]),
                    reserve_base=str(self._state["reserve_base_balance"]),
                    state_file=str(self.state_path),
                )

        self.logger().info(
            f"Initialized {self.config.id}: managed_quote={managed_quote_claim} {quote_asset}, "
            f"claimed_base={claimed_base_amount} {base_asset} (claim_source={claim_source}), "
            f"wallet_base_available={available_base_balance}, reference_price={reference_price}"
        )
        self._emit_structured(
            "range_ladder_initialized",
            connector=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            managed_quote=str(managed_quote_claim),
            claimed_base=str(claimed_base_amount),
            claim_source=claim_source,
            wallet_base_available=str(available_base_balance),
            reference_price=str(reference_price),
            reserve_quote=str(self._state["reserve_quote_balance"]),
            reserve_base=str(self._state["reserve_base_balance"]),
            state_file=str(self.state_path),
            schema_version=str(self.STATE_SCHEMA_VERSION),
            seed_value_quote=str(seed_value_quote),
        )

        # Diagnostics: surface a side that starts essentially unfunded so that
        # "no buys" / "no sells" is never a silent mystery. A side is unfunded when
        # the wallet cannot fund even a single minimum-notional order on that side.
        # This is EXPECTED for a lopsided wallet (policy 3.6: wait for fills to
        # convert), not an error -- the strategy will run one-sided until the market
        # moves a fill into the starved side.
        unfunded_threshold = self.config.min_order_quote
        available_base_value = available_base_balance * reference_price if reference_price > Decimal("0") else Decimal("0")
        if available_quote_balance < unfunded_threshold:
            self.logger().warning(
                f"{self.config.id}: BUY side initializes essentially unfunded "
                f"(available_quote={available_quote_balance} {quote_asset} < min_order_quote="
                f"{unfunded_threshold}). No buy orders will be placed until quote balance grows "
                "(e.g. via a sell fill or deposit). This is expected for a one-sided wallet."
            )
            self._emit_structured(
                "range_ladder_side_unfunded_at_init",
                side="buy",
                claimed_value=str(quote_seed),
                available_balance=str(available_quote_balance),
                available_value_quote=str(available_quote_balance),
                min_order_quote=str(unfunded_threshold),
            )
        if available_base_value < unfunded_threshold:
            self.logger().warning(
                f"{self.config.id}: SELL side initializes essentially unfunded "
                f"(available_base={available_base_balance} {base_asset}, value={available_base_value} "
                f"{quote_asset} < min_order_quote={unfunded_threshold}). No sell orders will be placed "
                "until base inventory grows (e.g. via a buy fill or deposit). This is expected for a "
                "one-sided wallet."
            )
            self._emit_structured(
                "range_ladder_side_unfunded_at_init",
                side="sell",
                claimed_value=str(base_seed_value),
                available_balance=str(available_base_balance),
                available_value_quote=str(available_base_value),
                min_order_quote=str(unfunded_threshold),
            )

        # Startup feasibility check: warn if no sell level is placeable after quantization
        if claimed_base_amount > Decimal("0"):
            try:
                eligible_sell_prices = [
                    p for p in self.config.sell_prices
                    if p > reference_price
                ]
                feasible_sell_found = False
                for sp in eligible_sell_prices:
                    q_price = Decimal(
                        self.market_data_provider.quantize_order_price(
                            self.config.connector_name, self.config.trading_pair, sp
                        )
                    )
                    q_amount = Decimal(
                        self.market_data_provider.quantize_order_amount(
                            self.config.connector_name, self.config.trading_pair, claimed_base_amount
                        )
                    )
                    notional = q_amount * q_price
                    if q_amount > Decimal("0") and notional >= self.config.min_order_quote:
                        feasible_sell_found = True
                        break
                if not feasible_sell_found and eligible_sell_prices:
                    q_amount_diag = Decimal(
                        self.market_data_provider.quantize_order_amount(
                            self.config.connector_name, self.config.trading_pair, claimed_base_amount
                        )
                    )
                    self.logger().warning(
                        f"{self.config.id}: STARTUP SELL FEASIBILITY WARNING — "
                        f"claimed_base={claimed_base_amount} {base_asset} quantizes to {q_amount_diag}, "
                        f"which cannot satisfy min_order_quote={self.config.min_order_quote} "
                        f"at any eligible sell level {[str(p) for p in eligible_sell_prices]}. "
                        f"No sell orders will be placed until base inventory increases. "
                        f"Consider raising claimed_base_value_quote or using claimed_base_amount."
                    )
                    self._emit_structured(
                        "range_ladder_startup_sell_infeasible",
                        claimed_base=str(claimed_base_amount),
                        quantized_base=str(q_amount_diag),
                        eligible_sell_prices=[str(p) for p in eligible_sell_prices],
                        min_order_quote=str(self.config.min_order_quote),
                        reference_price=str(reference_price),
                    )
            except Exception as e:
                self.logger().debug(f"Startup sell feasibility check failed (non-critical): {e}")
        return True

    @staticmethod
    def _safe_order_decimal(obj, attr) -> Optional[Decimal]:
        """Read a numeric attribute off an order/fee object, returning None if it is missing,
        None, or non-numeric (so a weird/absent value never crashes the booking sweep)."""
        if obj is None:
            return None
        value = getattr(obj, attr, None)
        if value is None:
            return None
        try:
            return _safe_decimal(value, attr)
        except (ValueError, TypeError, InvalidOperation):
            return None

    def _order_fee_breakdown(self, order, info: Dict, quote_asset: str, base_asset: str, exec_quote: Decimal):
        """Cumulative fee for an order, source-robust. Returns (fee_in_quote, alt_fees) where
        alt_fees maps any NON-quote fee asset -> cumulative amount (recorded for visibility but
        NOT folded into the two-asset ledger -- see the alternateFeeAsset edge case).

        Preference: per-fill TradeUpdate fee tokens (NonKYC: quote-denominated flat fees) ->
        connector cumulative_fee_paid(quote) -> property-style cumulative fee ->
        custom_info.cum_fees_quote -> fee_rate fallback (NOT 0; NonKYC orders carry no fee).
        """
        alt_fees: Dict[str, Decimal] = {}
        fee_in_quote: Optional[Decimal] = None

        order_fills = getattr(order, "order_fills", None) if order is not None else None
        if isinstance(order_fills, dict) and order_fills:
            fee_in_quote = Decimal("0")
            for trade_update in order_fills.values():
                fee_obj = getattr(trade_update, "fee", None)
                flat_fees = getattr(fee_obj, "flat_fees", None) or []
                for token_amount in flat_fees:
                    token = getattr(token_amount, "token", None)
                    amount = self._safe_order_decimal(token_amount, "amount") or Decimal("0")
                    if amount <= Decimal("0"):
                        continue
                    if token == quote_asset:
                        fee_in_quote += amount
                    else:
                        # Fee charged in the base asset or a third asset: it must NOT be
                        # subtracted from owned_quote. Record it for visibility only.
                        alt_fees[str(token)] = alt_fees.get(str(token), Decimal("0")) + amount

        if fee_in_quote is None and order is not None:
            method = getattr(order, "cumulative_fee_paid", None)
            if callable(method):
                try:
                    paid = method(quote_asset)
                    if paid is not None:
                        fee_in_quote = max(Decimal("0"), self._d(paid, "0"))
                except Exception:
                    fee_in_quote = None

        if fee_in_quote is None:
            for attr in ("cumulative_fee_in_quote", "cum_fees_quote"):
                val = self._safe_order_decimal(order, attr)
                if val is not None:
                    fee_in_quote = max(Decimal("0"), val)
                    break

        if fee_in_quote is None and info.get("cum_fees_quote") is not None:
            fee_in_quote = max(Decimal("0"), self._d(info.get("cum_fees_quote"), "0"))

        if fee_in_quote is None:
            # Fallback: NonKYC orders carry no fee field; derive from the configured rate so a
            # silent 0 never slowly overstates the fund.
            fee_in_quote = max(Decimal("0"), exec_quote * Decimal(self.config.fee_rate))

        return fee_in_quote, alt_fees

    def _sample_order_execution(self, executor: ExecutorInfo, quote_asset: str, base_asset: str):
        """Sample one of OUR orders' CUMULATIVE executed figures, source-robust.

        Returns (exec_base, exec_quote, exec_fees_quote, alt_fees). These are cumulative
        totals (not increments); the booking loop diffs them against the persisted progress.
        """
        order = self._get_executor_in_flight_order(executor)
        info = getattr(executor, "custom_info", {}) or {}
        order_price = self._d(getattr(executor.config, "price", "0") or "0")

        # exec_base: in-flight executed_amount_base -> custom_info.filled_amount_base
        #            -> derive from custom_info.filled_amount_quote / price -> 0
        exec_base = self._safe_order_decimal(order, "executed_amount_base")
        if exec_base is None and info.get("filled_amount_base") is not None:
            exec_base = self._d(info.get("filled_amount_base"), "0")
        if exec_base is None and info.get("filled_amount_quote") is not None and order_price > Decimal("0"):
            exec_base = self._d(info.get("filled_amount_quote"), "0") / order_price
        if exec_base is None:
            exec_base = Decimal("0")
        exec_base = max(Decimal("0"), exec_base)

        # exec_quote: in-flight executed_amount_quote -> custom_info.filled_amount_quote
        #             -> exec_base * order_price (exact for resting maker fills)
        exec_quote = self._safe_order_decimal(order, "executed_amount_quote")
        if exec_quote is None and info.get("filled_amount_quote") is not None:
            exec_quote = self._d(info.get("filled_amount_quote"), "0")
        if exec_quote is None:
            exec_quote = exec_base * order_price
        exec_quote = max(Decimal("0"), exec_quote)

        exec_fees, alt_fees = self._order_fee_breakdown(order, info, quote_asset, base_asset, exec_quote)
        return exec_base, exec_quote, exec_fees, alt_fees

    def _book_fills_from_orders(self):
        """v13 Part A: robust per-order incremental fill booking.

        Book each of OUR OWN orders' CUMULATIVE executed amounts (sampled every cycle) into
        owned_quote / owned_base. Partial fills accrue correctly; external transfers (a deposit
        or withdrawal is not an order) are ignored by construction; only orders carrying a
        level_id are booked, so a shared account's other activity is irrelevant.

        Per-order cumulative progress is persisted in state under "booked_fill_progress" (a new
        OPTIONAL key -- no schema bump). Monotonic guards mean a momentary 0/missing read never
        un-books. A closing executor gets a final sampling pass before its progress entry is
        pruned (so the map never grows unbounded).
        """
        if not self._state.get("initialized"):
            return

        base_asset, quote_asset = split_hb_trading_pair(self.config.trading_pair)
        owned_quote = self._d(self._state.get("owned_quote"), "0")
        owned_base = self._d(self._state.get("owned_base"), "0")
        progress_raw = self._state.get("booked_fill_progress")
        progress: Dict[str, Dict[str, str]] = dict(progress_raw) if isinstance(progress_raw, dict) else {}

        # Reset the per-cycle booked-fill side flags (drive the recycle windows).
        self._booked_buy_fill_this_cycle = False
        self._booked_sell_fill_this_cycle = False

        # One cycle after a re-seed, re-baseline open orders to their current cumulative WITHOUT
        # booking, so already-realized fills (already reflected in the re-seeded wallet) are not
        # retroactively re-booked. Subsequent cycles book normally from that baseline.
        reseed_priming = self._reseed_just_applied
        self._reseed_just_applied = False

        changed = False
        current_ids = set()

        for executor in self.executors_info:
            level_id = getattr(executor.config, "level_id", None)
            if level_id is None:
                continue  # not ours
            eid = executor.id
            current_ids.add(eid)
            side = getattr(executor.config, "side", None)

            exec_base, exec_quote, exec_fees, alt_fees = self._sample_order_execution(
                executor, quote_asset, base_asset
            )

            entry = progress.get(eid) or {}
            booked_base = self._d(entry.get("base"), "0")
            booked_quote = self._d(entry.get("quote"), "0")
            booked_fees = self._d(entry.get("fees"), "0")

            # Monotonic: never let a momentary low/missing read un-book a prior sample.
            exec_base = max(exec_base, booked_base)
            exec_quote = max(exec_quote, booked_quote)
            exec_fees = max(exec_fees, booked_fees)

            if reseed_priming:
                # Establish the post-reseed baseline; book nothing this cycle.
                progress[eid] = {"base": str(exec_base), "quote": str(exec_quote), "fees": str(exec_fees)}
                continue

            d_base = max(Decimal("0"), exec_base - booked_base)
            d_quote = max(Decimal("0"), exec_quote - booked_quote)
            d_fees = max(Decimal("0"), exec_fees - booked_fees)

            if d_base <= Decimal("0") and d_quote <= Decimal("0"):
                continue  # nothing new for this order

            if side == TradeType.BUY:
                owned_base += d_base
                owned_quote -= (d_quote + d_fees)
                self._booked_buy_fill_this_cycle = True
            elif side == TradeType.SELL:
                owned_base -= d_base
                owned_quote += (d_quote - d_fees)
                self._booked_sell_fill_this_cycle = True
            else:
                continue  # unknown side -- do not book

            owned_quote = max(Decimal("0"), owned_quote)
            owned_base = max(Decimal("0"), owned_base)
            progress[eid] = {"base": str(exec_base), "quote": str(exec_quote), "fees": str(exec_fees)}
            changed = True

            self._emit_structured(
                "range_ladder_fill_booked",
                executor_id=eid,
                level_id=level_id,
                side=(side.name if side is not None else ""),
                d_base=str(d_base),
                d_quote=str(d_quote),
                d_fees=str(d_fees),
                owned_quote=str(owned_quote),
                owned_base=str(owned_base),
                alt_fees={k: str(v) for k, v in alt_fees.items()},
            )

        # Prune progress entries for executors that have left executors_info (after the final
        # capture pass above), so the map does not grow without bound.
        pruned_ids = [eid for eid in progress if eid not in current_ids]
        for eid in pruned_ids:
            del progress[eid]

        if changed:
            # v15: arm the fill-settle grace window — the wallet snapshot will lag this fill
            # by up to one balance poll, so over-claim checks defer until it re-syncs.
            self._last_fill_booked_ts = self.market_data_provider.time()

        if changed or pruned_ids or reseed_priming:
            self._state["owned_quote"] = str(owned_quote)
            self._state["owned_base"] = str(owned_base)
            self._state["booked_fill_progress"] = progress
            self._save_state()
            if changed:
                self.logger().info(
                    f"{self.config.id}: ledger updated from fills. "
                    f"owned_quote={owned_quote} owned_base={owned_base}"
                )
                self._emit_structured(
                    "range_ladder_ledger_updated",
                    owned_quote=str(owned_quote),
                    owned_base=str(owned_base),
                    booked_orders=len(progress),
                )

    def _update_ledger_from_completed_executors(self):
        """Deprecated v12 name; thin alias for the v13 per-order booking. Kept so any external
        caller (and the existing integration tests) still resolve."""
        return self._book_fills_from_orders()

    def _evaluate_refresh_triggers(self, now: float):
        """Per-side cooldown + immediate cross-side refresh state machine (control-flow steps
        1-4). Called once per fully-processed cycle, AFTER _book_fills_from_orders has set the
        per-cycle booked-fill flags. Only sets the *_side_dirty intents; the cancel/recreate is
        applied later in stop_actions_proposal / create_actions_proposal.

        Contract:
          - Buy fill  -> dirty the SELL side (deploy the new base) + arm the BUY cooldown.
          - Sell fill -> dirty the BUY side (deploy the new quote) + arm the SELL cooldown.
          - buy_cooldown lapses (no buy fill for its duration) -> re-center the BUY side once.
          - sell_cooldown lapses -> re-center the SELL side once.
          - executor_refresh_time elapses -> global refresh of BOTH sides.
        A fill NEVER dirties its own side; a placement is not a fill (see _book_fills_from_orders),
        so this can never self-trigger a loop.
        """
        if not self.config.event_refresh_enabled:
            return

        triggers: List[str] = []

        # Step 4: initial placement -- both sides start dirty; seed the global timer so its
        # first fire is one full interval after session start (not immediately).
        if not self._refresh_timers_initialized:
            self._refresh_timers_initialized = True
            self._last_global_refresh_ts = now
            self._mark_side_dirty("buy", "initial_placement")
            self._mark_side_dirty("sell", "initial_placement")
            triggers.append("initial_placement")

        # Step 1: fills -> reset own cooldown (arm it) + dirty the OPPOSITE side immediately.
        if self._booked_buy_fill_this_cycle:
            self._last_buy_fill_ts = now
            self._buy_cooldown_armed = True
            self._mark_side_dirty("sell", "buy_fill")
            triggers.append("buy_fill->sell_refresh")
        if self._booked_sell_fill_this_cycle:
            self._last_sell_fill_ts = now
            self._sell_cooldown_armed = True
            self._mark_side_dirty("buy", "sell_fill")
            triggers.append("sell_fill->buy_refresh")

        # Step 2: cooldown lapse -> dirty the SAME side once, then disarm (re-arms on next fill).
        if self._buy_cooldown_armed and (now - self._last_buy_fill_ts) >= float(self.config.effective_buy_cooldown_time):
            self._buy_cooldown_armed = False
            self._mark_side_dirty("buy", "buy_cooldown_lapsed")
            triggers.append("buy_cooldown_lapsed")
        if self._sell_cooldown_armed and (now - self._last_sell_fill_ts) >= float(self.config.effective_sell_cooldown_time):
            self._sell_cooldown_armed = False
            self._mark_side_dirty("sell", "sell_cooldown_lapsed")
            triggers.append("sell_cooldown_lapsed")

        # Step 3: global periodic timer -> dirty BOTH sides.
        if (now - self._last_global_refresh_ts) >= float(self.config.executor_refresh_time):
            self._last_global_refresh_ts = now
            self._mark_side_dirty("buy", "global_timer")
            self._mark_side_dirty("sell", "global_timer")
            triggers.append("global_timer")

        if triggers:
            self._emit_structured(
                "range_ladder_refresh_triggers",
                triggers=triggers,
                buy_side_dirty=self._buy_side_dirty,
                sell_side_dirty=self._sell_side_dirty,
                buy_cooldown_armed=self._buy_cooldown_armed,
                sell_cooldown_armed=self._sell_cooldown_armed,
            )

    def _mark_side_dirty(self, side: str, reason: str):
        """Mark a side as needing a refresh. The FIRST reason in a cycle wins for the event
        label (initial/fill reasons fire before cooldown/global, which is the useful ordering)."""
        if side == "buy":
            if not self._buy_side_dirty:
                self._buy_dirty_reason = reason
            self._buy_side_dirty = True
        else:
            if not self._sell_side_dirty:
                self._sell_dirty_reason = reason
            self._sell_side_dirty = True

    def _refresh_status_fields(self, now: float) -> Dict[str, Any]:
        """Per-side refresh state surfaced in processed_data (and thence status / custom_info):
        the toggle, effective per-side cooldowns, seconds-until each cooldown lapse (None when a
        side is not armed / not counting down), both dirty flags, and the global-timer clock."""
        buy_cd = self.config.effective_buy_cooldown_time
        sell_cd = self.config.effective_sell_cooldown_time
        buy_cd_remaining = (
            max(0.0, float(buy_cd) - (now - self._last_buy_fill_ts))
            if self._buy_cooldown_armed else None
        )
        sell_cd_remaining = (
            max(0.0, float(sell_cd) - (now - self._last_sell_fill_ts))
            if self._sell_cooldown_armed else None
        )
        global_remaining = max(
            0.0, float(self.config.executor_refresh_time) - (now - self._last_global_refresh_ts)
        )
        return {
            "event_refresh_enabled": bool(self.config.event_refresh_enabled),
            "buy_cooldown_time": buy_cd,
            "sell_cooldown_time": sell_cd,
            "buy_cooldown_armed": self._buy_cooldown_armed,
            "sell_cooldown_armed": self._sell_cooldown_armed,
            "buy_cooldown_remaining_s": buy_cd_remaining,
            "sell_cooldown_remaining_s": sell_cd_remaining,
            "buy_side_dirty": self._buy_side_dirty,
            "sell_side_dirty": self._sell_side_dirty,
            "buy_dirty_reason": self._buy_dirty_reason,
            "sell_dirty_reason": self._sell_dirty_reason,
            "last_global_refresh_ts": self._last_global_refresh_ts,
            "global_refresh_remaining_s": global_remaining,
        }

    def _maybe_reseed_fund(self, reference_price: Decimal):
        """v13 Part C: guarded, one-shot managed-fund re-seed from the CURRENT wallet.

        Re-seeding is the intended way to change the managed baseline, because deposits are
        deliberately ignored by the fund (a transfer is not an order). Idempotent per
        (reseed_generation, reseed_fund_target_quote) token: it never repeats for the same token
        even if reseed_fund_from_wallet_once is left True. Bump reseed_generation to re-arm once.
        """
        if not self.config.reseed_fund_from_wallet_once:
            return
        if not self._state.get("initialized"):
            return  # first init handles the seed; nothing to re-seed yet
        if reference_price <= Decimal("0"):
            return

        target_quote = (
            Decimal(self.config.reseed_fund_target_quote)
            if self.config.reseed_fund_target_quote is not None
            else Decimal(self.config.total_amount_quote)
        )
        reseed_token = f"{int(self.config.reseed_generation)}:{target_quote}"
        if self._state.get("last_reseed_token") == reseed_token:
            return  # this exact re-seed already applied -> idempotent no-op

        # The re-seed claims from AVAILABLE balances only, so funds held in this controller's
        # own resting orders would be excluded from the new seed; when those orders later cancel
        # the money returns to the wallet, but a cancel is not a fill, so the ledger never
        # re-grows and the capital strands outside the fund. Defer until the book is flat: the
        # re-seed applies automatically on the first cycle with no live order executors (e.g.
        # after a session-end cancel wave). The token is NOT consumed by a deferral.
        active_executors = self._order_executors_active_or_shutting_down()
        if active_executors:
            if self._reseed_deferred_warned_token != reseed_token:
                self._reseed_deferred_warned_token = reseed_token
                self.logger().warning(
                    f"{self.config.id}: re-seed (token={reseed_token}) deferred: "
                    f"{len(active_executors)} order executor(s) still active or shutting down "
                    "hold funds that the wallet-based claim would strand. The re-seed applies "
                    "automatically on the first cycle with a flat book."
                )
                self._emit_structured(
                    "range_ladder_reseed_deferred_active_orders",
                    reseed_token=reseed_token,
                    active_executor_count=len(active_executors),
                )
            return

        base_asset, quote_asset = split_hb_trading_pair(self.config.trading_pair)
        total_quote_balance = self._safe_get_balance(quote_asset)
        total_base_balance = self._safe_get_balance(base_asset)
        available_quote_balance = self._safe_get_available_balance(quote_asset)
        available_base_balance = self._safe_get_available_balance(base_asset)

        (managed_quote_claim, claimed_base_amount, base_seed_value,
         quote_seed, seed_value_quote, claim_source) = self._compute_seed_claim(
            reference_price=reference_price,
            available_quote_balance=available_quote_balance,
            available_base_balance=available_base_balance,
            target_quote=target_quote,
        )

        old_owned_quote = self._d(self._state.get("owned_quote"), "0")
        old_owned_base = self._d(self._state.get("owned_base"), "0")
        old_seed_value = self._d(self._state.get("seed_value_quote"), "0")

        # Recompute owned_*/seed_value/initial_*/reserve_* from the current wallet; clear the
        # fills bookkeeping so the new baseline books forward cleanly. Never wipe unrelated state.
        self._state["reserve_quote_balance"] = str(max(Decimal("0"), total_quote_balance - managed_quote_claim))
        self._state["reserve_base_balance"] = str(max(Decimal("0"), total_base_balance - claimed_base_amount))
        self._state["initial_managed_quote"] = str(managed_quote_claim)
        self._state["initial_claimed_base_amount"] = str(claimed_base_amount)
        self._state["initial_reference_price"] = str(reference_price)
        self._state["owned_quote"] = str(managed_quote_claim)
        self._state["owned_base"] = str(claimed_base_amount)
        self._state["seed_value_quote"] = str(seed_value_quote)
        self._state["booked_fill_progress"] = {}
        self._state["tracked_fill_executor_ids"] = []
        self._state["last_reseed_token"] = reseed_token
        self._save_state()

        # Next booking pass re-baselines open orders to their current cumulative WITHOUT booking,
        # so already-realized fills (already reflected in the re-seeded wallet) are not re-booked.
        self._reseed_just_applied = True

        self.logger().warning(
            f"{self.config.id}: managed fund RE-SEEDED from wallet (token={reseed_token}, "
            f"claim_source={claim_source}). owned_quote {old_owned_quote}->{managed_quote_claim} "
            f"owned_base {old_owned_base}->{claimed_base_amount} "
            f"seed_value {old_seed_value}->{seed_value_quote}"
        )
        self._emit_structured(
            "range_ladder_fund_reseeded",
            reseed_token=reseed_token,
            reseed_generation=int(self.config.reseed_generation),
            target_quote=str(target_quote),
            claim_source=claim_source,
            old_owned_quote=str(old_owned_quote),
            new_owned_quote=str(managed_quote_claim),
            old_owned_base=str(old_owned_base),
            new_owned_base=str(claimed_base_amount),
            old_seed_value_quote=str(old_seed_value),
            new_seed_value_quote=str(seed_value_quote),
            reserve_quote=self._state["reserve_quote_balance"],
            reserve_base=self._state["reserve_base_balance"],
            reference_price=str(reference_price),
        )

    def _set_unavailable_processed_data(
        self,
        *,
        base_asset: str,
        quote_asset: str,
        reference_price: Decimal,
        best_bid: Decimal,
        best_ask: Decimal,
        market_data_ready: bool,
        market_data_error: str = "",
        initialization_ready: bool = True,
        initialization_blocked_reason: str = "",
    ):
        perf = self._performance_snapshot()
        reserve_quote_balance = self._d(self._state.get("reserve_quote_balance"), "0") if self._state_loaded else Decimal("0")
        reserve_base_balance = self._d(self._state.get("reserve_base_balance"), "0") if self._state_loaded else Decimal("0")
        total_quote_balance = self._safe_get_balance(quote_asset)
        total_base_balance = self._safe_get_balance(base_asset)
        available_quote_balance = self._safe_get_available_balance(quote_asset)
        available_base_balance = self._safe_get_available_balance(base_asset)
        # Use controller-owned ledger instead of wallet-derived totals
        owned_quote = self._d(self._state.get("owned_quote"), "0") if self._state_loaded else Decimal("0")
        owned_base = self._d(self._state.get("owned_base"), "0") if self._state_loaded else Decimal("0")
        managed_quote_total = max(Decimal("0"), owned_quote)
        managed_base_total = max(Decimal("0"), owned_base)
        managed_fund_value_quote = managed_quote_total + (managed_base_total * reference_price if reference_price > Decimal("0") else Decimal("0"))
        now = self.market_data_provider.time()
        market_data_unavailable_duration_s = (
            Decimal(str(max(0.0, now - self._market_data_unavailable_since)))
            if self._market_data_unavailable_since is not None else Decimal("0")
        )
        self.processed_data = {
            "reference_price": reference_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "total_quote_balance": total_quote_balance,
            "total_base_balance": total_base_balance,
            "available_quote_balance": available_quote_balance,
            "available_base_balance": available_base_balance,
            "reserve_quote_balance": reserve_quote_balance,
            "reserve_base_balance": reserve_base_balance,
            "managed_quote_total": managed_quote_total,
            "managed_base_total": managed_base_total,
            "managed_fund_value_quote": managed_fund_value_quote,
            "cap_factor": Decimal("0"),
            "seed_value_quote": self._seed_value_quote() if self._state_loaded else Decimal("0"),
            "deploy_ceiling_quote": Decimal("0"),
            "deploy_headroom_quote": Decimal("0"),
            "deployable_quote_total": Decimal("0"),
            "deployable_base_total": Decimal("0"),
            "active_buy_reserved_quote": Decimal("0"),
            "active_sell_reserved_base": Decimal("0"),
            "free_buy_budget_quote": Decimal("0"),
            "free_sell_budget_base": Decimal("0"),
            "buy_fee_headroom_quote": Decimal("0"),
            "ledger_surplus_quote": Decimal("0"),
            "refresh_wave_release_buy_quote": Decimal("0"),
            "refresh_wave_release_sell_base": Decimal("0"),
            "blocked_level_ids": self._recently_closed_level_ids(),
            "initial_fund_value_quote": Decimal("0"),
            "fund_growth_quote": Decimal("0"),
            "reconciliation_gap_quote": Decimal("0"),
            "price_regime": "unavailable",
            "config_rebuild_pending": self._config_rebuild_pending,
            "state_migrated_from_version": self._state_migrated_from_version,
            "market_data_ready": market_data_ready,
            "market_data_error": market_data_error,
            "market_data_unavailable_duration_s": market_data_unavailable_duration_s,
            "market_data_hard_pause": self._market_data_hard_pause,
            "initialization_ready": initialization_ready,
            "initialization_blocked_reason": initialization_blocked_reason,
            "session_expired": self._session_expired,
            "session_expired_reason": self._session_expired_reason,
            **perf,
        }

    def _safe_market_price(self, price_type: PriceType, field_name: str) -> Decimal:
        raw_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, price_type
        )
        parsed = _safe_decimal(raw_price, field_name)
        if parsed <= Decimal("0"):
            raise ValueError(f"{field_name} must be greater than zero")
        return parsed

    def _connector_supports_limit_maker(self) -> bool:
        if self._supports_limit_maker_cache is not None:
            return self._supports_limit_maker_cache
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
        except Exception:
            self._supports_limit_maker_cache = False
            return False
        try:
            supported = connector.supported_order_types()
        except Exception:
            self._supports_limit_maker_cache = False
            return False
        self._supports_limit_maker_cache = OrderType.LIMIT_MAKER in supported
        return self._supports_limit_maker_cache

    def _passive_execution_strategy(self) -> ExecutionStrategy:
        if not self.config.passive_order_placement:
            return ExecutionStrategy.LIMIT
        if self._connector_supports_limit_maker():
            return ExecutionStrategy.LIMIT_MAKER
        if not self._limit_maker_fallback_warned:
            self._limit_maker_fallback_warned = True
            self.logger().warning(
                f"{self.config.id}: connector {self.config.connector_name} does not support LIMIT_MAKER. "
                "Passive placement will use LIMIT orders with best-effort passivity checks instead."
            )
            self._emit_structured(
                "range_ladder_limit_maker_unsupported_fallback",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
            )
        return ExecutionStrategy.LIMIT


    def _active_executors(self) -> List[ExecutorInfo]:
        return [executor for executor in self.executors_info if executor.is_active]

    def _active_order_executors(self) -> List[ExecutorInfo]:
        return [
            executor for executor in self._active_executors()
            if getattr(executor.config, "type", "") == "order_executor"
        ]

    def _order_executors_active_or_shutting_down(self) -> List[ExecutorInfo]:
        """
        Return order executors that still have a real claim on exchange capital
        and level identity -- i.e. RUNNING, NOT_STARTED, or SHUTTING_DOWN.

        This is deliberately broader than _active_order_executors(), which uses
        ExecutorInfo.is_active (RUNNING + NOT_STARTED only). We must include
        SHUTTING_DOWN here because:

          1. OrderExecutor.control_shutdown_process() sleeps 5s after cancel,
             during which the executor is SHUTTING_DOWN and its order may still
             be open on the exchange (funds still held).
          2. ExecutorOrchestrator.execute_actions() refuses new creates for
             the same (connector, pair, side) while ANY executor in that key
             is SHUTTING_DOWN (see executor_orchestrator.py _shutdown_in_flight_keys).

        If we exclude SHUTTING_DOWN here, the controller proposes new creates
        that the orchestrator immediately defers, and the controller's internal
        reservation & level-blocking go out of sync with the orchestrator's
        actual behavior.
        """
        live_statuses = {
            RunnableStatus.NOT_STARTED,
            RunnableStatus.RUNNING,
            RunnableStatus.SHUTTING_DOWN,
        }
        return [
            executor for executor in self.executors_info
            if getattr(executor, "status", None) in live_statuses
            and getattr(executor.config, "type", "") == "order_executor"
        ]

    def _recently_closed_level_ids(self) -> Set[str]:
        now = self.market_data_provider.time()
        self._cleanup_cooldown_bypass()
        live_statuses = {
            RunnableStatus.NOT_STARTED,
            RunnableStatus.RUNNING,
            RunnableStatus.SHUTTING_DOWN,  # still blocked during shutdown
        }
        blocked: Set[str] = set()
        event_mode = bool(self.config.event_refresh_enabled)
        for executor in self.executors_info:
            level_id = getattr(executor.config, "level_id", None)
            if level_id is None:
                continue
            status = getattr(executor, "status", None)
            if status in live_statuses:
                # An order already exists at this level -- always blocked from a duplicate create
                # (in BOTH modes), whether RUNNING, NOT_STARTED, or still SHUTTING_DOWN.
                blocked.add(level_id)
                continue
            if event_mode:
                # Per-side model: a CLOSED level carries no per-level cooldown. When (and whether)
                # its side rebuilds is governed entirely by the per-side cooldown + immediate
                # cross-side refresh -- a filled buy level does not re-buy until the BUY cooldown
                # lapses or the global timer fires, not via a per-level block here. This replaces
                # (does not double-act with) the legacy cooldown + recycle window below.
                continue
            if self._should_bypass_level_cooldown(level_id):
                continue
            # v12 Part B (legacy): directional recycle window bypass. Level IDs are buy_<token> /
            # sell_<token>; if this level's side has an open recycle window (opened by a
            # TOTAL-balance increase from a fill on the OTHER side), skip its cooldown so the
            # offsetting order deploys within recycle_max_latency_seconds.
            if level_id.startswith("buy_") and now < self._recycle_bypass_buy_until:
                continue
            if level_id.startswith("sell_") and now < self._recycle_bypass_sell_until:
                continue
            if executor.close_timestamp is not None and (now - executor.close_timestamp) < self.config.cooldown_time:
                blocked.add(level_id)
        return blocked

    @staticmethod
    def _executor_side(executor: ExecutorInfo) -> Optional[TradeType]:
        return getattr(executor.config, "side", None)

    @staticmethod
    def _executor_order_id(executor: ExecutorInfo) -> Optional[str]:
        custom_info = getattr(executor, "custom_info", {}) or {}
        order_id = custom_info.get("order_id")
        if order_id:
            return order_id
        order_ids = custom_info.get("order_ids")
        if isinstance(order_ids, list) and len(order_ids) > 0:
            return order_ids[-1]
        return None

    def _get_executor_in_flight_order(self, executor: ExecutorInfo):
        order_id = self._executor_order_id(executor)
        if not order_id:
            return None
        try:
            connector = self.market_data_provider.get_connector(executor.connector_name)
            in_flight_orders = getattr(connector, "in_flight_orders", {}) or {}
            return in_flight_orders.get(order_id)
        except Exception:
            return None

    def _remaining_open_order_amounts(self, executor: ExecutorInfo):
        configured_price = self._d(getattr(executor.config, "price", "0") or "0")
        configured_amount = self._d(getattr(executor.config, "amount", "0") or "0")
        order = self._get_executor_in_flight_order(executor)

        if order is None:
            return configured_price, configured_amount, max(Decimal("0"), configured_price * configured_amount), "config_fallback"

        try:
            live_amount = _safe_decimal(getattr(order, "amount", configured_amount), "live order amount")
        except ValueError:
            live_amount = configured_amount
        try:
            raw_price = order.price if getattr(order, "price", None) is not None else configured_price
            live_price = _safe_decimal(raw_price, "live order price")
        except ValueError:
            live_price = configured_price

        if getattr(order, "is_done", False):
            return live_price, Decimal("0"), Decimal("0"), "in_flight_done"

        try:
            executed_base = _safe_decimal(getattr(order, "executed_amount_base", Decimal("0")), "executed_amount_base", default="0")
        except ValueError:
            executed_base = Decimal("0")

        remaining_base = max(Decimal("0"), live_amount - executed_base)
        remaining_quote = max(Decimal("0"), remaining_base * live_price)
        return live_price, remaining_base, remaining_quote, "in_flight"

    def _active_reserved_quote_for_buys(self) -> Decimal:
        total = Decimal("0")
        counts: Dict[str, int] = {}
        # Use the shutdown-aware set -- SHUTTING_DOWN executors still hold quote on
        # the exchange until the cancel settles.
        for executor in self._order_executors_active_or_shutting_down():
            if self._executor_side(executor) == TradeType.BUY:
                _, _, remaining_quote, source = self._remaining_open_order_amounts(executor)
                total += remaining_quote
                counts[source] = counts.get(source, 0) + 1
        self._buy_reservation_sources = counts
        return total

    def _active_reserved_base_for_sells(self) -> Decimal:
        total = Decimal("0")
        counts: Dict[str, int] = {}
        # Use the shutdown-aware set -- SHUTTING_DOWN executors still hold base on
        # the exchange until the cancel settles.
        for executor in self._order_executors_active_or_shutting_down():
            if self._executor_side(executor) == TradeType.SELL:
                _, remaining_base, _, source = self._remaining_open_order_amounts(executor)
                total += remaining_base
                counts[source] = counts.get(source, 0) + 1
        self._sell_reservation_sources = counts
        return total

    # ------------------------------------------------------------------ refresh-wave budget fix
    # A refresh cancel wave releases the cancelled orders' collateral, but the release is
    # visible only asynchronously: the executors linger in SHUTTING_DOWN (still counted as
    # reserved) and the CACHED wallet balances lag the exchange by up to a balance poll.
    # Sizing the rebuild from the raw free budget in that window collapsed the live Kraken
    # ladder (free_sell_budget_base=7E-8 while 1.06 XMR sat in the outgoing orders). The wave
    # record lets the budget computation credit back exactly what the wave releases, from the
    # controller's OWN ledger -- never waiting on exchange balance updates.

    def _wave_reservation_of(self, executor: ExecutorInfo, side: TradeType) -> Decimal:
        """The reservation a wave credits for one executor: remaining quote for buys,
        remaining base for sells (same source as _active_reserved_*)."""
        _, remaining_base, remaining_quote, _ = self._remaining_open_order_amounts(executor)
        return remaining_quote if side == TradeType.BUY else remaining_base

    def _record_refresh_wave_cancels(self, side: TradeType, executors: List[ExecutorInfo],
                                     now: float, accumulate: bool = False):
        """Start (or, for the legacy per-executor path, extend) the side's wave record with
        the reservations of the executors being cancelled by this refresh."""
        side_name = "buy" if side == TradeType.BUY else "sell"
        released = Decimal("0")
        cancelled_ids: Set[str] = set()
        for executor in executors:
            released += max(Decimal("0"), self._wave_reservation_of(executor, side))
            cancelled_ids.add(executor.id)
        existing = self._refresh_wave.get(side_name)
        if accumulate and existing is not None:
            existing["released"] += released
            existing["cancelled_ids"] |= cancelled_ids
            existing["started_ts"] = now
            # More collateral is coming back: re-open the post-cancel balance gate.
            existing["balance_refresh_ts"] = None
            existing["balance_gate_done"] = False
            return
        self._refresh_wave[side_name] = self._new_wave_record(now, released, cancelled_ids)

    @staticmethod
    def _new_wave_record(now: float, released: Decimal, cancelled_ids: Set[str]) -> dict:
        return {
            "released": released,
            "cancelled_ids": cancelled_ids,
            "started_ts": now,
            "issued_ts": None,
            "issued_count": 0,
            "attempts": 0,
            "last_log": None,
            # Intended-vs-live reconciliation (preflight-retry fix)
            "intended": {},          # level_id -> intended amount, pruned as levels satisfy
            "next_retry_ts": 0.0,    # backoff for heal retries
            "cap_warned": False,     # retry-cap WARNING latch
            "preflight_drops": 0,    # feedback events received from the budget preflight
            # Post-cancel balance refresh/gate (preflight-retry fix)
            "balance_refresh_ts": None,
            "balance_gate_done": False,
        }

    def _note_side_creates_issued(self, side_name: str, actions: List[CreateExecutorAction], now: float):
        """Record an emitted rebuild for a side: issuance timestamp/count plus the INTENDED
        ladder (level -> amount) that the intended-vs-live reconciliation heals toward.
        Creates a released=0 wave record for cancel-less rebuilds (initial placement,
        watchdog re-place) so a dropped proposal is retried there too.

        A heal issuance (dirty reason "ladder_reconcile") MERGES its levels into the existing
        intent, keeping the retry counters; any other issuance is a NEW refresh generation --
        intent replaced, retry state reset (a newer refresh supersedes older proposals)."""
        record = self._refresh_wave.get(side_name)
        if record is None:
            record = self._new_wave_record(now, Decimal("0"), set())
            self._refresh_wave[side_name] = record
        reason = self._buy_dirty_reason if side_name == "buy" else self._sell_dirty_reason
        issued_intent = {
            a.executor_config.level_id: a.executor_config.amount
            for a in actions
            if getattr(a.executor_config, "level_id", None)
        }
        if reason == "ladder_reconcile" and record["intended"]:
            record["intended"].update(issued_intent)
        else:
            record["intended"] = issued_intent
            record["attempts"] = 0
            record["next_retry_ts"] = 0.0
            record["cap_warned"] = False
            record["preflight_drops"] = 0
        record["issued_ts"] = now
        record["issued_count"] = len(actions)

    def _wave_cancels_in_flight(self, side_name: str) -> bool:
        """True while any executor cancelled by this side's refresh wave is still
        live/shutting down. The side's rebuild must WAIT for them: their levels are still
        blocked, so placing early would concentrate the whole credited budget into the few
        unblocked rungs and deform the ladder into one oversized order."""
        record = self._refresh_wave.get(side_name)
        if not record or not record["cancelled_ids"]:
            return False
        side = TradeType.BUY if side_name == "buy" else TradeType.SELL
        return any(
            executor.id in record["cancelled_ids"] and self._executor_side(executor) == side
            for executor in self._order_executors_active_or_shutting_down()
        )

    def _maybe_request_post_cancel_balance_refresh(self, side_name: str, record: dict, now: float):
        """One-shot per wave: once the cancels have closed, ask the connector to refresh its
        cached balances so the budget preflight sees the freed collateral within a REST
        round-trip instead of a full polling cycle. Connector-agnostic and best-effort: a
        connector without _update_balances (or no running loop) is skipped silently -- the
        gate timeout and the reconciliation retries cover it."""
        if record.get("balance_refresh_ts") is not None:
            return
        if not record["cancelled_ids"] or record["released"] <= Decimal("0"):
            return
        record["balance_refresh_ts"] = now
        if int(self.config.post_cancel_balance_timeout_seconds) <= 0:
            record["balance_gate_done"] = True  # gate disabled; the refresh below still fires
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            updater = getattr(connector, "_update_balances", None)
            if callable(updater):
                result = updater()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            self.logger().info(
                f"{self.config.id}: post-cancel balance refresh requested for the "
                f"{side_name} side (released={record['released']})."
            )
            self._emit_structured(
                "range_ladder_post_cancel_balance_refresh",
                side=side_name,
                released=str(record["released"]),
            )
        except Exception as e:
            self.logger().debug(f"{self.config.id}: post-cancel balance refresh failed: {e}")

    def _wave_balance_gate_active(self, side_name: str) -> bool:
        """True while this side's creates should wait for the freed collateral to appear in
        the connector's CACHED available balance (or the timeout). The first placement attempt
        after a cancel wave otherwise races the balance snapshot and gets dropped/resized by
        the budget preflight (the 2026-07-07 19:18 dust-sell failure)."""
        record = self._refresh_wave.get(side_name)
        if not record or record.get("balance_gate_done") or record.get("balance_refresh_ts") is None:
            return False
        now = self.market_data_provider.time()
        if (now - record["balance_refresh_ts"]) >= float(self.config.post_cancel_balance_timeout_seconds):
            record["balance_gate_done"] = True
            self.logger().info(
                f"{self.config.id}: post-cancel balance gate ({side_name}) timed out after "
                f"{self.config.post_cancel_balance_timeout_seconds}s -- placing anyway; the "
                "reconciliation retries any residual preflight drops."
            )
            self._emit_structured(
                "range_ladder_post_cancel_balance_gate_timeout",
                side=side_name,
                timeout_s=self.config.post_cancel_balance_timeout_seconds,
            )
            return False
        base_asset, quote_asset = split_hb_trading_pair(self.config.trading_pair)
        asset = quote_asset if side_name == "buy" else base_asset
        if self._safe_get_available_balance(asset) >= record["released"]:
            record["balance_gate_done"] = True  # the freed collateral is visible -- place now
            return False
        return True

    def on_budget_preflight_result(self, action=None, result: str = "", original_amount=None,
                                   adjusted_amount=None, reason: str = "", **_kwargs):
        """Called (best-effort) by the executor orchestrator when its budget preflight drops
        or resizes one of THIS controller's create actions. Records the failure on the side's
        refresh wave so the intended-vs-live reconciliation retries on the next pass instead
        of waiting out the backoff. Never raises."""
        try:
            config = getattr(action, "executor_config", None)
            level_id = getattr(config, "level_id", None) or ""
            side = getattr(config, "side", None)
            side_name = ("buy" if side == TradeType.BUY
                         else "sell" if side == TradeType.SELL else None)
            self.logger().info(
                f"{self.config.id}: budget preflight {result} for {side_name or '?'} level "
                f"{level_id or '?'} (amount {original_amount} -> {adjusted_amount}, "
                f"reason={reason}); the ladder reconciliation will retry."
            )
            self._emit_structured(
                "range_ladder_preflight_feedback",
                side=side_name or "",
                level_id=level_id,
                result=result,
                reason=reason,
                original_amount=str(original_amount),
                adjusted_amount=str(adjusted_amount),
            )
            if side_name is None:
                return
            record = self._refresh_wave.get(side_name)
            if record is not None:
                record["preflight_drops"] = record.get("preflight_drops", 0) + 1
                record["next_retry_ts"] = 0.0  # retry on the next reconcile pass
        except Exception:
            pass

    def _wave_still_held(self, side_name: str) -> Decimal:
        """EXACT ledger-side credit: the reservations of this wave's cancelled executors that
        are STILL live/shutting down (i.e. still counted inside active_*_reserved). Shrinks to
        zero on its own as the cancels complete."""
        record = self._refresh_wave.get(side_name)
        if not record or not record["cancelled_ids"]:
            return Decimal("0")
        side = TradeType.BUY if side_name == "buy" else TradeType.SELL
        total = Decimal("0")
        for executor in self._order_executors_active_or_shutting_down():
            if executor.id in record["cancelled_ids"] and self._executor_side(executor) == side:
                total += max(Decimal("0"), self._wave_reservation_of(executor, side))
        return total

    def _reconcile_refresh_waves(self, now: float, allow_repropose: bool):
        """Wave lifecycle + intended-vs-live reconciliation (preflight-retry fix).

        Placement success is tracked PER LEVEL: a level of the intended ladder is satisfied
        once any executor for it materialized after issuance (live, shutting down, or already
        closed again -- a fill is success, never re-placed here). Levels never observed live
        were dropped somewhere (budget preflight against a stale balance snapshot, orchestrator
        deferral, ...) and are re-proposed with backoff up to reconcile_max_attempts per
        refresh generation; levels observed at dust size (below preflight_min_fill_ratio of
        intended) are stopped and re-proposed at full size. This generalizes the previous
        round's deferred-create re-propose: "1 dust order out of 9" heals exactly like
        "0 orders out of 9".

        Also fires the one-shot post-cancel balance refresh once a wave's cancels finish
        closing. Called with allow_repropose=False from update_processed_data (bookkeeping
        before budgets) and True from determine_executor_actions (may re-mark a side dirty
        and queue dust stops for the same tick's stop/create pass)."""
        for side_name, side in (("buy", TradeType.BUY), ("sell", TradeType.SELL)):
            record = self._refresh_wave.get(side_name)
            if record is None:
                continue
            if (now - record["started_ts"]) > self.REFRESH_WAVE_TTL_SECONDS:
                self.logger().warning(
                    f"{self.config.id}: refresh wave ({side_name}) expired unresolved after "
                    f"{self.REFRESH_WAVE_TTL_SECONDS:.0f}s (released={record['released']}, "
                    f"attempts={record['attempts']}). Dropping the budget credit."
                )
                self._refresh_wave[side_name] = None
                continue

            cancels_in_flight = self._wave_cancels_in_flight(side_name)
            if not cancels_in_flight:
                # Cancels confirmed/closed: force one connector balance refresh so the budget
                # preflight sees the freed collateral quickly (Kraken's poll lags cancels).
                self._maybe_request_post_cancel_balance_refresh(side_name, record, now)

            if record["issued_ts"] is None:
                continue  # cancels out, rebuild not yet issued -- the dirty flag drives it

            intended: Dict[str, Decimal] = record.get("intended") or {}
            if not intended:
                # Legacy resolution (no per-level intent recorded): any NEW live executor on
                # the side resolves the wave.
                if any(e.id not in record["cancelled_ids"]
                       for e in self._order_executors_active_or_shutting_down()
                       if self._executor_side(e) == side):
                    self._refresh_wave[side_name] = None
                continue

            # ---- classify every intended level against the executors we can observe
            ratio = max(Decimal("0"), Decimal(self.config.preflight_min_fill_ratio))
            live_statuses = (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
            missing: List[str] = []
            satisfied: List[str] = []
            for level_id, intended_amount in intended.items():
                live_executor = None
                placed_elsewhere = False
                for executor in self.executors_info:
                    if getattr(executor.config, "level_id", None) != level_id:
                        continue
                    if self._executor_side(executor) != side:
                        continue
                    if executor.id in record["cancelled_ids"]:
                        continue
                    status = getattr(executor, "status", None)
                    if status in live_statuses:
                        live_executor = executor
                        break
                    if status == RunnableStatus.SHUTTING_DOWN:
                        placed_elsewhere = True  # placed; being stopped by another path
                        continue
                    close_ts = getattr(executor, "close_timestamp", None)
                    if close_ts is not None and close_ts >= record["issued_ts"]:
                        placed_elsewhere = True  # placed then closed (fill/cancel) -- success
                if live_executor is not None:
                    live_amount = self._d(getattr(live_executor.config, "amount", "0") or "0")
                    if ratio > Decimal("0") and live_amount < intended_amount * ratio:
                        # Dust survivor (preflight resize slipped through): stop it and keep
                        # the level in the intent so it retries at full size once closed.
                        if allow_repropose and live_executor.id not in self._reconcile_stop_ids:
                            self._reconcile_stop_ids.add(live_executor.id)
                            self._record_refresh_wave_cancels(side, [live_executor], now, accumulate=True)
                            self.logger().warning(
                                f"{self.config.id}: stopping dust-sized {side_name} order at "
                                f"{level_id}: live amount {live_amount} < "
                                f"{ratio} * intended {intended_amount}. The level retries at "
                                "full size once the cancel settles."
                            )
                            self._emit_structured(
                                "range_ladder_reconcile_dust_stop",
                                side=side_name,
                                level_id=level_id,
                                live_amount=str(live_amount),
                                intended_amount=str(intended_amount),
                                min_fill_ratio=str(ratio),
                            )
                    else:
                        satisfied.append(level_id)
                elif placed_elsewhere:
                    satisfied.append(level_id)
                else:
                    missing.append(level_id)
            for level_id in satisfied:
                intended.pop(level_id, None)
            if not intended:
                # Every intended level materialized (or was consumed) -- wave resolved.
                self._refresh_wave[side_name] = None
                continue

            if not allow_repropose:
                continue
            dirty = self._buy_side_dirty if side_name == "buy" else self._sell_side_dirty
            if dirty:
                continue  # a newer trigger owns this side; it supersedes the pending intent
            if self._wave_cancels_in_flight(side_name):
                continue  # our own dust stops (or the wave's cancels) are still closing
            if now < record["next_retry_ts"]:
                continue  # backoff between heal attempts
            if record["attempts"] >= int(self.config.reconcile_max_attempts):
                if not record["cap_warned"]:
                    record["cap_warned"] = True
                    self.logger().warning(
                        f"{self.config.id}: intended-vs-live reconciliation for the "
                        f"{side_name} side reached the retry cap "
                        f"({self.config.reconcile_max_attempts} attempts) with "
                        f"{len(intended)} level(s) still unplaced {sorted(intended)} "
                        f"(preflight drops seen: {record['preflight_drops']}). MANUAL "
                        "ATTENTION NEEDED -- retries stop until a newer refresh supersedes "
                        f"(or the wave expires after {self.REFRESH_WAVE_TTL_SECONDS:.0f}s)."
                    )
                    self._emit_structured(
                        "range_ladder_reconcile_retry_cap",
                        side=side_name,
                        attempts=record["attempts"],
                        missing_levels=sorted(intended),
                        preflight_drops=record["preflight_drops"],
                    )
                continue
            plan = self._plan_buy_book() if side == TradeType.BUY else self._plan_sell_book()
            if not plan:
                self._refresh_wave[side_name] = None
                continue  # nothing fundable/eligible anymore (regime/budget moved on)

            record["attempts"] += 1
            record["next_retry_ts"] = now + float(self.config.reconcile_retry_seconds)
            age = now - record["issued_ts"]
            self._mark_side_dirty(side_name, "ladder_reconcile")
            self.logger().info(
                f"{self.config.id}: re-proposing {len(missing)} missing {side_name} level(s) "
                f"{sorted(missing)} (deferral age {age:.1f}s, attempt {record['attempts']}/"
                f"{self.config.reconcile_max_attempts}, preflight_drops="
                f"{record['preflight_drops']})."
            )
            self._emit_structured(
                "range_ladder_intended_vs_live_heal",
                side=side_name,
                missing_levels=sorted(missing),
                attempt=record["attempts"],
                deferral_age_s=round(age, 3),
                planned_levels=len(plan),
                preflight_drops=record["preflight_drops"],
            )
            # Backward-compatible event: when the WHOLE issuance vanished (no live level at
            # all on this side), this is exactly the previous round's lost-creates case.
            side_has_any_live = any(
                self._executor_side(e) == side
                for e in self._order_executors_active_or_shutting_down()
            )
            if not side_has_any_live:
                self._emit_structured(
                    "range_ladder_deferred_creates_reproposed",
                    side=side_name,
                    deferred_count=record["issued_count"],
                    deferral_age_s=round(age, 3),
                    attempt=record["attempts"],
                    planned_levels=len(plan),
                )

    def _run_empty_side_watchdog(self, now: float):
        """Zero-level deadlock watchdog (fix 3). A side with NO live/shutting-down order
        executors whose effective budget can fund a rebuild (its plan is non-empty) is forced
        dirty after empty_side_watchdog_seconds, so an empty ladder can never sit idle until
        the 12h global timer (the 2026-07-07 deadlock). An armed per-side cooldown is
        respected -- its lapse re-centers the side anyway, and cooldowns only arm on fills,
        so an empty side's cooldown can never re-arm (respected at most once)."""
        if not self.config.event_refresh_enabled:
            return  # legacy mode re-places from the free budget every cycle by design
        if self._market_data_hard_pause or self._session_expired:
            self._side_empty_since = {"buy": None, "sell": None}
            return
        if not self.processed_data or not self.processed_data.get("initialization_ready", True) \
                or not self.processed_data.get("market_data_ready", True):
            return
        if now < self._refresh_quiet_until:
            return  # post-refresh settle window: creates are deliberately paused
        for side_name, side, dirty, cooldown_armed in (
            ("buy", TradeType.BUY, self._buy_side_dirty, self._buy_cooldown_armed),
            ("sell", TradeType.SELL, self._sell_side_dirty, self._sell_cooldown_armed),
        ):
            side_executors = [e for e in self._order_executors_active_or_shutting_down()
                              if self._executor_side(e) == side]
            if side_executors or dirty:
                self._side_empty_since[side_name] = None
                continue
            if self._refresh_wave.get(side_name) is not None:
                # An unresolved refresh wave owns this side: the intended-vs-live
                # reconciliation is retrying (or has capped out and warned). The watchdog
                # yielding here keeps the retry cap meaningful -- no order spam after it.
                self._side_empty_since[side_name] = None
                continue
            plan = self._plan_buy_book() if side == TradeType.BUY else self._plan_sell_book()
            if not plan:
                self._side_empty_since[side_name] = None
                continue
            if self._side_empty_since[side_name] is None:
                self._side_empty_since[side_name] = now
                continue
            empty_for = now - self._side_empty_since[side_name]
            if empty_for < float(self.config.empty_side_watchdog_seconds):
                continue
            if cooldown_armed:
                continue  # respected at most once: lapse re-centers this side by itself
            free_buy = self.processed_data.get("free_buy_budget_quote", Decimal("0"))
            free_sell = self.processed_data.get("free_sell_budget_base", Decimal("0"))
            self._mark_side_dirty(side_name, "empty_side_watchdog")
            self._side_empty_since[side_name] = now  # re-arm; no re-fire spam next cycle
            self.logger().warning(
                f"{self.config.id}: empty-side watchdog fired for {side_name}: the side sat "
                f"empty for {empty_for:.1f}s with a fundable plan ({len(plan)} level(s)) and "
                f"no pending trigger. free_buy_budget_quote={free_buy} "
                f"free_sell_budget_base={free_sell}. Forcing a re-place."
            )
            self._emit_structured(
                "range_ladder_empty_side_watchdog_fired",
                side=side_name,
                empty_for_s=round(empty_for, 3),
                planned_levels=len(plan),
                free_buy_budget_quote=str(free_buy),
                free_sell_budget_base=str(free_sell),
                watchdog_seconds=self.config.empty_side_watchdog_seconds,
            )

    def _performance_snapshot(self) -> Dict[str, Decimal]:
        realized = Decimal("0")
        unrealized = Decimal("0")
        fees = Decimal("0")
        base_amount = Decimal("0")
        abs_notional_quote = Decimal("0")

        for position in self.positions_held or []:
            try:
                realized += Decimal(position.realized_pnl_quote)
                unrealized += Decimal(position.unrealized_pnl_quote)
                fees += Decimal(position.cum_fees_quote)
                qty = Decimal(position.amount)
                px = Decimal(position.breakeven_price)
                if position.side == TradeType.BUY:
                    base_amount += qty
                else:
                    base_amount -= qty
                abs_notional_quote += qty * px
            except Exception:
                continue

        global_pnl = realized + unrealized - fees
        return {
            "inventory_realized_pnl_quote": realized,
            "inventory_unrealized_pnl_quote": unrealized,
            "inventory_cum_fees_quote": fees,
            "inventory_global_pnl_quote": global_pnl,
            "inventory_net_base_amount": base_amount,
            "inventory_abs_notional_quote": abs_notional_quote,
            # Deprecated alias kept for backward compatibility with any downstream consumers.
            "inventory_gross_base_value_quote": abs_notional_quote,
        }


    def _seed_value_quote(self) -> Decimal:
        """Realized seed value captured at first init (deploy-ceiling floor).

        Falls back to the initial managed-quote + initial-claimed-base value for
        states written before seed_value_quote was persisted.
        """
        if self._state.get("seed_value_quote") is not None:
            return max(Decimal("0"), self._d(self._state.get("seed_value_quote"), "0"))
        initial_managed = self._d(self._state.get("initial_managed_quote"), "0")
        initial_base = self._d(self._state.get("initial_claimed_base_amount"), "0")
        initial_ref = self._d(self._state.get("initial_reference_price"), "0")
        return max(Decimal("0"), initial_managed + initial_base * initial_ref)

    def _compute_deploy_ceiling(self, seed_value_quote: Decimal, managed_fund_value_quote: Decimal) -> Decimal:
        """Ceiling on total deployed fund value.

        Starts at the realized seed value and ratchets up with the fills-only managed
        fund value as the strategy compounds earned profit, hard-capped at
        max_fund_value_quote. It never shrinks deployment below what the fund is
        actually worth (max(seed, managed)) and never exceeds the configured cap.
        """
        floor = max(Decimal("0"), seed_value_quote)
        managed = max(Decimal("0"), managed_fund_value_quote)
        cap = Decimal(self.config.max_fund_value_quote)
        return min(cap, max(floor, managed))

    def _compute_deploy_budgets(
        self,
        *,
        reference_price: Decimal,
        available_quote: Decimal,
        available_base: Decimal,
        active_buy_reserved_quote: Decimal,
        active_sell_reserved_base: Decimal,
        deploy_ceiling: Decimal,
        owned_quote: Optional[Decimal] = None,
        owned_base: Optional[Decimal] = None,
        held_quote: Decimal = Decimal("0"),
        held_base: Decimal = Decimal("0"),
        balance_settling: bool = False,
        record_diagnostics: bool = True,
    ):
        """Size each side's deployable budget, bounded by the deploy ceiling.

        Budget SOURCE (ledger_funded_budgets=True, default): THIS controller's OWN managed-fund
        ledger -- the un-reserved owned quote/base (owned minus its own resting reservations) --
        bounded by the live wallet as a SAFETY FLOOR. This lets many controllers share one
        exchange account with no contention (each spends only what it owns) and lets a controller
        redeploy its own proceeds with no quota change, while never trying to place orders for
        funds not physically free in the wallet. A deposit raises only the floor, never owned, so
        deposit-exclusion is preserved (the budget is capped at owned_*_free).

        Budget SOURCE (ledger_funded_budgets=False, legacy): the raw wallet available balance,
        quota-capped -- byte-for-byte the prior behavior.

        `shared_account_quote_quota`, when set, is an ADDITIONAL hard cap on the buy budget in
        BOTH modes. No fixed base/quote ratio after seed; the COMBINED new deployed value plus
        what is already on the book is throttled (pro-rata) so total deployed never exceeds
        deploy_ceiling.

        Returns (free_buy_budget_quote, free_sell_budget_base, throttle_scale, headroom).
        """
        ref = max(Decimal("0"), reference_price)
        avail_quote = max(Decimal("0"), available_quote)
        avail_base = max(Decimal("0"), available_base)

        # Ledger funding needs the owned figures; without them (legacy or a bare unit-test call)
        # fall back to the wallet-funded path so behavior is unchanged.
        use_ledger = (
            bool(self.config.ledger_funded_budgets)
            and owned_quote is not None
            and owned_base is not None
        )

        if use_ledger:
            owned_quote_free = max(Decimal("0"), Decimal(owned_quote) - max(Decimal("0"), active_buy_reserved_quote))
            owned_base_free = max(Decimal("0"), Decimal(owned_base) - max(Decimal("0"), active_sell_reserved_base))
            # Wallet floor: never size above what is physically free in the wallet.
            buy_budget_quote = min(owned_quote_free, avail_quote)
            sell_budget_base = min(owned_base_free, avail_base)
            # Warn only on a GENUINE shortfall: the ledger owns more than the wallet TOTAL
            # (available + held) can back. Inventory locked in our own resting orders shows up as
            # `held`, not as missing, so it must not trip the warning; and the warning is deferred
            # while balances are settling (a post-fill/post-reconnect race can make the cached wallet
            # transiently stale-low). The budget clamp `min(owned_free, available)` above is unchanged.
            if record_diagnostics:
                self._note_wallet_floor("buy", Decimal(owned_quote), avail_quote, held_quote,
                                        buy_budget_quote, balance_settling)
                self._note_wallet_floor("sell", Decimal(owned_base), avail_base, held_base,
                                        sell_budget_base, balance_settling)
        else:
            # Legacy: raw wallet available (byte-for-byte the prior behavior).
            buy_budget_quote = avail_quote
            sell_budget_base = avail_base

        # shared_account_quote_quota: an additional hard cap on the BUY budget in BOTH modes.
        if self.config.shared_account_quote_quota is not None:
            buy_budget_quote = min(
                buy_budget_quote, max(Decimal("0"), Decimal(self.config.shared_account_quote_quota))
            )

        # Buy-side fee headroom: the exchange holds notional + fee as collateral for a resting
        # buy (NonKYC computes hold_amount = notional + fee), so deploying 100% of the budget
        # makes the cumulative hold exceed available quote by the sum of fees and the last
        # rung(s) reject with insufficient funds. Dividing by (1 + fee_rate) sizes the total
        # buy notional so notional + fee fits the budget exactly. Applies in BOTH funding
        # modes. The sell side is NOT haircut: sell fees are deducted from proceeds, never
        # held as extra collateral.
        fee_rate = max(Decimal("0"), Decimal(self.config.fee_rate))
        buy_fee_headroom_quote = Decimal("0")
        if fee_rate > Decimal("0") and buy_budget_quote > Decimal("0"):
            pre_haircut_budget = buy_budget_quote
            buy_budget_quote = buy_budget_quote / (Decimal("1") + fee_rate)
            buy_fee_headroom_quote = pre_haircut_budget - buy_budget_quote
        if record_diagnostics:
            self._last_buy_fee_headroom_quote = buy_fee_headroom_quote

        active_reserved_value = (
            max(Decimal("0"), active_buy_reserved_quote)
            + max(Decimal("0"), active_sell_reserved_base) * ref
        )
        headroom = max(Decimal("0"), deploy_ceiling - active_reserved_value)
        desired_new_value = buy_budget_quote + sell_budget_base * ref

        throttle_scale = Decimal("1")
        if desired_new_value > headroom and desired_new_value > Decimal("0"):
            throttle_scale = headroom / desired_new_value
            buy_budget_quote = buy_budget_quote * throttle_scale
            sell_budget_base = sell_budget_base * throttle_scale

        return buy_budget_quote, sell_budget_base, throttle_scale, headroom

    def _is_balance_settling(self) -> bool:
        """True while the connector reports a post-fill / post-reconnect REST balance sync in flight.
        Strict identity check (``is True``): a connector without the attribute reports False via the
        default, and an auto-truthy MagicMock attribute (in tests) is NOT mistaken for settling."""
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            return getattr(connector, "is_balance_settling", False) is True
        except Exception:
            return False

    def _within_fill_settle_grace(self, now: float) -> bool:
        """True within fill_settle_grace_seconds of the last BOOKED fill. Connectors without a
        settling flag (e.g. Kraken, poll-only balances) leave the wallet snapshot one balance
        poll behind a fill; the ledger books instantly, so the gap reads as a false over-claim
        until the wallet re-syncs. Deferring the over-claim checks through this window kills
        that false positive without touching the (conservative) budget clamp."""
        if self._last_fill_booked_ts is None:
            return False
        return (now - self._last_fill_booked_ts) < self.config.fill_settle_grace_seconds

    def _note_wallet_floor(self, side: str, owned: Decimal, available: Decimal, held: Decimal,
                           clamped_budget: Decimal, settling: bool = False):
        """Transition-based diagnostic: warn the FIRST cycle a GENUINE wallet-floor shortfall appears
        on a side, then stay silent until it clears -- no per-cycle spam.

        A shortfall is genuine only when the ledger owns MORE than the wallet TOTAL can back, i.e.
        owned > available + held. Inventory locked in our own resting orders is `held` (not missing),
        so it never trips this; and the check is deferred while balances are settling (a post-fill /
        post-reconnect race can make the cached wallet transiently stale-low). This is purely
        diagnostic -- the budget clamp `min(owned_free, available)` is applied separately and unchanged.
        """
        attr = "_buy_wallet_floor_bound" if side == "buy" else "_sell_wallet_floor_bound"
        total = max(Decimal("0"), available) + max(Decimal("0"), held)
        # Small relative+absolute tolerance so Decimal noise never raises a false alarm.
        tolerance = total * Decimal("0.005") + Decimal("1e-12")
        binds = (not settling) and (owned > total + tolerance)
        was_binding = getattr(self, attr, False)
        if binds and not was_binding:
            self.logger().warning(
                f"{self.config.id}: wallet floor binding on {side} side -- the managed-fund ledger "
                f"owns more ({owned}) than the wallet TOTAL can back (available {available} + held "
                f"{held} = {total}); clamping the {side} budget to {clamped_budget}. Likely an "
                "unsettled deposit, ledger/wallet drift, or an external spend."
            )
            self._emit_structured(
                "range_ladder_wallet_floor_binding",
                side=side,
                owned=str(owned),
                available=str(available),
                held=str(held),
                total=str(total),
                clamped_budget=str(clamped_budget),
                reason="ledger_owned_exceeds_wallet_total",
            )
        # Only update the latch when NOT settling, so a genuine shortfall that first appears during
        # settling still warns on the first post-settle cycle.
        if not settling:
            setattr(self, attr, binds)

    async def update_processed_data(self):
        now = self.market_data_provider.time()
        base_asset, quote_asset = split_hb_trading_pair(self.config.trading_pair)

        try:
            reference_price = self._safe_market_price(PriceType.MidPrice, "reference_price")
            best_bid = self._safe_market_price(PriceType.BestBid, "best_bid")
            best_ask = self._safe_market_price(PriceType.BestAsk, "best_ask")
        except ValueError as e:
            error_message = str(e)
            if self._market_data_unavailable_since is None:
                self._market_data_unavailable_since = now
            self._handle_live_runtime_settings_update()
            unavailable_for = now - self._market_data_unavailable_since
            if self._market_data_hard_pause and unavailable_for < self.config.max_market_data_unavailable_seconds:
                self._market_data_hard_pause = False
                self.logger().warning(
                    f"{self.config.id}: clearing market-data hard pause because the hot-updated threshold "
                    f"({self.config.max_market_data_unavailable_seconds}s) now exceeds the current outage duration "
                    f"({unavailable_for:.1f}s)."
                )
                self._emit_structured(
                    "range_ladder_market_data_hard_pause_cleared",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    reason="threshold_extended",
                    unavailable_for_s=round(unavailable_for, 3),
                    threshold_s=self.config.max_market_data_unavailable_seconds,
                )
            if self._last_market_data_error != error_message:
                self._last_market_data_error = error_message
                self.logger().warning(
                    f"{self.config.id}: market data unavailable or non-finite, pausing order creation: {error_message}"
                )
                self._emit_structured(
                    "range_ladder_market_data_unavailable",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    error=error_message,
                    unavailable_for_s=round(unavailable_for, 3),
                )
            if (
                unavailable_for >= self.config.max_market_data_unavailable_seconds
                and not self._market_data_hard_pause
            ):
                self._market_data_hard_pause = True
                self.logger().warning(
                    f"{self.config.id}: market data has been unavailable for {unavailable_for:.1f}s. "
                    "Entering hard-pause mode and winding down active ladder orders."
                )
                self._emit_structured(
                    "range_ladder_market_data_hard_pause",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    unavailable_for_s=round(unavailable_for, 3),
                    threshold_s=self.config.max_market_data_unavailable_seconds,
                )
            self._set_unavailable_processed_data(
                base_asset=base_asset,
                quote_asset=quote_asset,
                reference_price=Decimal("0"),
                best_bid=Decimal("0"),
                best_ask=Decimal("0"),
                market_data_ready=False,
                market_data_error=error_message,
                initialization_ready=False,
                initialization_blocked_reason="market_data_unavailable",
            )
            self._emit_diagnostic_heartbeat_if_due()
            return

        if self._market_data_unavailable_since is not None:
            recovered_after = now - self._market_data_unavailable_since
            self._emit_structured(
                "range_ladder_market_data_recovered",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                recovered_after_s=round(recovered_after, 3),
                hard_pause_cleared=self._market_data_hard_pause,
            )
            self._market_data_unavailable_since = None
            self._last_market_data_error = None
            if self._market_data_hard_pause:
                self.logger().warning(
                    f"{self.config.id}: market data recovered after hard pause. "
                    "Fresh ladder creation will resume once active orders are clear."
                )
                self._market_data_hard_pause = False

        self._last_market_data_error = None

        initialized = self._ensure_initialized(reference_price)
        self._cleanup_cooldown_bypass()

        if not initialized:
            self._set_unavailable_processed_data(
                base_asset=base_asset,
                quote_asset=quote_asset,
                reference_price=reference_price,
                best_bid=best_bid,
                best_ask=best_ask,
                market_data_ready=True,
                initialization_ready=False,
                initialization_blocked_reason=self._initialization_blocked_reason or "",
            )
            self._emit_diagnostic_heartbeat_if_due()
            return

        # v13: fill booking moved DOWN to after the balance reads (so the re-seed and the
        # booked owned_* both feed managed_* the same cycle). See the booking call below.

        if self._session_started_ts is None:
            self._session_started_ts = now
            self._emit_structured(
                "range_ladder_session_started",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                max_session_duration_hours=self.config.max_session_duration_hours,
            )

        session_elapsed_s = max(0.0, now - self._session_started_ts)
        self._handle_live_runtime_settings_update()
        session_limit_s = float(self.config.max_session_duration_hours * Decimal("3600"))
        if session_elapsed_s >= session_limit_s:
            if not self._session_expired or self._session_expired_reason != "max_session_duration_reached":
                self._session_expired = True
                self._session_expired_reason = "max_session_duration_reached"
                self._session_expired_logged = False
            if not self._session_expired_logged:
                self._session_expired_logged = True
                self.logger().warning(
                    f"{self.config.id}: maximum session duration reached ({self.config.max_session_duration_hours}h). "
                    "Controller will stop creating new ladder orders and wind down active orders."
                )
                self._emit_structured(
                    "range_ladder_session_expired",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    session_elapsed_s=round(session_elapsed_s, 3),
                    max_session_duration_hours=self.config.max_session_duration_hours,
                )
        elif self._session_expired and self._session_expired_reason == "max_session_duration_reached":
            self._session_expired = False
            self._session_expired_reason = ""
            self._session_expired_logged = False
            self.logger().warning(
                f"{self.config.id}: clearing session-expired state because the hot-updated maximum session duration "
                f"({self.config.max_session_duration_hours}h) now exceeds the current session age "
                f"({session_elapsed_s / 3600:.3f}h)."
            )
            self._emit_structured(
                "range_ladder_session_expiry_cleared",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                session_elapsed_s=round(session_elapsed_s, 3),
                max_session_duration_hours=self.config.max_session_duration_hours,
                reason="threshold_extended",
            )

        current_runtime_signature = self._runtime_config_signature()
        if self._applied_runtime_config_signature is None:
            self._applied_runtime_config_signature = current_runtime_signature
        elif current_runtime_signature != self._applied_runtime_config_signature:
            if self._pending_runtime_config_signature != current_runtime_signature:
                self._pending_runtime_config_signature = current_runtime_signature
                self._config_rebuild_pending = True
                self._config_rebuild_reason = "runtime_config_changed"
                self.logger().warning(
                    f"{self.config.id}: detected hot-updated ladder configuration. "
                    "Active ladder orders will be cancelled and rebuilt immediately."
                )
                self._emit_structured(
                    "range_ladder_runtime_config_changed",
                    connector=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    reason=self._config_rebuild_reason,
                )

        total_quote_balance = self._safe_get_balance(quote_asset)
        total_base_balance = self._safe_get_balance(base_asset)
        available_quote_balance = self._safe_get_available_balance(quote_asset)
        available_base_balance = self._safe_get_available_balance(base_asset)

        # v13 Part C: guarded one-shot managed-fund re-seed from the CURRENT wallet (idempotent
        # per token). Runs before booking so the freshly seeded owned_* feed managed_* this cycle.
        try:
            self._maybe_reseed_fund(reference_price)
        except Exception as e:
            self.logger().exception(f"{self.config.id}: fund re-seed failed")
            self._emit_structured("range_ladder_ledger_update_error", error=str(e))

        # v13 Part A: book fills from our OWN orders' cumulative executed amounts. Runs AFTER the
        # balance reads and BEFORE owned_* is loaded for managed_* below, so the ceiling and the
        # re-anchor see freshly booked values THIS cycle. It mutates and persists owned_*.
        try:
            self._book_fills_from_orders()
        except Exception as e:
            self.logger().exception(f"{self.config.id}: fill booking failed")
            self._emit_structured("range_ladder_ledger_update_error", error=str(e))

        if self.config.event_refresh_enabled:
            # Per-side cooldown + immediate cross-side refresh: a booked fill on one side dirties
            # the OPPOSITE side now and arms its own cooldown; cooldown lapse re-centers the same
            # side once; executor_refresh_time refreshes both. Booking already set the per-cycle
            # fill flags above; this only sets the dirty intents (applied in the action proposals).
            self._evaluate_refresh_triggers(now)
        else:
            # LEGACY (event_refresh_enabled=False): v12/v13 directional recycle windows. A booked
            # fill on one side (a sell fill brought quote in -> deploy to BUYS; a buy fill brought
            # base in -> deploy to SELLS) opens a short cooldown-bypass window on the opposite side.
            # A pure one-sided total change (a deposit/withdrawal) is NOT an order and opens NO
            # window. As a backstop, a TWO-SIDED wallet move with a genuine trade signature may also
            # open a window. Window duration stays recycle_max_latency_seconds.
            base_increase_threshold = (
                self.config.min_order_quote / reference_price if reference_price > Decimal("0") else Decimal("0")
            )
            open_buy_window = self._booked_sell_fill_this_cycle
            open_sell_window = self._booked_buy_fill_this_cycle
            if (
                self._prev_total_quote_balance is not None
                and self._prev_total_base_balance is not None
                and base_increase_threshold > Decimal("0")
            ):
                quote_delta = total_quote_balance - self._prev_total_quote_balance
                base_delta = total_base_balance - self._prev_total_base_balance
                # genuine SELL signature: quote up AND base down -> fund buys
                if quote_delta >= self.config.min_order_quote and base_delta <= -base_increase_threshold:
                    open_buy_window = True
                # genuine BUY signature: base up AND quote down -> fund sells
                if base_delta >= base_increase_threshold and quote_delta <= -self.config.min_order_quote:
                    open_sell_window = True
            if open_buy_window:
                self._recycle_bypass_buy_until = now + self.config.recycle_max_latency_seconds
                self._emit_structured(
                    "range_ladder_recycle_window_opened",
                    side="buy",
                    trigger=("booked_fill" if self._booked_sell_fill_this_cycle else "wallet_two_sided"),
                    window_s=self.config.recycle_max_latency_seconds,
                )
            if open_sell_window:
                self._recycle_bypass_sell_until = now + self.config.recycle_max_latency_seconds
                self._emit_structured(
                    "range_ladder_recycle_window_opened",
                    side="sell",
                    trigger=("booked_fill" if self._booked_buy_fill_this_cycle else "wallet_two_sided"),
                    window_s=self.config.recycle_max_latency_seconds,
                )

        reserve_quote_balance = self._d(self._state.get("reserve_quote_balance"))
        reserve_base_balance = self._d(self._state.get("reserve_base_balance"))

        # Use controller-owned ledger instead of wallet-derived totals (freshly booked above)
        owned_quote = self._d(self._state.get("owned_quote"), "0")
        owned_base = self._d(self._state.get("owned_base"), "0")

        # v14: defer all ledger/wallet over-claim reconciliation (re-anchor + warnings) while the
        # connector's balances are still settling. Right after a fill books or a WS reconnect the
        # cached wallet can be transiently stale-low, which would falsely read as an over-claim.
        # v15: connectors without a settling flag (poll-only balances, e.g. Kraken) get the same
        # protection from a time-based grace window after each booked fill.
        balance_settling = self._is_balance_settling() or self._within_fill_settle_grace(now)

        # v12 Issue 1: self-heal a persistently over-claimed ledger by re-anchoring DOWN to
        # wallet truth. Runs right after owned_* are loaded and BEFORE managed_* are computed
        # so the corrected values flow into the deploy ceiling and all reporting THIS cycle.
        # Only ever correct downward (the over-claimed side); a wallet that exceeds the ledger
        # is legitimate under the self-balance model (idle reserve, deposits, un-booked
        # proceeds) and stays a no-op. The booking loop's downward math is untouched -- this
        # is the safety net for fills that loop misses.
        reanchor_wallet_derived_quote = max(Decimal("0"), total_quote_balance - reserve_quote_balance)
        reanchor_wallet_derived_base = max(Decimal("0"), total_base_balance - reserve_base_balance)
        reanchor_quote_overclaim = max(Decimal("0"), owned_quote - total_quote_balance)
        reanchor_base_overclaim = max(Decimal("0"), owned_base - total_base_balance)
        reanchor_overclaim_quote = reanchor_quote_overclaim + reanchor_base_overclaim * reference_price
        if balance_settling:
            # Defer the over-claim self-heal while balances are settling -- do not touch the grace
            # timer; it resumes next cycle once the wallet has synced.
            pass
        elif reanchor_overclaim_quote > self.config.ledger_reconcile_threshold_quote:
            if self._overclaim_since is None:
                self._overclaim_since = now
            elif (now - self._overclaim_since) >= self.config.ledger_overclaim_reanchor_seconds:
                new_owned_quote = max(Decimal("0"), min(owned_quote, reanchor_wallet_derived_quote))
                new_owned_base = max(Decimal("0"), min(owned_base, reanchor_wallet_derived_base))
                if new_owned_quote < owned_quote or new_owned_base < owned_base:
                    self.logger().warning(
                        f"{self.config.id}: re-anchoring over-claimed ledger to wallet. "
                        f"owned_quote {owned_quote}->{new_owned_quote} owned_base {owned_base}->{new_owned_base}"
                    )
                    self._emit_structured(
                        "range_ladder_ledger_reanchored",
                        old_owned_quote=str(owned_quote), new_owned_quote=str(new_owned_quote),
                        old_owned_base=str(owned_base), new_owned_base=str(new_owned_base),
                        wallet_derived_quote=str(reanchor_wallet_derived_quote),
                        wallet_derived_base=str(reanchor_wallet_derived_base),
                        overclaim_quote=str(reanchor_overclaim_quote),
                        grace_seconds=self.config.ledger_overclaim_reanchor_seconds,
                    )
                    owned_quote, owned_base = new_owned_quote, new_owned_base
                    self._state["owned_quote"] = str(owned_quote)
                    self._state["owned_base"] = str(owned_base)
                    self._save_state()
                self._overclaim_since = None
        else:
            self._overclaim_since = None

        managed_quote_total = max(Decimal("0"), owned_quote)
        managed_base_total = max(Decimal("0"), owned_base)
        managed_fund_value_quote = managed_quote_total + managed_base_total * reference_price

        # Reconciliation alert (self-balance model):
        # Under the self-balance model, deployment INTENTIONALLY tracks the live wallet,
        # so the wallet legitimately exceeds the fills-only ledger (idle reserve, later
        # deposits, un-booked fill proceeds). That positive surplus is EXPECTED and must
        # NOT spam warnings. The only genuine accounting fault is the inverse: the ledger
        # believing it owns MORE than the wallet physically holds (an over-claim), which
        # would cause the controller to size orders against money that isn't there.
        # v12 Issue 1: single threshold source -- the same config value gates the re-anchor
        # above and this warning. Once re-anchored, owned_* <= wallet truth so the over-claim
        # below computes to zero and this warning falls silent on its own.
        RECONCILIATION_ALERT_THRESHOLD_QUOTE = self.config.ledger_reconcile_threshold_quote
        wallet_derived_quote = max(Decimal("0"), total_quote_balance - reserve_quote_balance)
        wallet_derived_base = max(Decimal("0"), total_base_balance - reserve_base_balance)
        ledger_quote_drift = wallet_derived_quote - owned_quote
        ledger_base_drift = wallet_derived_base - owned_base
        # Over-claim: ledger value beyond what the TOTAL wallet can back.
        quote_overclaim = max(Decimal("0"), owned_quote - total_quote_balance)
        base_overclaim = max(Decimal("0"), owned_base - total_base_balance)
        ledger_overclaim_quote = quote_overclaim + base_overclaim * reference_price
        overclaim_above_threshold = ledger_overclaim_quote > RECONCILIATION_ALERT_THRESHOLD_QUOTE
        now_ts = self.market_data_provider.time()
        should_warn = False
        # Defer the over-claim warning while balances settle (a post-fill/reconnect race can read as a
        # false over-claim). The latch is not set during settling, so a GENUINE persistent over-claim
        # still warns on the first post-settle cycle (test 6 -> defer, test 7 -> fires after RESOLVED).
        if overclaim_above_threshold and not balance_settling:
            if not self._last_drift_above_threshold:
                should_warn = True
            elif (now_ts - self._last_drift_warning_time) >= self._drift_warning_interval:
                should_warn = True
        self._last_drift_above_threshold = overclaim_above_threshold and not balance_settling
        if should_warn:
            self._last_drift_warning_time = now_ts
            self.logger().warning(
                f"{self.config.id}: ledger over-claim detected — the fills-only ledger believes it "
                f"holds more than the wallet physically contains. "
                f"owned_quote={owned_quote} total_quote={total_quote_balance} "
                f"owned_base={owned_base} total_base={total_base_balance} "
                f"overclaim_quote={ledger_overclaim_quote}"
            )
            self._emit_structured(
                "range_ladder_reconciliation_overclaim",
                owned_quote=str(owned_quote),
                total_quote_balance=str(total_quote_balance),
                wallet_derived_quote=str(wallet_derived_quote),
                drift_quote=str(ledger_quote_drift),
                owned_base=str(owned_base),
                total_base_balance=str(total_base_balance),
                wallet_derived_base=str(wallet_derived_base),
                drift_base=str(ledger_base_drift),
                overclaim_quote=str(ledger_overclaim_quote),
            )

        # Ledger-understatement diagnostic (the INVERSE of the over-claim): a missed final
        # SELL fill understates owned_quote, and the v12 re-anchor is deliberately downward-
        # only, so understated proceeds strand outside the fund silently. This surfaces a
        # SUSTAINED wallet-over-ledger surplus for visibility. DIAGNOSTIC ONLY -- the ledger
        # is never auto-corrected upward: a legitimate idle reserve or deposit also produces
        # surplus, so this is a visibility aid, not an error.
        ledger_surplus_quote = (
            max(Decimal("0"), wallet_derived_quote - owned_quote)
            + max(Decimal("0"), wallet_derived_base - owned_base) * reference_price
        )
        if balance_settling:
            # Defer while balances settle -- the persistence timer is neither advanced nor
            # reset, so a genuine surplus first seen during settling resumes counting on the
            # first post-settle cycle.
            pass
        elif ledger_surplus_quote > RECONCILIATION_ALERT_THRESHOLD_QUOTE:
            if self._understatement_since is None:
                self._understatement_since = now_ts
            elif (
                (now_ts - self._understatement_since) >= self.LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS
                and (now_ts - self._last_understatement_warning_time) >= self._drift_warning_interval
            ):
                self._last_understatement_warning_time = now_ts
                self.logger().warning(
                    f"{self.config.id}: possible ledger understatement — the wallet has held "
                    f"{ledger_surplus_quote} quote more than the fills-only ledger owns for over "
                    f"{self.LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS:.0f}s. "
                    f"owned_quote={owned_quote} wallet_derived_quote={wallet_derived_quote} "
                    f"owned_base={owned_base} wallet_derived_base={wallet_derived_base}. "
                    "This is expected if you hold reserve/deposits; investigate only if this "
                    "grew after fills (a missed fill leaves proceeds stranded outside the fund)."
                )
                self._emit_structured(
                    "range_ladder_ledger_understatement_suspected",
                    owned_quote=str(owned_quote),
                    owned_base=str(owned_base),
                    wallet_derived_quote=str(wallet_derived_quote),
                    wallet_derived_base=str(wallet_derived_base),
                    surplus_quote=str(ledger_surplus_quote),
                    threshold_quote=str(RECONCILIATION_ALERT_THRESHOLD_QUOTE),
                    persistence_seconds=self.LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS,
                )
        else:
            self._understatement_since = None

        self._cycles_seen += 1

        # Refresh-wave bookkeeping (TTL + resolution only; re-propose runs in
        # determine_executor_actions where it may re-mark a side dirty in time for the
        # stop/create pass of the same tick).
        self._reconcile_refresh_waves(now, allow_repropose=False)

        active_buy_reserved_quote = self._active_reserved_quote_for_buys()
        active_sell_reserved_base = self._active_reserved_base_for_sells()

        # Self-balance deployment model:
        #   - The fund ledger (owned_quote/owned_base) is fills-only and invariant to deposits
        #     (a transfer is not an order, so v13 booking never touches it). It grows with
        #     realized success and shrinks on withdrawal of fund money (via the v12 re-anchor).
        #   - Deployment each cycle sizes from the LIVE available wallet balance,
        #     bounded by a growth ceiling that starts at the realized seed value and
        #     compounds with the managed fund value, hard-capped at max_fund_value_quote.
        #   - No fixed base/quote ratio after seed: each side deploys what it holds;
        #     the combined deployed value is throttled to the ceiling.
        # This makes idle reserve and fill proceeds usable on the next cycle without ever
        # deleting the state file. (Deposits are deliberately NOT pulled into trading -- to
        # change the managed baseline, re-seed via reseed_fund_from_wallet_once or re-init.)
        #
        # v13 NOTE (realized vs. unrealized): managed_fund_value_quote marks held base at the
        # CURRENT reference_price, so the ceiling moves with UNREALIZED price action on held
        # inventory as well as with realized round-trip profit. This existing market-value
        # behavior is intentionally preserved; switch to realized-only PnL only if explicitly
        # asked. (Surfaced here so the operator can decide later.)
        seed_value_quote = self._seed_value_quote()
        deploy_ceiling = self._compute_deploy_ceiling(seed_value_quote, managed_fund_value_quote)

        # Wallet inventory locked in resting orders ("held") backs the ledger just like available cash,
        # so the wallet-floor warning must compare owned against available + held (not available alone).
        held_quote = max(Decimal("0"), total_quote_balance - available_quote_balance)
        held_base = max(Decimal("0"), total_base_balance - available_base_balance)

        # Refresh-wave budget credits (fix 1). Ledger side: the EXACT reservations of the
        # wave's cancelled executors still live/shutting down (they inflate active_*_reserved
        # until the cancel completes -- subtract them so owned_*_free sees the release
        # immediately). Wallet side: the CACHED available lags the exchange by up to a balance
        # poll after a cancel releases collateral -- credit up to the recorded release against
        # the CACHED hold (min(released, held)); as the cache refreshes, held shrinks and the
        # credit self-cancels, so nothing is ever double-counted in any settle ordering.
        buy_wave = self._refresh_wave.get("buy")
        sell_wave = self._refresh_wave.get("sell")
        wave_ledger_credit_quote = self._wave_still_held("buy")
        wave_ledger_credit_base = self._wave_still_held("sell")
        wave_wallet_credit_quote = (
            min(buy_wave["released"], held_quote) if buy_wave is not None else Decimal("0")
        )
        wave_wallet_credit_base = (
            min(sell_wave["released"], held_base) if sell_wave is not None else Decimal("0")
        )
        self._wave_ledger_credit_quote = wave_ledger_credit_quote
        self._wave_ledger_credit_base = wave_ledger_credit_base

        free_buy_budget_quote, free_sell_budget_base, throttle_scale, deploy_headroom = self._compute_deploy_budgets(
            reference_price=reference_price,
            available_quote=available_quote_balance + wave_wallet_credit_quote,
            available_base=available_base_balance + wave_wallet_credit_base,
            active_buy_reserved_quote=max(Decimal("0"), active_buy_reserved_quote - wave_ledger_credit_quote),
            active_sell_reserved_base=max(Decimal("0"), active_sell_reserved_base - wave_ledger_credit_base),
            deploy_ceiling=deploy_ceiling,
            owned_quote=owned_quote,
            owned_base=owned_base,
            held_quote=held_quote,
            held_base=held_base,
            balance_settling=balance_settling,
        )

        # INFO-log the effective refresh budget per active wave (latched on value change so a
        # short wave logs a handful of lines, not one per tick).
        if buy_wave is not None or sell_wave is not None:
            raw_buy, raw_sell, _, _ = self._compute_deploy_budgets(
                reference_price=reference_price,
                available_quote=available_quote_balance,
                available_base=available_base_balance,
                active_buy_reserved_quote=active_buy_reserved_quote,
                active_sell_reserved_base=active_sell_reserved_base,
                deploy_ceiling=deploy_ceiling,
                owned_quote=owned_quote,
                owned_base=owned_base,
                held_quote=held_quote,
                held_base=held_base,
                balance_settling=balance_settling,
                record_diagnostics=False,
            )
            for side_name, wave, raw_free, effective, released in (
                ("buy", buy_wave, raw_buy, free_buy_budget_quote,
                 buy_wave["released"] if buy_wave is not None else Decimal("0")),
                ("sell", sell_wave, raw_sell, free_sell_budget_base,
                 sell_wave["released"] if sell_wave is not None else Decimal("0")),
            ):
                if wave is None:
                    continue
                log_signature = (str(raw_free), str(released), str(effective))
                if wave["last_log"] != log_signature:
                    wave["last_log"] = log_signature
                    self.logger().info(
                        f"{self.config.id}: refresh budget ({side_name}): free={raw_free} + "
                        f"released_reservations={released} = effective={effective}"
                    )

        # Un-reserved owned figures (the ledger-funded budget source), surfaced for diagnostics so
        # shared-account behavior is observable: owned_*_free vs the wallet available_*.
        owned_quote_free = max(Decimal("0"), owned_quote - active_buy_reserved_quote)
        owned_base_free = max(Decimal("0"), owned_base - active_sell_reserved_base)

        # cap_factor is the throttle scale applied this cycle (1.0 = no throttle).
        cap_factor = throttle_scale
        # deployable_*_total feed the placement loops as the stable per-cycle base.
        deployable_quote_total = free_buy_budget_quote
        deployable_base_total = free_sell_budget_base

        initial_managed_quote = self._d(self._state.get("initial_managed_quote"))
        initial_claimed_base_amount = self._d(self._state.get("initial_claimed_base_amount"))
        initial_reference_price = self._d(self._state.get("initial_reference_price"), "1")
        initial_fund_value_quote = initial_managed_quote + (initial_claimed_base_amount * initial_reference_price)
        fund_growth_quote = managed_fund_value_quote - initial_fund_value_quote

        perf = self._performance_snapshot()
        reconciliation_gap_quote = fund_growth_quote - perf["inventory_global_pnl_quote"]

        if (
            not self.positions_held
            and self._cycles_seen >= 5
            and not self._positions_empty_warning_emitted
        ):
            self._positions_empty_warning_emitted = True
            self.logger().warning(
                f"{self.config.id}: positions_held is still empty after {self._cycles_seen} cycles. "
                "Inventory PnL metrics may remain zero until the connector/orchestrator reports held positions."
            )
            self._emit_structured(
                "range_ladder_positions_tracking_warning",
                cycles_seen=self._cycles_seen,
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
            )

        price_regime = self._current_price_regime(reference_price)
        if price_regime != self._last_price_regime:
            if price_regime in {"below_buy_range", "above_sell_range"}:
                self.logger().warning(
                    f"{self.config.id}: price regime changed to {price_regime} at {reference_price}. "
                    "The market is currently outside the configured ladder range."
                )
            self._emit_structured(
                "range_ladder_price_regime_changed",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                previous_regime=self._last_price_regime or "",
                new_regime=price_regime,
                reference_price=str(reference_price),
            )
            self._last_price_regime = price_regime

        self.processed_data = {
            "reference_price": reference_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "total_quote_balance": total_quote_balance,
            "total_base_balance": total_base_balance,
            "available_quote_balance": available_quote_balance,
            "available_base_balance": available_base_balance,
            "reserve_quote_balance": reserve_quote_balance,
            "reserve_base_balance": reserve_base_balance,
            "managed_quote_total": managed_quote_total,
            "managed_base_total": managed_base_total,
            "managed_fund_value_quote": managed_fund_value_quote,
            "cap_factor": cap_factor,
            "seed_value_quote": seed_value_quote,
            "deploy_ceiling_quote": deploy_ceiling,
            "deploy_headroom_quote": deploy_headroom,
            "deployable_quote_total": deployable_quote_total,
            "deployable_base_total": deployable_base_total,
            "active_buy_reserved_quote": active_buy_reserved_quote,
            "active_sell_reserved_base": active_sell_reserved_base,
            "free_buy_budget_quote": free_buy_budget_quote,
            "free_sell_budget_base": free_sell_budget_base,
            "buy_fee_headroom_quote": self._last_buy_fee_headroom_quote,
            "ledger_surplus_quote": ledger_surplus_quote,
            "refresh_wave_release_buy_quote": buy_wave["released"] if buy_wave is not None else Decimal("0"),
            "refresh_wave_release_sell_base": sell_wave["released"] if sell_wave is not None else Decimal("0"),
            "ledger_funded_budgets": bool(self.config.ledger_funded_budgets),
            "owned_quote_free": owned_quote_free,
            "owned_base_free": owned_base_free,
            "blocked_level_ids": self._recently_closed_level_ids(),
            "initial_fund_value_quote": initial_fund_value_quote,
            "fund_growth_quote": fund_growth_quote,
            "reconciliation_gap_quote": reconciliation_gap_quote,
            "price_regime": price_regime,
            "config_rebuild_pending": self._config_rebuild_pending,
            "state_migrated_from_version": self._state_migrated_from_version,
            "market_data_ready": True,
            "market_data_error": "",
            "market_data_unavailable_duration_s": Decimal("0"),
            "market_data_hard_pause": self._market_data_hard_pause,
            "initialization_ready": True,
            "initialization_blocked_reason": "",
            "session_elapsed_s": Decimal(str(session_elapsed_s)),
            "session_expired": self._session_expired,
            "session_expired_reason": self._session_expired_reason,
            "max_session_duration_hours": self.config.max_session_duration_hours,
            "max_market_data_unavailable_seconds": self.config.max_market_data_unavailable_seconds,
            "max_fund_value_quote": Decimal(self.config.max_fund_value_quote),
            "reservation_sources_buy": dict(self._buy_reservation_sources),
            "reservation_sources_sell": dict(self._sell_reservation_sources),
            **self._refresh_status_fields(now),
            **perf,
        }

        if not self._startup_logged:
            self._startup_logged = True
            self.logger().info(
                f"First cycle for {self.config.id}: "
                f"{base_asset} avail={available_base_balance:.8f} total={total_base_balance:.8f} | "
                f"{quote_asset} avail={available_quote_balance:.8f} total={total_quote_balance:.8f}"
            )
            self._emit_structured(
                "range_ladder_live_run_started",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                diagnostic_log_path=self.diagnostic_log_path,
                state_file_abs=str(self.state_path_abs),
                max_session_duration_hours=self.config.max_session_duration_hours,
                max_market_data_unavailable_seconds=self.config.max_market_data_unavailable_seconds,
            )

        self.logger().debug(
            f"Cycle {self.config.id}: mid={reference_price} bid={best_bid} ask={best_ask} "
            f"managed_fund={managed_fund_value_quote} free_buy={free_buy_budget_quote} "
            f"free_sell={free_sell_budget_base} pnl={perf['inventory_global_pnl_quote']} "
            f"recon_gap={reconciliation_gap_quote} regime={price_regime} "
            f"rebuild_pending={self._config_rebuild_pending} session_expired={self._session_expired} "
            f"market_data_hard_pause={self._market_data_hard_pause}"
        )
        self._emit_structured(
            "range_ladder_cycle",
            connector=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            mid_price=str(reference_price),
            best_bid=str(best_bid),
            best_ask=str(best_ask),
            managed_quote_total=str(managed_quote_total),
            managed_base_total=str(managed_base_total),
            managed_fund_value_quote=str(managed_fund_value_quote),
            free_buy_budget_quote=str(free_buy_budget_quote),
            account_available_quote_cap=str(available_quote_balance),
            controller_quota_quote=(
                None if self.config.shared_account_quote_quota is None
                else str(self.config.shared_account_quote_quota)
            ),
            free_sell_budget_base=str(free_sell_budget_base),
            inventory_global_pnl_quote=str(perf["inventory_global_pnl_quote"]),
            reconciliation_gap_quote=str(reconciliation_gap_quote),
            active_executors=len(self._active_order_executors()),
            price_regime=price_regime,
            config_rebuild_pending=str(self._config_rebuild_pending),
            session_expired=str(self._session_expired),
            market_data_hard_pause=str(self._market_data_hard_pause),
        )

        # v12 Part B: store this cycle's TOTAL balances for next-cycle delta comparison.
        # Only reached on a fully-processed cycle (the market-data-unavailable and
        # not-initialized early returns above intentionally do not advance these, so a
        # transient outage never fabricates a spurious delta on recovery).
        self._prev_total_quote_balance = total_quote_balance
        self._prev_total_base_balance = total_base_balance

        # Zero-level deadlock watchdog (fix 3): runs AFTER processed_data is assembled so the
        # plan/budget checks see THIS cycle's effective budgets.
        self._run_empty_side_watchdog(now)

        self._emit_diagnostic_heartbeat_if_due()


    def _find_executor_by_id(self, executor_id: str):
        """Find an executor by its ID from the controller's executors."""
        for executor in self.executors_info:
            if executor.id == executor_id:
                return executor
        return None

    def _executor_side_by_id(self, executor_id: str) -> Optional[TradeType]:
        """Side of the executor with this id, or None when the id does not resolve (a stop
        action can race an executors_info refresh; a missing executor is neither side and
        must not crash the cycle)."""
        executor = self._find_executor_by_id(executor_id)
        if executor is None:
            return None
        return self._executor_side(executor)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        # v12 Issue 3: reset the side-specific defer flags every cycle so a cycle with no
        # stops never inherits a stale defer from a previous one.
        self._defer_buy_creates_this_cycle = False
        self._defer_sell_creates_this_cycle = False

        # Fix 4: BEFORE the stop/create pass, resolve refresh waves and re-propose rebuilds
        # whose creates the orchestrator dropped (may re-mark a side dirty for this tick).
        self._reconcile_refresh_waves(
            self.market_data_provider.time(),
            allow_repropose=bool(self.config.event_refresh_enabled),
        )

        actions: List[ExecutorAction] = []
        stop_actions = self.stop_actions_proposal()
        actions.extend(stop_actions)

        # v12 Issue 3: if we are stopping executors this cycle, defer create decisions only
        # for the SIDE(S) that have stops -- a stop on one side must not block creates on the
        # other. This still prevents sizing against stale reservations from soon-to-be-
        # cancelled orders on the SAME side (the original compression rationale).
        if stop_actions:
            has_buy_stops = any(
                self._executor_side_by_id(a.executor_id) == TradeType.BUY
                for a in stop_actions
                if hasattr(a, 'executor_id')
            )
            has_sell_stops = any(
                self._executor_side_by_id(a.executor_id) == TradeType.SELL
                for a in stop_actions
                if hasattr(a, 'executor_id')
            )
            self._defer_buy_creates_this_cycle = has_buy_stops
            self._defer_sell_creates_this_cycle = has_sell_stops
            if has_buy_stops or has_sell_stops:
                self._emit_structured(
                    "range_ladder_create_deferred_for_stops",
                    buy_stops=has_buy_stops,
                    sell_stops=has_sell_stops,
                    stop_count=len(stop_actions),
                )
        # Do NOT return early: fall through so the unaffected side can still place this cycle.
        actions.extend(self.create_actions_proposal())
        return actions

    @staticmethod
    def _price_level_token(price: Decimal) -> str:
        normalized = format(price.normalize(), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return normalized or "0"

    def _buy_level_id(self, idx: int) -> str:
        return f"buy_{self._price_level_token(self.config.buy_prices[idx])}"

    def _sell_level_id(self, idx: int) -> str:
        return f"sell_{self._price_level_token(self.config.sell_prices[idx])}"

    def _can_place_buy_level(self, price: Decimal) -> bool:
        if not self.config.passive_order_placement:
            return True
        return price < self.processed_data["best_bid"]

    def _can_place_sell_level(self, price: Decimal) -> bool:
        if not self.config.passive_order_placement:
            return True
        return price > self.processed_data["best_ask"]

    def _emit_compression_event_if_changed(
        self,
        side: str,
        candidate_indexes: List[int],
        kept_indexes: List[int],
        total_budget,
    ):
        if side == "buy":
            level_id_fn = self._buy_level_id
            price_list = self.config.buy_prices
            signature_attr = "_last_buy_compression_signature"
            event_type = "range_ladder_buy_levels_compressed_for_min_notional"
            budget_field = "free_buy_budget_quote"
            budget_asset = self.processed_data.get("quote_asset", "")
        else:
            level_id_fn = self._sell_level_id
            price_list = self.config.sell_prices
            signature_attr = "_last_sell_compression_signature"
            event_type = "range_ladder_sell_levels_compressed_for_min_notional"
            budget_field = "free_sell_budget_base"
            budget_asset = self.processed_data.get("base_asset", "")

        candidate_level_ids = [level_id_fn(idx) for idx in candidate_indexes]
        kept_level_ids = [level_id_fn(idx) for idx in kept_indexes]
        dropped_indexes = [idx for idx in candidate_indexes if idx not in set(kept_indexes)]
        dropped_prices = [str(price_list[idx]) for idx in dropped_indexes]

        if not dropped_indexes:
            setattr(self, signature_attr, None)
            return

        signature = (tuple(candidate_level_ids), tuple(kept_level_ids))
        if getattr(self, signature_attr) == signature:
            return

        setattr(self, signature_attr, signature)
        self.logger().info(
            f"{self.config.id}: compressed {side} ladder for min notional. "
            f"dropped_prices={dropped_prices} kept_levels={kept_level_ids} "
            f"{budget_field}={total_budget} min_order_quote={self.config.min_order_quote}"
        )
        self._emit_structured(
            event_type,
            dropped_prices=dropped_prices,
            dropped_level_ids=[level_id_fn(idx) for idx in dropped_indexes],
            kept_level_ids=kept_level_ids,
            min_order_quote=str(self.config.min_order_quote),
            **{budget_field: str(total_budget), "budget_asset": budget_asset},
        )

    # ---------------------------------------------------------------- exchange-minimum gate
    # v15: the connector's quantize_order_amount only snaps to the size quantum — it does NOT
    # zero amounts below the exchange's min_order_size (that is only enforced connector-side at
    # order creation, where it raises and burns executor retries). The controller must therefore
    # enforce the exchange trading rule itself. One shared feasibility gate is used by the
    # compression, the create path and the planner so the three can never drift.

    def _exchange_trading_rule(self):
        """The connector's TradingRule for our pair, or None when unavailable (e.g. mocked
        provider in tests, or rules not yet fetched)."""
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            rules = getattr(connector, "trading_rules", None)
            if not isinstance(rules, dict):
                return None
            return rules.get(self.config.trading_pair)
        except Exception:
            return None

    @staticmethod
    def _rule_decimal(rule, attr: str) -> Decimal:
        """A TradingRule numeric field as a positive finite Decimal, else 0 (missing, NaN, Inf
        and unparsable values all mean 'no exchange constraint')."""
        try:
            value = Decimal(str(getattr(rule, attr, 0) or 0))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")
        return value if value.is_finite() and value > Decimal("0") else Decimal("0")

    def _exchange_min_order_size(self) -> Decimal:
        """Exchange minimum order amount in BASE units (0 when unknown)."""
        rule = self._exchange_trading_rule()
        if rule is None:
            return Decimal("0")
        return self._rule_decimal(rule, "min_order_size")

    def _exchange_min_notional(self) -> Decimal:
        """Exchange minimum order value in QUOTE units (0 when unknown). Takes the stricter of
        min_notional_size and min_order_value — connectors populate one or the other."""
        rule = self._exchange_trading_rule()
        if rule is None:
            return Decimal("0")
        return max(self._rule_decimal(rule, "min_notional_size"),
                   self._rule_decimal(rule, "min_order_value"))

    def _level_quantization_failure(self, qamount: Decimal, qprice: Decimal) -> Optional[str]:
        """Single feasibility gate for a quantized level. Returns a reason string when the level
        cannot be placed (zero amount, below config min notional, or below the EXCHANGE minimum
        order size / notional), or None when the level is feasible."""
        if qamount <= Decimal("0"):
            return "quantized_amount_zero"
        if qamount < self._exchange_min_order_size():
            return "below_exchange_min_order_size"
        notional = qamount * qprice
        if notional < self.config.min_order_quote:
            return "notional_below_min"
        if notional < self._exchange_min_notional():
            return "below_exchange_min_notional"
        return None

    def _compress_buy_level_indexes_for_min_notional(
        self,
        candidate_indexes: List[int],
        total_quote_budget: Decimal,
    ) -> List[int]:
        """
        Concentrate the available budget into the nearest kept buy levels.

        Drop the farthest (lowest-priced) eligible buy levels until every kept level
        can be seeded above min_order_quote, RE-NORMALIZING the weights over the kept
        subset so the available budget is fully deployed across the levels we keep
        (nearest first). A budget that can fund at least one level always keeps at
        least one level -- it never strands a small balance by spreading it across
        far levels that each fall below min notional.
        """
        kept_indexes = list(candidate_indexes)
        if total_quote_budget < self.config.min_order_quote:
            return []

        while kept_indexes:
            # Concentrate: each kept level's allocation is its weight RE-NORMALIZED
            # over the currently-kept subset, so the whole budget is deployed across
            # the nearest levels. This must match the placement loop in
            # _create_buy_actions.
            kept_weight_total = sum(self.config.normalized_buy_weights[idx] for idx in kept_indexes)
            if kept_weight_total <= Decimal("0"):
                return []

            all_kept_levels_feasible = True
            for idx in kept_indexes:
                level_weight = self.config.normalized_buy_weights[idx] / kept_weight_total
                level_quote = total_quote_budget * level_weight
                # Quantization-aware feasibility: simulate what _build_buy_executor_action does
                price = self.config.buy_prices[idx]
                quantized_price = Decimal(
                    self.market_data_provider.quantize_order_price(
                        self.config.connector_name, self.config.trading_pair, price
                    )
                )
                if quantized_price <= Decimal("0"):
                    all_kept_levels_feasible = False
                    break
                amount = level_quote / quantized_price
                quantized_amount = Decimal(
                    self.market_data_provider.quantize_order_amount(
                        self.config.connector_name, self.config.trading_pair, amount
                    )
                )
                if self._level_quantization_failure(quantized_amount, quantized_price) is not None:
                    all_kept_levels_feasible = False
                    break

            if all_kept_levels_feasible:
                return kept_indexes

            kept_indexes.pop()  # drop lowest / farthest buy level first

        return []

    def _compress_sell_level_indexes_for_min_notional(
        self,
        candidate_indexes: List[int],
        total_base_budget: Decimal,
    ) -> List[int]:
        """
        Concentrate the available base inventory into the nearest kept sell levels.

        Mirrors buy-side compression: drop the farthest (highest-priced) eligible sell
        levels until every kept level clears min_order_quote, RE-NORMALIZING the weights
        over the kept subset so the available base is fully deployed across the nearest
        sell levels (340, 345, ... first). A budget that can fund one level keeps one.
        """
        kept_indexes = list(candidate_indexes)
        if total_base_budget <= Decimal("0"):
            return []

        while kept_indexes:
            # Concentrate: each kept level's allocation is its weight RE-NORMALIZED
            # over the currently-kept subset. This must match the placement loop in
            # _create_sell_actions.
            kept_weight_total = sum(self.config.normalized_sell_weights[idx] for idx in kept_indexes)
            if kept_weight_total <= Decimal("0"):
                return []

            all_kept_levels_feasible = True
            for idx in kept_indexes:
                level_weight = self.config.normalized_sell_weights[idx] / kept_weight_total
                level_base = total_base_budget * level_weight
                # Quantization-aware feasibility: simulate what _build_sell_executor_action does
                price = self.config.sell_prices[idx]
                quantized_price = Decimal(
                    self.market_data_provider.quantize_order_price(
                        self.config.connector_name, self.config.trading_pair, price
                    )
                )
                quantized_amount = Decimal(
                    self.market_data_provider.quantize_order_amount(
                        self.config.connector_name, self.config.trading_pair, level_base
                    )
                )
                if self._level_quantization_failure(quantized_amount, quantized_price) is not None:
                    all_kept_levels_feasible = False
                    break

            if all_kept_levels_feasible:
                return kept_indexes

            kept_indexes.pop()  # drop highest / farthest sell level first

        return []



    def _build_buy_executor_action(
        self,
        *,
        idx: int,
        level_id: str,
        price: Decimal,
        order_quote: Decimal,
        remaining_quote_before: Decimal,
        kept_buy_indexes: List[int],
        passive_execution_strategy: ExecutionStrategy,
    ):
        quantized_price = Decimal(
            self.market_data_provider.quantize_order_price(self.config.connector_name, self.config.trading_pair, price)
        )
        amount = order_quote / quantized_price
        quantized_amount = Decimal(
            self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, amount
            )
        )
        notional = quantized_amount * quantized_price
        failure_reason = self._level_quantization_failure(quantized_amount, quantized_price)
        if failure_reason is not None:
            self._emit_structured(
                "range_ladder_buy_level_skipped_post_quantization",
                level_id=level_id,
                raw_price=str(price),
                quantized_price=str(quantized_price),
                raw_amount=str(amount),
                quantized_amount=str(quantized_amount),
                notional=str(notional),
                min_order_quote=str(self.config.min_order_quote),
                exchange_min_order_size=str(self._exchange_min_order_size()),
                allocated_quote=str(order_quote),
                reason=failure_reason,
            )
            return None, Decimal("0")
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,
            amount=quantized_amount,
            price=quantized_price,
            execution_strategy=passive_execution_strategy,
        )
        self._emit_structured(
            "range_ladder_buy_action",
            level_id=level_id,
            price=str(quantized_price),
            amount=str(quantized_amount),
            notional_quote=str(notional),
            remaining_quote_before=str(remaining_quote_before),
            compressed_buy_levels=[self._buy_level_id(i) for i in kept_buy_indexes],
        )
        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config,
            min_fill_ratio=self._action_min_fill_ratio(),
        ), notional

    def _build_sell_executor_action(
        self,
        *,
        idx: int,
        level_id: str,
        price: Decimal,
        order_base: Decimal,
        remaining_base_before: Decimal,
        kept_sell_indexes: List[int],
        passive_execution_strategy: ExecutionStrategy,
    ):
        quantized_price = Decimal(
            self.market_data_provider.quantize_order_price(self.config.connector_name, self.config.trading_pair, price)
        )
        quantized_amount = Decimal(
            self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, order_base
            )
        )
        notional = quantized_amount * quantized_price
        failure_reason = self._level_quantization_failure(quantized_amount, quantized_price)
        if failure_reason is not None:
            self._emit_structured(
                "range_ladder_sell_level_skipped_post_quantization",
                level_id=level_id,
                raw_price=str(price),
                quantized_price=str(quantized_price),
                raw_amount=str(order_base),
                quantized_amount=str(quantized_amount),
                notional=str(notional),
                min_order_quote=str(self.config.min_order_quote),
                exchange_min_order_size=str(self._exchange_min_order_size()),
                allocated_base=str(order_base),
                reason=failure_reason,
            )
            return None, Decimal("0")
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.SELL,
            amount=quantized_amount,
            price=quantized_price,
            execution_strategy=passive_execution_strategy,
        )
        self._emit_structured(
            "range_ladder_sell_action",
            level_id=level_id,
            price=str(quantized_price),
            amount=str(quantized_amount),
            notional_quote=str(notional),
            remaining_base_before=str(remaining_base_before),
            compressed_sell_levels=[self._sell_level_id(i) for i in kept_sell_indexes],
        )
        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config,
            min_fill_ratio=self._action_min_fill_ratio(),
        ), quantized_amount

    def _action_min_fill_ratio(self) -> Optional[Decimal]:
        """min_fill_ratio carried on create actions so the budget preflight DROPS (for
        full-size retry) instead of resizing an order to dust. None when disabled."""
        ratio = Decimal(self.config.preflight_min_fill_ratio)
        return ratio if ratio > Decimal("0") else None

    def _check_plan_budget_invariant(self, side_name: str, planned_total: Decimal,
                                     budget: Decimal, context: str):
        """Safety-net invariant (2026-07-08 addendum): a side's planned/issued amounts must
        never exceed its effective budget (quote notional for buys, base for sells). The
        planner sizes FROM the budget, so this is expected to always hold -- a violation
        means an upstream sizing bug. WARNING + structured event only, rate-limited; NEVER
        blocks the refresh (placement stays bounded by the create loop and the budget
        preflight)."""
        epsilon = max(Decimal("1e-9"), budget * Decimal("1e-6"))
        if planned_total <= budget + epsilon:
            return
        now = self.market_data_provider.time()
        if (now - self._plan_invariant_last_warn.get(side_name, 0.0)) < self._drift_warning_interval:
            return
        self._plan_invariant_last_warn[side_name] = now
        self.logger().warning(
            f"{self.config.id}: PLAN/BUDGET INVARIANT VIOLATION ({side_name}, {context}): "
            f"planned={planned_total} exceeds the effective budget={budget} by "
            f"{planned_total - budget}. Placement remains bounded by the create loop and the "
            "budget preflight -- investigate the sizing math."
        )
        self._emit_structured(
            "range_ladder_plan_budget_invariant_violation",
            side=side_name,
            context=context,
            planned_total=str(planned_total),
            budget=str(budget),
            excess=str(planned_total - budget),
        )

    def _heal_scope_levels(self, side_name: str) -> Optional[Set[str]]:
        """When this side's dirty reason is a reconciliation heal, the set of level ids the
        rebuild may place (the still-unsatisfied intent); None for a normal full rebuild."""
        reason = self._buy_dirty_reason if side_name == "buy" else self._sell_dirty_reason
        if reason != "ladder_reconcile":
            return None
        record = self._refresh_wave.get(side_name)
        if not record or not record.get("intended"):
            return None
        return set(record["intended"].keys())

    # ---------------------------------------------------------------- per-side refresh planner
    # The planner mirrors the create path's eligibility (passive filter) + compression + weight
    # distribution + quantization PURELY (no events, no actions). It answers "what book would a
    # fresh rebuild of this side rest right now?" -- used by the no-op/dust guard (skip churn when
    # the rebuild reproduces the resting book) and the dirty-clear convergence check. A unit test
    # cross-checks the planner against the live create path on a fresh build so they never drift.

    def _side_rebuild_budget_quote(self) -> Decimal:
        """Quote a fresh BUY rebuild would deploy: the side's current free budget PLUS the quote
        that cancelling its own resting buys would return to availability (the ceiling already
        bounds free + reserved). This matches the budget the create path will actually see once
        the side's resting orders are cancelled. `free` is already fee-haircut by
        _compute_deploy_budgets; the reserved add-back gets the same haircut, because once the
        resting notional (and its fee hold) returns to the wallet it is re-haircut before
        redeployment -- keeping the planner and the live rebuild sized identically."""
        p = self.processed_data or {}
        free = self._d(p.get("free_buy_budget_quote", "0"), "0")
        reserved = self._d(p.get("active_buy_reserved_quote", "0"), "0")
        # A refresh wave's cancelled-but-still-closing reservations were already credited
        # into `free` by the budget computation -- exclude them from the add-back so the
        # rebuild never counts the same reservation twice.
        reserved = max(Decimal("0"), reserved - self._wave_ledger_credit_quote)
        fee_rate = max(Decimal("0"), Decimal(self.config.fee_rate))
        return max(Decimal("0"), free + reserved / (Decimal("1") + fee_rate))

    def _side_rebuild_budget_base(self) -> Decimal:
        """Base a fresh SELL rebuild would deploy (free + this side's own resting reservation)."""
        p = self.processed_data or {}
        free = self._d(p.get("free_sell_budget_base", "0"), "0")
        reserved = self._d(p.get("active_sell_reserved_base", "0"), "0")
        # Exclude the wave's already-credited reservations (see _side_rebuild_budget_quote).
        reserved = max(Decimal("0"), reserved - self._wave_ledger_credit_base)
        return max(Decimal("0"), free + reserved)

    def _quantize_buy_level(self, price: Decimal, order_quote: Decimal):
        """Pure quantization mirror of _build_buy_executor_action; returns (qamount, notional,
        qprice) or None when the level is infeasible (zero amount / sub-min-notional)."""
        qprice = self._d(self.market_data_provider.quantize_order_price(
            self.config.connector_name, self.config.trading_pair, price), "0")
        if qprice <= Decimal("0"):
            return None
        qamount = self._d(self.market_data_provider.quantize_order_amount(
            self.config.connector_name, self.config.trading_pair, order_quote / qprice), "0")
        notional = qamount * qprice
        if self._level_quantization_failure(qamount, qprice) is not None:
            return None
        return qamount, notional, qprice

    def _quantize_sell_level(self, price: Decimal, order_base: Decimal):
        """Pure quantization mirror of _build_sell_executor_action."""
        qprice = self._d(self.market_data_provider.quantize_order_price(
            self.config.connector_name, self.config.trading_pair, price), "0")
        qamount = self._d(self.market_data_provider.quantize_order_amount(
            self.config.connector_name, self.config.trading_pair, order_base), "0")
        notional = qamount * qprice
        if self._level_quantization_failure(qamount, qprice) is not None:
            return None
        return qamount, notional, qprice

    def _plan_buy_book(self) -> Dict[str, Decimal]:
        """The BUY book a fresh rebuild would rest, as {level_id: quantized_base_amount}."""
        p = self.processed_data
        if not p or "best_bid" not in p:
            return {}
        budget = self._side_rebuild_budget_quote()
        if budget < self.config.min_order_quote:
            return {}
        eligible = [idx for idx, price in enumerate(self.config.buy_prices)
                    if self._can_place_buy_level(price)]
        kept = self._compress_buy_level_indexes_for_min_notional(eligible, budget)
        if not kept:
            return {}
        kept_weight_total = sum(self.config.normalized_buy_weights[i] for i in kept)
        if kept_weight_total <= Decimal("0"):
            return {}
        book: Dict[str, Decimal] = {}
        remaining = budget
        pending = list(kept)
        while pending and remaining >= self.config.min_order_quote:
            idx = pending.pop(0)
            price = self.config.buy_prices[idx]
            level_weight = self.config.normalized_buy_weights[idx] / kept_weight_total
            if level_weight <= Decimal("0"):
                continue
            target_quote = min(budget * level_weight, remaining)
            quantized = None
            if target_quote >= self.config.min_order_quote:
                quantized = self._quantize_buy_level(price, target_quote)
            if quantized is None and self.config.allow_partial_levels and not pending and remaining >= self.config.min_order_quote:
                quantized = self._quantize_buy_level(price, remaining)
            if quantized is None:
                continue
            qamount, notional, _ = quantized
            book[self._buy_level_id(idx)] = qamount
            remaining = max(Decimal("0"), remaining - notional)
        return book

    def _plan_sell_book(self) -> Dict[str, Decimal]:
        """The SELL book a fresh rebuild would rest, as {level_id: quantized_base_amount}."""
        p = self.processed_data
        if not p or "best_ask" not in p:
            return {}
        budget = self._side_rebuild_budget_base()
        if budget <= Decimal("0"):
            return {}
        eligible = [idx for idx, price in enumerate(self.config.sell_prices)
                    if self._can_place_sell_level(price)]
        kept = self._compress_sell_level_indexes_for_min_notional(eligible, budget)
        if not kept:
            return {}
        kept_weight_total = sum(self.config.normalized_sell_weights[i] for i in kept)
        if kept_weight_total <= Decimal("0"):
            return {}
        book: Dict[str, Decimal] = {}
        remaining = budget
        pending = list(kept)
        while pending and remaining > Decimal("0"):
            idx = pending.pop(0)
            price = self.config.sell_prices[idx]
            level_weight = self.config.normalized_sell_weights[idx] / kept_weight_total
            if level_weight <= Decimal("0"):
                continue
            target_base = min(budget * level_weight, remaining)
            quantized = None
            if target_base * price >= self.config.min_order_quote:
                quantized = self._quantize_sell_level(price, target_base)
            if quantized is None and self.config.allow_partial_levels and not pending and (remaining * price) >= self.config.min_order_quote:
                quantized = self._quantize_sell_level(price, remaining)
            if quantized is None:
                continue
            qamount, _, _ = quantized
            book[self._sell_level_id(idx)] = qamount
            remaining = max(Decimal("0"), remaining - qamount)
        return book

    def _resting_side_book(self, side: TradeType) -> Dict[str, Decimal]:
        """{level_id: configured base amount} for this side's currently-resting (active) orders."""
        book: Dict[str, Decimal] = {}
        for executor in self._active_order_executors():
            if self._executor_side(executor) != side:
                continue
            level_id = getattr(executor.config, "level_id", None)
            if not level_id:
                continue
            amount = self._d(getattr(executor.config, "amount", "0") or "0")
            book[level_id] = book.get(level_id, Decimal("0")) + max(Decimal("0"), amount)
        return book

    def _side_refresh_converged(self, side: TradeType) -> bool:
        """True when refreshing `side` would NOT meaningfully change its book -> skip the
        cancel/recreate (no churn). This is BOTH guards in one test:
          - No-op guard: the rebuild rests the SAME rungs and the total deployed value differs by
            less than min_order_quote (one order's worth) -> nothing worth churning for.
          - Dust guard: a sub-min_order_quote freed amount can neither add a rung nor shift the
            total by a whole order, so it falls under the same threshold -> skip.
        A changed rung SET (a rung became eligible/ineligible, or compression added/dropped one --
        e.g. the 305->312 re-center) is always a real change -> refresh.
        """
        if side == TradeType.BUY:
            planned, resting = self._plan_buy_book(), self._resting_side_book(TradeType.BUY)
            prices = {self._buy_level_id(i): self.config.buy_prices[i] for i in range(len(self.config.buy_prices))}
        else:
            planned, resting = self._plan_sell_book(), self._resting_side_book(TradeType.SELL)
            prices = {self._sell_level_id(i): self.config.sell_prices[i] for i in range(len(self.config.sell_prices))}
        if set(planned.keys()) != set(resting.keys()):
            return False
        planned_notional = sum(amt * prices.get(lid, Decimal("0")) for lid, amt in planned.items())
        resting_notional = sum(amt * prices.get(lid, Decimal("0")) for lid, amt in resting.items())
        return abs(planned_notional - resting_notional) < Decimal(self.config.min_order_quote)

    def _nearest_eligible_rung(self, side: TradeType) -> str:
        """Anchor rung for the refresh event: nearest-to-price eligible rung on `side`."""
        if side == TradeType.BUY:
            elig = [p for p in self.config.buy_prices if self._can_place_buy_level(p)]
            return str(max(elig)) if elig else ""
        elig = [p for p in self.config.sell_prices if self._can_place_sell_level(p)]
        return str(min(elig)) if elig else ""

    def _create_buy_actions(self) -> List[CreateExecutorAction]:
        # v12 Issue 3: a buy stop this cycle defers only buy creates (sell creates proceed).
        if self._defer_buy_creates_this_cycle:
            return []
        # Per-side model: only (re)build the BUY side when it is dirty (a trigger fired). When
        # the side is quiet, empty rungs stay empty -- a filled buy level is not instantly re-
        # bought; it waits for the BUY cooldown to lapse or the global timer (the contract).
        if self.config.event_refresh_enabled and not self._buy_side_dirty:
            return []
        # Refresh-wave gate: while this side's cancelled orders are still closing, their
        # levels are blocked -- placing now would concentrate the whole credited budget into
        # the few unblocked rungs. Wait; the dirty flag keeps the rebuild owed.
        if self._wave_cancels_in_flight("buy"):
            return []
        # Post-cancel balance gate: give the forced balance refresh a moment to surface the
        # freed collateral so the budget preflight doesn't drop the rebuild on a stale
        # snapshot (bounded by post_cancel_balance_timeout_seconds).
        if self._wave_balance_gate_active("buy"):
            return []
        # Heal scope (intended-vs-live reconciliation): a heal rebuild places ONLY the
        # still-unsatisfied intended levels -- a level that filled during the retry window is
        # success, and re-placing it here would bypass the per-side cooldown contract.
        heal_scope = self._heal_scope_levels("buy")
        actions: List[CreateExecutorAction] = []
        blocked_levels: Set[str] = self.processed_data["blocked_level_ids"]
        remaining_quote_budget = self.processed_data["free_buy_budget_quote"]

        eligible_buy_indexes: List[int] = []
        previous_filter_reasons = self._buy_level_filter_reasons
        current_filter_reasons: Dict[str, Optional[str]] = {}
        for idx, price in enumerate(self.config.buy_prices):
            level_id = self._buy_level_id(idx)
            if heal_scope is not None and level_id not in heal_scope:
                current_filter_reasons[level_id] = "heal_scope"
                continue
            if level_id in blocked_levels:
                current_filter_reasons[level_id] = "blocked"
                if previous_filter_reasons.get(level_id) != "blocked":
                    self._emit_structured(
                        "range_ladder_buy_level_filtered_blocked",
                        level_id=level_id,
                        price=str(price),
                    )
                continue
            if not self._can_place_buy_level(price):
                current_filter_reasons[level_id] = "not_passive"
                if previous_filter_reasons.get(level_id) != "not_passive":
                    self._emit_structured(
                        "range_ladder_buy_level_filtered_not_passive",
                        level_id=level_id,
                        price=str(price),
                        best_bid=str(self.processed_data["best_bid"]),
                    )
                continue
            current_filter_reasons[level_id] = None
            if previous_filter_reasons.get(level_id) is not None:
                self._emit_structured(
                    "range_ladder_buy_level_eligible_again",
                    level_id=level_id,
                    price=str(price),
                    previous_reason=previous_filter_reasons.get(level_id),
                )
            eligible_buy_indexes.append(idx)
        self._buy_level_filter_reasons = current_filter_reasons

        kept_buy_indexes = self._compress_buy_level_indexes_for_min_notional(
            candidate_indexes=eligible_buy_indexes,
            total_quote_budget=remaining_quote_budget,
        )
        self._emit_compression_event_if_changed(
            side="buy",
            candidate_indexes=eligible_buy_indexes,
            kept_indexes=kept_buy_indexes,
            total_budget=remaining_quote_budget,
        )
        if not kept_buy_indexes:
            return []

        passive_execution_strategy = self._passive_execution_strategy()
        pending_indexes = list(kept_buy_indexes)

        # Concentrate: deploy the full available budget across the KEPT (nearest)
        # levels by re-normalizing each kept level's weight over the kept subset.
        # This matches _compress_buy_level_indexes_for_min_notional so that placement
        # actually deploys the concentrated amounts the compression check assumed --
        # a small balance funds the nearest level(s) fully instead of stranding budget
        # on far levels that fall below min notional.
        deployable_quote_total = self.processed_data.get(
            "deployable_quote_total", remaining_quote_budget
        )
        kept_weight_total = sum(self.config.normalized_buy_weights[i] for i in kept_buy_indexes)

        while pending_indexes and remaining_quote_budget >= self.config.min_order_quote:
            idx = pending_indexes.pop(0)
            price = self.config.buy_prices[idx]
            level_id = self._buy_level_id(idx)
            if kept_weight_total <= Decimal("0"):
                break
            level_weight = self.config.normalized_buy_weights[idx] / kept_weight_total
            if level_weight <= Decimal("0"):
                continue

            target_quote = deployable_quote_total * level_weight

            # Never request more than what's actually free right now.
            target_quote = min(target_quote, remaining_quote_budget)

            if target_quote < self.config.min_order_quote:
                continue

            action, notional = self._build_buy_executor_action(
                idx=idx,
                level_id=level_id,
                price=price,
                order_quote=target_quote,
                remaining_quote_before=remaining_quote_budget,
                kept_buy_indexes=kept_buy_indexes,
                passive_execution_strategy=passive_execution_strategy,
            )

            if action is None and self.config.allow_partial_levels and not pending_indexes and remaining_quote_budget >= self.config.min_order_quote:
                action, notional = self._build_buy_executor_action(
                    idx=idx,
                    level_id=level_id,
                    price=price,
                    order_quote=remaining_quote_budget,
                    remaining_quote_before=remaining_quote_budget,
                    kept_buy_indexes=kept_buy_indexes,
                    passive_execution_strategy=passive_execution_strategy,
                )

            if action is None:
                continue

            actions.append(action)
            remaining_quote_budget = max(Decimal("0"), remaining_quote_budget - notional)

        return actions

    def _create_sell_actions(self) -> List[CreateExecutorAction]:
        # v12 Issue 3: a sell stop this cycle defers only sell creates (buy creates proceed).
        if self._defer_sell_creates_this_cycle:
            return []
        # Per-side model: only (re)build the SELL side when it is dirty (see _create_buy_actions).
        if self.config.event_refresh_enabled and not self._sell_side_dirty:
            return []
        # Refresh-wave gate: see _create_buy_actions -- wait for this side's cancels to close.
        if self._wave_cancels_in_flight("sell"):
            return []
        # Post-cancel balance gate: see _create_buy_actions.
        if self._wave_balance_gate_active("sell"):
            return []
        # Heal scope: see _create_buy_actions -- heal rebuilds fill only the missing rungs.
        heal_scope = self._heal_scope_levels("sell")
        actions: List[CreateExecutorAction] = []
        blocked_levels: Set[str] = self.processed_data["blocked_level_ids"]
        remaining_base_budget = self.processed_data["free_sell_budget_base"]

        eligible_sell_indexes: List[int] = []
        previous_filter_reasons = self._sell_level_filter_reasons
        current_filter_reasons: Dict[str, Optional[str]] = {}
        for idx, price in enumerate(self.config.sell_prices):
            level_id = self._sell_level_id(idx)
            if heal_scope is not None and level_id not in heal_scope:
                current_filter_reasons[level_id] = "heal_scope"
                continue
            if level_id in blocked_levels:
                current_filter_reasons[level_id] = "blocked"
                if previous_filter_reasons.get(level_id) != "blocked":
                    self._emit_structured(
                        "range_ladder_sell_level_filtered_blocked",
                        level_id=level_id,
                        price=str(price),
                    )
                continue
            if not self._can_place_sell_level(price):
                current_filter_reasons[level_id] = "not_passive"
                if previous_filter_reasons.get(level_id) != "not_passive":
                    self._emit_structured(
                        "range_ladder_sell_level_filtered_not_passive",
                        level_id=level_id,
                        price=str(price),
                        best_ask=str(self.processed_data["best_ask"]),
                    )
                continue
            current_filter_reasons[level_id] = None
            if previous_filter_reasons.get(level_id) is not None:
                self._emit_structured(
                    "range_ladder_sell_level_eligible_again",
                    level_id=level_id,
                    price=str(price),
                    previous_reason=previous_filter_reasons.get(level_id),
                )
            eligible_sell_indexes.append(idx)
        self._sell_level_filter_reasons = current_filter_reasons

        kept_sell_indexes = self._compress_sell_level_indexes_for_min_notional(
            candidate_indexes=eligible_sell_indexes,
            total_base_budget=remaining_base_budget,
        )
        self._emit_compression_event_if_changed(
            side="sell",
            candidate_indexes=eligible_sell_indexes,
            kept_indexes=kept_sell_indexes,
            total_budget=remaining_base_budget,
        )
        if not kept_sell_indexes:
            return []

        passive_execution_strategy = self._passive_execution_strategy()
        pending_indexes = list(kept_sell_indexes)

        # Concentrate over the kept subset (see _create_buy_actions for rationale).
        deployable_base_total = self.processed_data.get(
            "deployable_base_total", remaining_base_budget
        )
        kept_weight_total = sum(self.config.normalized_sell_weights[i] for i in kept_sell_indexes)

        while pending_indexes and remaining_base_budget > Decimal("0"):
            idx = pending_indexes.pop(0)
            price = self.config.sell_prices[idx]
            level_id = self._sell_level_id(idx)
            if kept_weight_total <= Decimal("0"):
                break
            level_weight = self.config.normalized_sell_weights[idx] / kept_weight_total
            if level_weight <= Decimal("0"):
                continue

            target_base = deployable_base_total * level_weight
            target_base = min(target_base, remaining_base_budget)

            if target_base * price < self.config.min_order_quote:
                continue

            action, used_base = self._build_sell_executor_action(
                idx=idx,
                level_id=level_id,
                price=price,
                order_base=target_base,
                remaining_base_before=remaining_base_budget,
                kept_sell_indexes=kept_sell_indexes,
                passive_execution_strategy=passive_execution_strategy,
            )

            if action is None and self.config.allow_partial_levels and not pending_indexes and (remaining_base_budget * price) >= self.config.min_order_quote:
                action, used_base = self._build_sell_executor_action(
                    idx=idx,
                    level_id=level_id,
                    price=price,
                    order_base=remaining_base_budget,
                    remaining_base_before=remaining_base_budget,
                    kept_sell_indexes=kept_sell_indexes,
                    passive_execution_strategy=passive_execution_strategy,
                )

            if action is None:
                continue

            actions.append(action)
            remaining_base_budget = max(Decimal("0"), remaining_base_budget - used_base)

        return actions

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        if not self.processed_data:
            return []

        if not self.processed_data.get("market_data_ready", True) or not self.processed_data.get("initialization_ready", True):
            return []

        if self._market_data_hard_pause or self._session_expired:
            return []

        if self._config_rebuild_pending and self._active_order_executors():
            self._emit_structured(
                "range_ladder_rebuild_waiting_for_cancels",
                active_order_executors=len(self._active_order_executors()),
                rebuild_reason=self._config_rebuild_reason,
            )
            return []

        # Post-refresh settle gate: after a refresh cancel wave, let the exchange
        # release collateral before we try to place new orders. When disabled
        # (post_refresh_settle_seconds=0), _refresh_quiet_until stays at 0 and
        # this check is always a no-op.
        now = self.market_data_provider.time()
        if now < self._refresh_quiet_until:
            self._emit_structured(
                "range_ladder_create_blocked_post_refresh_settle",
                remaining_s=round(self._refresh_quiet_until - now, 3),
            )
            return []

        actions: List[CreateExecutorAction] = []
        buy_actions = self._create_buy_actions()
        sell_actions = self._create_sell_actions()
        actions.extend(buy_actions)
        actions.extend(sell_actions)

        # Safety-net invariant (2026-07-08 addendum): issued creates must fit the budget.
        if buy_actions:
            issued_notional = sum(
                (a.executor_config.amount * a.executor_config.price for a in buy_actions),
                Decimal("0"),
            )
            self._check_plan_budget_invariant(
                "buy", issued_notional,
                self.processed_data.get("free_buy_budget_quote", Decimal("0")), "issued_creates")
        if sell_actions:
            issued_base = sum((a.executor_config.amount for a in sell_actions), Decimal("0"))
            self._check_plan_budget_invariant(
                "sell", issued_base,
                self.processed_data.get("free_sell_budget_base", Decimal("0")), "issued_creates")

        # Per-side model: a side's refresh is complete once its rebuild has been ISSUED -- clear
        # its dirty flag so it is not rebuilt again next cycle (no self-trigger loop). We treat a
        # build as issued when it produced actions OR there is genuinely nothing to place (dust /
        # no eligible rung). If the planner still wants orders but none were placed this cycle
        # (e.g. the just-cancelled levels are still SHUTTING_DOWN and thus blocked), the side stays
        # dirty and retries next cycle. Sides that were deferred this cycle keep their dirty flag.
        # Issued creates are noted on the wave record (fix 4): if the orchestrator drops them on a
        # stop/create conflict, _reconcile_refresh_waves re-proposes them next cycle.
        if self.config.event_refresh_enabled:
            if self._buy_side_dirty and not self._defer_buy_creates_this_cycle:
                if buy_actions:
                    self._note_side_creates_issued("buy", buy_actions, now)
                    self._buy_side_dirty = False
                    self._buy_dirty_reason = ""
                elif not self._plan_buy_book():
                    self._buy_side_dirty = False
                    self._buy_dirty_reason = ""
                    self._refresh_wave["buy"] = None  # nothing to place -> the wave is over
            if self._sell_side_dirty and not self._defer_sell_creates_this_cycle:
                if sell_actions:
                    self._note_side_creates_issued("sell", sell_actions, now)
                    self._sell_side_dirty = False
                    self._sell_dirty_reason = ""
                elif not self._plan_sell_book():
                    self._sell_side_dirty = False
                    self._sell_dirty_reason = ""
                    self._refresh_wave["sell"] = None  # nothing to place -> the wave is over
        else:
            # LEGACY: creates run every cycle; note issuance so the wave record resolves once
            # the re-placed orders are live (its budget credit then retires promptly).
            if buy_actions and self._refresh_wave.get("buy") is not None:
                self._note_side_creates_issued("buy", buy_actions, now)
            if sell_actions and self._refresh_wave.get("sell") is not None:
                self._note_side_creates_issued("sell", sell_actions, now)

        if not actions:
            self._emit_structured(
                "range_ladder_noop_cycle",
                blocked_levels=",".join(sorted(self.processed_data["blocked_level_ids"])),
                free_buy_budget_quote=str(self.processed_data["free_buy_budget_quote"]),
                free_sell_budget_base=str(self.processed_data["free_sell_budget_base"]),
                price_regime=str(self.processed_data.get("price_regime", "")),
                session_expired=str(self._session_expired),
                market_data_hard_pause=str(self._market_data_hard_pause),
            )
        return actions

    def _append_dirty_side_cancels(self, now: float, actions: List[StopExecutorAction],
                                   active_order_executors: List[ExecutorInfo]) -> bool:
        """Per-side refresh APPLY step (event_refresh_enabled=True). For each DIRTY side:
          - No-op / dust guard: if a fresh rebuild would reproduce the resting book (e.g. nothing
            changed, or a sub-min_order_quote freed amount can't fund a new rung), clear the dirty
            flag WITHOUT cancelling -- no churn.
          - Otherwise cancel ALL resting executors on that side, bypass-marking each level so the
            cancel starts no residual cooldown. The create path rebuilds the side from its current
            managed budget next cycle (cancel cycle N -> recreate cycle N+1 via the per-side defer).
        Returns whether any stop was emitted. Skips entirely on a non-ready cycle so a market-data
        blip never cancels the book against stale/empty processed_data.
        """
        if (not self.processed_data
                or not self.processed_data.get("market_data_ready", True)
                or not self.processed_data.get("initialization_ready", True)):
            return False

        stopped = False
        for side, dirty_attr, reason_attr in (
            (TradeType.BUY, "_buy_side_dirty", "_buy_dirty_reason"),
            (TradeType.SELL, "_sell_side_dirty", "_sell_dirty_reason"),
        ):
            if not getattr(self, dirty_attr):
                continue
            reason = getattr(self, reason_attr) or "refresh"
            side_name = "buy" if side == TradeType.BUY else "sell"

            if reason == "ladder_reconcile":
                # Heal mode (intended-vs-live reconciliation): NEVER cancel the surviving
                # orders. Their levels are blocked, so the create path fills only the
                # missing rungs, sized from the current effective budget.
                continue

            if self._side_refresh_converged(side):
                # The resting book already equals what a rebuild would place -> nothing to do.
                setattr(self, dirty_attr, False)
                setattr(self, reason_attr, "")
                self._emit_structured(
                    "range_ladder_side_refresh_skipped",
                    side=side_name, reason=reason, guard="noop_or_dust",
                )
                continue

            resting = [e for e in active_order_executors if self._executor_side(e) == side]
            if not resting:
                # Nothing to cancel; the create path (re)builds this side this cycle and then
                # clears the dirty flag (initial placement / post-cancel follow-through).
                continue

            budget = (self._side_rebuild_budget_quote() if side == TradeType.BUY
                      else self._side_rebuild_budget_base())
            planned = self._plan_buy_book() if side == TradeType.BUY else self._plan_sell_book()

            # Safety-net invariant (2026-07-08 addendum): the plan must fit the budget.
            if side == TradeType.BUY:
                level_prices = {self._buy_level_id(i): p for i, p in enumerate(self.config.buy_prices)}
                planned_total = sum(
                    (amount * level_prices.get(level_id, Decimal("0"))
                     for level_id, amount in planned.items()),
                    Decimal("0"),
                )
            else:
                planned_total = sum(planned.values(), Decimal("0"))
            self._check_plan_budget_invariant(side_name, planned_total, budget, "side_refresh_plan")

            # Compression guard (fix 5): never cancel healthy orders into an EMPTY
            # replacement. An empty candidate set while the effective rebuild budget could
            # fund at least one level means every rung is blocked/filtered or the sizing is
            # off -- keep the resting book and abort this side's refresh instead of
            # deadlocking with zero orders (the 2026-07-07 failure cancelled 8 live sells
            # against kept_levels=[]).
            if not planned:
                reference_price = self._d(self.processed_data.get("reference_price", "0"), "0")
                budget_notional = budget if side == TradeType.BUY else budget * reference_price
                if budget_notional >= Decimal(self.config.min_order_quote):
                    setattr(self, dirty_attr, False)
                    setattr(self, reason_attr, "")
                    p = self.processed_data
                    self.logger().warning(
                        f"{self.config.id}: aborting {side_name} refresh ({reason}): the rebuild "
                        f"kept ZERO levels while {len(resting)} live order(s) rest and the "
                        f"effective budget could fund at least one level -- keeping the existing "
                        f"orders. rebuild_budget={budget} budget_notional={budget_notional} "
                        f"free_buy_budget_quote={p.get('free_buy_budget_quote')} "
                        f"free_sell_budget_base={p.get('free_sell_budget_base')} "
                        f"min_order_quote={self.config.min_order_quote}"
                    )
                    self._emit_structured(
                        "range_ladder_side_refresh_aborted_empty_plan",
                        side=side_name,
                        reason=reason,
                        resting_levels=len(resting),
                        rebuild_budget=str(budget),
                        budget_notional=str(budget_notional),
                        free_buy_budget_quote=str(p.get("free_buy_budget_quote", Decimal("0"))),
                        free_sell_budget_base=str(p.get("free_sell_budget_base", Decimal("0"))),
                        min_order_quote=str(self.config.min_order_quote),
                    )
                    continue

            # Refresh-wave record (fix 1): fix the cancelled reservations BEFORE the stops go
            # out so the rebuild's budget credits them deterministically from the controller's
            # own ledger -- never waiting on exchange balance updates.
            self._record_refresh_wave_cancels(side, resting, now)
            released = self._refresh_wave[side_name]["released"]
            free_now = (self.processed_data.get("free_buy_budget_quote", Decimal("0"))
                        if side == TradeType.BUY
                        else self.processed_data.get("free_sell_budget_base", Decimal("0")))
            self.logger().info(
                f"{self.config.id}: refresh budget ({side_name}): free={free_now} + "
                f"released_reservations={released} = effective={budget}"
            )

            for executor in resting:
                level_id = getattr(executor.config, "level_id", "")
                self._mark_bypass_cooldown_for_level(level_id)
                actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                stopped = True
            # Dirty flag stays set: the rebuild lands next cycle (creates are deferred on a side
            # that has stops this cycle), and create_actions_proposal clears it once issued.
            self._emit_structured(
                "range_ladder_side_refresh",
                side=side_name,
                reason=reason,
                budget=str(budget),
                anchor_rung=self._nearest_eligible_rung(side),
                levels_cancelled=len(resting),
                levels_created=len(planned),
            )
        return stopped

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        actions: List[StopExecutorAction] = []
        active_order_executors = self._active_order_executors()

        if self._market_data_hard_pause and self.config.cancel_orders_on_market_data_hard_pause:
            if active_order_executors:
                for executor in active_order_executors:
                    level_id = getattr(executor.config, "level_id", "")
                    self._mark_bypass_cooldown_for_level(level_id)
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                    self._emit_structured(
                        "range_ladder_market_data_hard_pause_stop",
                        executor_id=executor.id,
                        level_id=level_id,
                    )
                return actions

        if self._session_expired and self.config.cancel_orders_on_session_end:
            if active_order_executors:
                for executor in active_order_executors:
                    level_id = getattr(executor.config, "level_id", "")
                    self._mark_bypass_cooldown_for_level(level_id)
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                    self._emit_structured(
                        "range_ladder_session_expired_stop",
                        executor_id=executor.id,
                        level_id=level_id,
                        reason=self._session_expired_reason,
                    )
                return actions

        if self._config_rebuild_pending:
            if active_order_executors:
                for executor in active_order_executors:
                    level_id = getattr(executor.config, "level_id", "")
                    self._mark_bypass_cooldown_for_level(level_id)
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                    self._emit_structured(
                        "range_ladder_runtime_config_rebuild_stop",
                        executor_id=executor.id,
                        level_id=level_id,
                        rebuild_reason=self._config_rebuild_reason,
                    )
                return actions

            if self._pending_runtime_config_signature is not None:
                self._applied_runtime_config_signature = self._pending_runtime_config_signature
            self._pending_runtime_config_signature = None
            self._config_rebuild_pending = False
            self._config_rebuild_reason = ""
            self.logger().info(
                f"{self.config.id}: runtime ladder config rebuild is clear to recreate fresh orders."
            )
            self._emit_structured(
                "range_ladder_runtime_config_rebuild_ready",
                connector=self.config.connector_name,
                trading_pair=self.config.trading_pair,
            )

        # Refresh policy: cancel and recreate resting orders to track current prices/budgets.
        now = self.market_data_provider.time()

        # Targeted dust stops queued by the intended-vs-live reconciliation: cancel live
        # orders the budget preflight resized below preflight_min_fill_ratio so their levels
        # can be retried at full size (a dust order burns its level and defeats the
        # under-placement detection).
        if self._reconcile_stop_ids:
            for executor in active_order_executors:
                if executor.id in self._reconcile_stop_ids:
                    level_id = getattr(executor.config, "level_id", "")
                    self._mark_bypass_cooldown_for_level(level_id)
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                    self._emit_structured(
                        "range_ladder_reconcile_dust_stop_issued",
                        executor_id=executor.id,
                        level_id=level_id,
                    )
            self._reconcile_stop_ids.clear()

        # Post-refresh settle gate: if we recently emitted refresh cancels, don't
        # emit more until the configured settle window expires. This gives the
        # exchange time to release collateral and balances to converge before
        # the next create wave.
        if now < self._refresh_quiet_until:
            return actions

        if self.config.event_refresh_enabled:
            # Per-side model: cancel the resting orders on whichever side(s) a trigger marked
            # dirty (fill cross-side, cooldown lapse, or the global timer), skipping the cancel
            # when the rebuild would reproduce the same book (no-op / dust guard).
            refresh_stopped_any = self._append_dirty_side_cancels(now, actions, active_order_executors)
        else:
            # LEGACY (event_refresh_enabled=False): per-executor-age refresh -- cancel any order
            # older than executor_refresh_time.
            refresh_stopped_any = False
            legacy_stopped: Dict[TradeType, List[ExecutorInfo]] = {TradeType.BUY: [], TradeType.SELL: []}
            for executor in active_order_executors:
                age = now - executor.timestamp
                if age >= self.config.executor_refresh_time:
                    # v12 Issue 2: a refresh cancel must NEVER start a cooldown -- nothing
                    # filled, the order is merely being re-priced. Bypass-mark the level before
                    # the stop (identical to the hard-pause, session-end, and config-rebuild
                    # stop branches) so _recently_closed_level_ids does not park it on cooldown.
                    self._mark_bypass_cooldown_for_level(getattr(executor.config, "level_id", ""))
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                    self._emit_structured(
                        "range_ladder_refresh_stop",
                        executor_id=executor.id,
                        level_id=getattr(executor.config, "level_id", ""),
                        age_s=round(age, 3),
                    )
                    refresh_stopped_any = True
                    executor_side = self._executor_side(executor)
                    if executor_side in legacy_stopped:
                        legacy_stopped[executor_side].append(executor)
            # Refresh-wave record (fix 1) for the legacy path: the re-place next cycle sizes
            # from the free budget, which must credit these cancels' reservations too.
            for legacy_side, stopped_executors in legacy_stopped.items():
                if stopped_executors:
                    self._record_refresh_wave_cancels(legacy_side, stopped_executors, now, accumulate=True)

        if refresh_stopped_any and self.config.post_refresh_settle_seconds > 0:
            self._refresh_quiet_until = now + self.config.post_refresh_settle_seconds
            self._emit_structured(
                "range_ladder_refresh_settle_window_opened",
                quiet_until_ts=self._refresh_quiet_until,
                settle_s=self.config.post_refresh_settle_seconds,
                stops_this_cycle=len([a for a in actions if hasattr(a, "executor_id")]),
            )

        return actions

    def to_format_status(self) -> List[str]:
        if not self.processed_data:
            return ["Controller not ready."]
        p = self.processed_data
        blocked = sorted(list(p["blocked_level_ids"]))
        lines = [
            "Strategy: fixed range inventory ladder",
            f"Pair: {self.config.connector_name} {self.config.trading_pair}",
            f"Mid / Bid / Ask: {p['reference_price']:.6f} / {p['best_bid']:.6f} / {p['best_ask']:.6f}",
            f"Price regime: {p['price_regime']} | Config rebuild pending: {p['config_rebuild_pending']}",
            f"Market data ready: {p.get('market_data_ready', True)} | Hard pause: {p.get('market_data_hard_pause', False)}",
            f"Initialization ready: {p.get('initialization_ready', True)} | Session expired: {p.get('session_expired', False)}",
            f"Wallet avail / total: {p['available_quote_balance']:.6f} / {p['total_quote_balance']:.6f} {p['quote_asset']} | {p['available_base_balance']:.6f} / {p['total_base_balance']:.6f} {p['base_asset']}",
            f"Managed quote / base: {p['managed_quote_total']:.6f} {p['quote_asset']} / {p['managed_base_total']:.6f} {p['base_asset']}",
            f"Managed fund value: {p['managed_fund_value_quote']:.6f} {p['quote_asset']} | Throttle factor: {p['cap_factor']:.6f}",
            f"Seed value / Deploy ceiling: {p.get('seed_value_quote', Decimal('0')):.6f} / {p.get('deploy_ceiling_quote', Decimal('0')):.6f} {p['quote_asset']} (cap {self.config.max_fund_value_quote})",
            f"Refresh model: {'event / per-side' if p.get('event_refresh_enabled', self.config.event_refresh_enabled) else 'legacy / per-executor-age'} | "
            f"global refresh in {p.get('global_refresh_remaining_s', 0.0):.0f}s (every {self.config.executor_refresh_time}s)",
            f"Buy cooldown {p.get('buy_cooldown_time', self.config.effective_buy_cooldown_time)}s "
            f"({'lapses in %.0fs' % p['buy_cooldown_remaining_s'] if p.get('buy_cooldown_armed') and p.get('buy_cooldown_remaining_s') is not None else 'idle'}) | "
            f"dirty={p.get('buy_side_dirty', False)}{(' (%s)' % p.get('buy_dirty_reason')) if p.get('buy_side_dirty') and p.get('buy_dirty_reason') else ''}",
            f"Sell cooldown {p.get('sell_cooldown_time', self.config.effective_sell_cooldown_time)}s "
            f"({'lapses in %.0fs' % p['sell_cooldown_remaining_s'] if p.get('sell_cooldown_armed') and p.get('sell_cooldown_remaining_s') is not None else 'idle'}) | "
            f"dirty={p.get('sell_side_dirty', False)}{(' (%s)' % p.get('sell_dirty_reason')) if p.get('sell_side_dirty') and p.get('sell_dirty_reason') else ''}",
            f"Deployable quote / base: {p['deployable_quote_total']:.6f} {p['quote_asset']} / {p['deployable_base_total']:.6f} {p['base_asset']}",
            f"Funding mode: {'ledger (own owned_*_free, wallet-floored)' if p.get('ledger_funded_budgets', self.config.ledger_funded_budgets) else 'legacy (raw wallet)'} | "
            f"owned_free q/b: {p.get('owned_quote_free', Decimal('0')):.6f} / {p.get('owned_base_free', Decimal('0')):.6f} | "
            f"wallet avail q/b: {p.get('available_quote_balance', Decimal('0')):.6f} / {p.get('available_base_balance', Decimal('0')):.6f}",
            f"Free buy budget: {p['free_buy_budget_quote']:.6f} {p['quote_asset']}",
            f"Shared-account quote quota: "
            f"{('none' if self.config.shared_account_quote_quota is None else f'{self.config.shared_account_quote_quota:.6f}')} "
            f"{p['quote_asset']}",
            f"Free sell inventory: {p['free_sell_budget_base']:.6f} {p['base_asset']}",
            (
                f"Post-refresh settle: active for {round(self._refresh_quiet_until - self.market_data_provider.time(), 1)}s more"
                if self._refresh_quiet_until > self.market_data_provider.time()
                else (
                    f"Post-refresh settle: idle (configured={self.config.post_refresh_settle_seconds}s)"
                    if self.config.post_refresh_settle_seconds > 0
                    else "Post-refresh settle: disabled"
                )
            ),
            f"Inventory PnL (real/unreal/global): {p['inventory_realized_pnl_quote']:.6f} / "
            f"{p['inventory_unrealized_pnl_quote']:.6f} / {p['inventory_global_pnl_quote']:.6f} {p['quote_asset']}",
            f"Inventory fees: {p['inventory_cum_fees_quote']:.6f} {p['quote_asset']} | "
            f"Net base held: {p['inventory_net_base_amount']:.6f} {p['base_asset']}",
            f"Fund growth since init: {p['fund_growth_quote']:.6f} {p['quote_asset']} | "
            f"Reconciliation gap: {p['reconciliation_gap_quote']:.6f} {p['quote_asset']}",
            f"Reserved wallet balances: {p['reserve_quote_balance']:.6f} {p['quote_asset']} / {p['reserve_base_balance']:.6f} {p['base_asset']}",
            f"State file: {self.state_path} | Schema: {self.STATE_SCHEMA_VERSION}",
            f"Diagnostic log: {self.diagnostic_log_path if self.config.diagnostic_log_enabled else 'disabled'}",
            f"Session elapsed: {p.get('session_elapsed_s', Decimal('0')):.3f}s | Max session hours: {self.config.max_session_duration_hours}",
        ]
        if not p.get("market_data_ready", True):
            lines.append(f"Market data error: {p.get('market_data_error', '')}")
        if p.get("market_data_hard_pause", False):
            lines.append(
                f"Market data unavailable for: {p.get('market_data_unavailable_duration_s', Decimal('0')):.3f}s "
                f"(threshold {self.config.max_market_data_unavailable_seconds}s)"
            )
        if not p.get("initialization_ready", True):
            lines.append(f"Initialization blocked: {p.get('initialization_blocked_reason', '')}")
        if p.get("session_expired", False):
            lines.append(f"Session expired reason: {p.get('session_expired_reason', '')}")
        if self._state_migrated_from_version is not None:
            lines.append(f"State migrated from schema version: {self._state_migrated_from_version}")
        lines.append(
            f"Tracked held positions: {len(self.positions_held or [])} | Active executors: {len(self._active_order_executors())}"
        )
        lines.append(f"Reservation sources buy: {self._buy_reservation_sources} | sell: {self._sell_reservation_sources}")
        if blocked:
            lines.append(f"Blocked/cooldown levels: {', '.join(blocked)}")
        else:
            lines.append("Blocked/cooldown levels: none")
        # Show eligible price levels based on current bid/ask.
        # v12 Issue 4: take the level index directly via enumerate -- never look it up by
        # value (buy_prices.index(price)), which is O(n^2) and silently wrong with dup prices.
        eligible_buys = [
            str(price) for idx, price in enumerate(self.config.buy_prices)
            if self._can_place_buy_level(price)
            and self._buy_level_id(idx) not in p["blocked_level_ids"]
        ]
        eligible_sells = [
            str(price) for idx, price in enumerate(self.config.sell_prices)
            if self._can_place_sell_level(price)
            and self._sell_level_id(idx) not in p["blocked_level_ids"]
        ]
        lines.append(f"Eligible buy prices: {', '.join(eligible_buys) if eligible_buys else 'none'}")
        lines.append(f"Eligible sell prices: {', '.join(eligible_sells) if eligible_sells else 'none'}")
        return lines

    def get_custom_info(self) -> dict:
        if not self.processed_data:
            return {}
        p = self.processed_data
        return {
            "strategy": "range_inventory_ladder",
            "connector": self.config.connector_name,
            "trading_pair": self.config.trading_pair,
            "reference_price": str(p["reference_price"]),
            "managed_quote_total": str(p["managed_quote_total"]),
            "managed_base_total": str(p["managed_base_total"]),
            "managed_fund_value_quote": str(p["managed_fund_value_quote"]),
            "cap_factor": str(p["cap_factor"]),
            "seed_value_quote": str(p.get("seed_value_quote", Decimal("0"))),
            "deploy_ceiling_quote": str(p.get("deploy_ceiling_quote", Decimal("0"))),
            "deploy_headroom_quote": str(p.get("deploy_headroom_quote", Decimal("0"))),
            "max_fund_value_quote": str(self.config.max_fund_value_quote),
            "event_refresh_enabled": str(p.get("event_refresh_enabled", self.config.event_refresh_enabled)),
            "buy_cooldown_time": str(p.get("buy_cooldown_time", self.config.effective_buy_cooldown_time)),
            "sell_cooldown_time": str(p.get("sell_cooldown_time", self.config.effective_sell_cooldown_time)),
            "buy_cooldown_armed": str(p.get("buy_cooldown_armed", False)),
            "sell_cooldown_armed": str(p.get("sell_cooldown_armed", False)),
            "buy_cooldown_remaining_s": str(p.get("buy_cooldown_remaining_s")),
            "sell_cooldown_remaining_s": str(p.get("sell_cooldown_remaining_s")),
            "buy_side_dirty": str(p.get("buy_side_dirty", False)),
            "sell_side_dirty": str(p.get("sell_side_dirty", False)),
            "buy_dirty_reason": str(p.get("buy_dirty_reason", "")),
            "sell_dirty_reason": str(p.get("sell_dirty_reason", "")),
            "last_global_refresh_ts": str(p.get("last_global_refresh_ts", 0.0)),
            "global_refresh_remaining_s": str(p.get("global_refresh_remaining_s", 0.0)),
            "deployable_quote_total": str(p["deployable_quote_total"]),
            "deployable_base_total": str(p["deployable_base_total"]),
            "free_buy_budget_quote": str(p["free_buy_budget_quote"]),
            "free_sell_budget_base": str(p["free_sell_budget_base"]),
            "buy_fee_headroom_quote": str(p.get("buy_fee_headroom_quote", Decimal("0"))),
            "ledger_surplus_quote": str(p.get("ledger_surplus_quote", Decimal("0"))),
            "refresh_wave_release_buy_quote": str(p.get("refresh_wave_release_buy_quote", Decimal("0"))),
            "refresh_wave_release_sell_base": str(p.get("refresh_wave_release_sell_base", Decimal("0"))),
            "ledger_funded_budgets": str(p.get("ledger_funded_budgets", self.config.ledger_funded_budgets)),
            "owned_quote_free": str(p.get("owned_quote_free", Decimal("0"))),
            "owned_base_free": str(p.get("owned_base_free", Decimal("0"))),
            "available_quote_balance": str(p.get("available_quote_balance", "0")),
            "available_base_balance": str(p.get("available_base_balance", "0")),
            "total_quote_balance": str(p.get("total_quote_balance", "0")),
            "total_base_balance": str(p.get("total_base_balance", "0")),
            "reserve_quote_balance": str(p["reserve_quote_balance"]),
            "reserve_base_balance": str(p["reserve_base_balance"]),
            "inventory_realized_pnl_quote": str(p["inventory_realized_pnl_quote"]),
            "inventory_unrealized_pnl_quote": str(p["inventory_unrealized_pnl_quote"]),
            "inventory_cum_fees_quote": str(p["inventory_cum_fees_quote"]),
            "inventory_global_pnl_quote": str(p["inventory_global_pnl_quote"]),
            "inventory_net_base_amount": str(p["inventory_net_base_amount"]),
            "inventory_abs_notional_quote": str(p["inventory_abs_notional_quote"]),
            "initial_fund_value_quote": str(p["initial_fund_value_quote"]),
            "fund_growth_quote": str(p["fund_growth_quote"]),
            "reconciliation_gap_quote": str(p["reconciliation_gap_quote"]),
            "state_file": str(self.state_path),
            "state_schema_version": str(self.STATE_SCHEMA_VERSION),
            "state_recovery_reason": self._state_recovery_reason or "",
            "state_migrated_from_version": str(self._state_migrated_from_version) if self._state_migrated_from_version is not None else "",
            "price_regime": str(p.get("price_regime", "")),
            "config_rebuild_pending": str(self._config_rebuild_pending),
            "market_data_ready": str(p.get("market_data_ready", True)),
            "market_data_error": p.get("market_data_error", ""),
            "market_data_unavailable_duration_s": str(p.get("market_data_unavailable_duration_s", Decimal("0"))),
            "market_data_hard_pause": str(p.get("market_data_hard_pause", False)),
            "initialization_ready": str(p.get("initialization_ready", True)),
            "initialization_blocked_reason": p.get("initialization_blocked_reason", ""),
            "session_elapsed_s": str(p.get("session_elapsed_s", Decimal("0"))),
            "session_expired": str(p.get("session_expired", False)),
            "session_expired_reason": p.get("session_expired_reason", ""),
            "diagnostic_log_enabled": str(self.config.diagnostic_log_enabled),
            "diagnostic_log_path": str(self.diagnostic_log_path),
            "diagnostic_heartbeat_interval_seconds": str(self.config.diagnostic_heartbeat_interval_seconds),
            "buy_prices": [str(price) for price in self.config.buy_prices],
            "sell_prices": [str(price) for price in self.config.sell_prices],
            "blocked_level_ids": sorted(list(p["blocked_level_ids"])),
            "active_order_executors": len(self._active_order_executors()),
            "tracked_positions": len(self.positions_held or []),
            "reservation_sources_buy": self._buy_reservation_sources,
            "reservation_sources_sell": self._sell_reservation_sources,
            "timestamp_ms": int(self.market_data_provider.time() * 1e3),
        }
