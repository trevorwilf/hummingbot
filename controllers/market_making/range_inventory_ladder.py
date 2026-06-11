
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
        default=Decimal("1000"),
        json_schema_extra={
            "prompt": "Enter the hard cap for total deployable fund value in quote (e.g. 1000): ",
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
            "prompt": "Refresh/cancel stale working orders after how many seconds? ",
            "prompt_on_new": True,
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
    cooldown_time: int = Field(
        default=30,
        json_schema_extra={
            "prompt": "Cooldown after a level closes before recreating it (seconds): ",
            "prompt_on_new": True,
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
        mode="before",
    )
    @classmethod
    def parse_decimals(cls, value, validation_info: ValidationInfo):
        if isinstance(value, str):
            value = value.strip()
        return _safe_decimal(value, validation_info.field_name, default="0")

    @field_validator("executor_refresh_time", "cooldown_time", "max_market_data_unavailable_seconds", "diagnostic_heartbeat_interval_seconds", mode="before")
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
    """

    STATE_SCHEMA_VERSION = 10
    SUPPORTED_STATE_SCHEMA_VERSIONS = {6, 7, 8, 9, 10}
    STATE_MAX_FUTURE_SKEW_SECONDS = Decimal("86400")  # 1 day
    INITIALIZATION_UNAVAILABLE_BALANCE_TOLERANCE = Decimal("0.00000001")


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


    @property
    def state_path(self) -> Path:
        file_name = self.config.state_file_name or f"range_inventory_ladder_{self.config.id}.json"
        return Path("data") / file_name

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

    def _write_diagnostic_event(self, event_type: str, **payload):
        if not self.config.diagnostic_log_enabled:
            return
        try:
            self.diagnostic_log_path.parent.mkdir(parents=True, exist_ok=True)
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
            f"max_market_data_unavailable_seconds={self.config.max_market_data_unavailable_seconds}"
        )
        self._emit_structured(
            "range_ladder_live_runtime_settings_updated",
            previous_signature=list(previous_signature),
            current_signature=list(current_signature),
            executor_refresh_time=self.config.executor_refresh_time,
            cooldown_time=self.config.cooldown_time,
            max_session_duration_hours=self.config.max_session_duration_hours,
            max_market_data_unavailable_seconds=self.config.max_market_data_unavailable_seconds,
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
        self._cooldown_bypass_until_by_level[level_id] = (
            self.market_data_provider.time() + max(1, self.config.cooldown_time + 1)
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

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, sort_keys=True)
            os.replace(tmp_path, str(self.state_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise


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
        # total_amount_quote -- it is a SUBSET, not additive. claimed_base_value_quote
        # (or claimed_base_amount) is a one-time STARTING seed only; after seeding it
        # never influences deployment again (deployment tracks the live wallet).
        #
        #   base_seed_value  = min(available_base * ref, claimed_base_value_quote)
        #   base_seed_amount = base_seed_value / ref   (quantized)
        #   quote_seed       = min(available_quote, total_amount_quote - base_seed_value)
        #   total seed value = quote_seed + base_seed_value  <=  total_amount_quote
        base_seed_value = Decimal("0")
        claimed_base_amount = Decimal("0")
        claim_source = "none"
        if self.config.use_wallet_balance and reference_price > Decimal("0"):
            available_base_value = available_base_balance * reference_price
            if self.config.claimed_base_amount is not None and self.config.claimed_base_amount > Decimal("0"):
                # Explicit base amount claim, still bounded as a subset of total_amount_quote.
                desired_base_value = min(available_base_balance, self.config.claimed_base_amount) * reference_price
                base_seed_value = min(desired_base_value, Decimal(self.config.total_amount_quote))
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
            max(Decimal("0"), Decimal(self.config.total_amount_quote) - base_seed_value),
        )
        managed_quote_claim = quote_seed
        seed_value_quote = quote_seed + base_seed_value

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

    def _update_ledger_from_completed_executors(self):
        """Scan executors for newly completed fills and update owned_quote/owned_base."""
        if not self._state.get("initialized"):
            return

        tracked_executor_ids = set(self._state.get("tracked_fill_executor_ids", []))
        owned_quote = self._d(self._state.get("owned_quote"), "0")
        owned_base = self._d(self._state.get("owned_base"), "0")
        changed = False

        # Prune tracked IDs for executors no longer in executors_info
        current_executor_ids = {e.id for e in self.executors_info}
        stale_ids = tracked_executor_ids - current_executor_ids
        if stale_ids:
            tracked_executor_ids -= stale_ids

        for executor in self.executors_info:
            if executor.id in tracked_executor_ids:
                continue
            if executor.is_active:
                continue  # not done yet
            level_id = getattr(executor.config, "level_id", None)
            if level_id is None:
                continue  # not ours

            # Prefer the executor's exact fill accounting published via custom_info.
            # OrderExecutor exposes filled_amount_base/quote + cum_fees_quote there because
            # its public filled_amount_quote property is hardcoded to 0 (POSITION_HOLD
            # semantics, co-designed with the orchestrator). Fall back to the legacy public
            # fields only for other executor types or state written by older runs.
            info = getattr(executor, "custom_info", {}) or {}
            side = getattr(executor.config, "side", None) or info.get("side")
            config_price = self._d(getattr(executor.config, "price", "0") or "0")

            filled_quote = self._d(info.get("filled_amount_quote"), "0")
            filled_base = self._d(info.get("filled_amount_base"), "0")
            fees = self._d(info.get("cum_fees_quote"), "0")

            if filled_quote <= Decimal("0") and filled_base <= Decimal("0"):
                # legacy fallback: derive base from the public fields + config price
                filled_quote = max(Decimal("0"), executor.filled_amount_quote)
                fees = max(Decimal("0"), executor.cum_fees_quote)
                if filled_quote > Decimal("0") and config_price > Decimal("0"):
                    filled_base = filled_quote / config_price

            if filled_quote <= Decimal("0") and filled_base <= Decimal("0"):
                tracked_executor_ids.add(executor.id)
                continue  # genuinely no fill

            # Clamp to non-negative in case of malformed custom_info / fallback data.
            filled_quote = max(Decimal("0"), filled_quote)
            filled_base = max(Decimal("0"), filled_base)
            fees = max(Decimal("0"), fees)

            if side == TradeType.BUY:
                owned_quote -= (filled_quote + fees)
                owned_base += filled_base
                changed = True
            elif side == TradeType.SELL:
                owned_base -= filled_base
                owned_quote += (filled_quote - fees)
                changed = True

            tracked_executor_ids.add(executor.id)

        if changed:
            owned_quote = max(Decimal("0"), owned_quote)
            owned_base = max(Decimal("0"), owned_base)
            self._state["owned_quote"] = str(owned_quote)
            self._state["owned_base"] = str(owned_base)
            self._state["tracked_fill_executor_ids"] = sorted(list(tracked_executor_ids))
            self._save_state()
            self.logger().info(
                f"{self.config.id}: ledger updated from fills. "
                f"owned_quote={owned_quote} owned_base={owned_base}"
            )
            self._emit_structured(
                "range_ladder_ledger_updated",
                owned_quote=str(owned_quote),
                owned_base=str(owned_base),
                tracked_fill_executor_ids=len(tracked_executor_ids),
            )
        elif stale_ids:
            # Save pruned list even if no fill changes
            self._state["tracked_fill_executor_ids"] = sorted(list(tracked_executor_ids))
            self._save_state()

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
        for executor in self.executors_info:
            level_id = getattr(executor.config, "level_id", None)
            if level_id is None:
                continue
            status = getattr(executor, "status", None)
            if status in live_statuses:
                blocked.add(level_id)
                continue
            if self._should_bypass_level_cooldown(level_id):
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
    ):
        """Size each side's deployable budget from the LIVE wallet, bounded by the ceiling.

        No fixed base/quote ratio after seed: the buy side deploys whatever quote is
        available, the sell side deploys whatever base is available. The COMBINED new
        deployed value plus what is already on the book is throttled (pro-rata) so the
        total deployed value never exceeds deploy_ceiling.

        Returns (free_buy_budget_quote, free_sell_budget_base, throttle_scale, headroom).
        """
        ref = max(Decimal("0"), reference_price)
        account_quote_cap = max(Decimal("0"), available_quote)
        if self.config.shared_account_quote_quota is not None:
            account_quote_cap = min(account_quote_cap, Decimal(self.config.shared_account_quote_quota))
        buy_budget_quote = account_quote_cap
        sell_budget_base = max(Decimal("0"), available_base)

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

        # Update owned ledger from completed executor fills
        try:
            self._update_ledger_from_completed_executors()
        except Exception as e:
            self.logger().exception(f"{self.config.id}: ledger update from fills failed")
            self._emit_structured("range_ladder_ledger_update_error", error=str(e))

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

        reserve_quote_balance = self._d(self._state.get("reserve_quote_balance"))
        reserve_base_balance = self._d(self._state.get("reserve_base_balance"))

        # Use controller-owned ledger instead of wallet-derived totals
        owned_quote = self._d(self._state.get("owned_quote"), "0")
        owned_base = self._d(self._state.get("owned_base"), "0")
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
        RECONCILIATION_ALERT_THRESHOLD_QUOTE = Decimal("0.5")
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
        if overclaim_above_threshold:
            if not self._last_drift_above_threshold:
                should_warn = True
            elif (now_ts - self._last_drift_warning_time) >= self._drift_warning_interval:
                should_warn = True
        self._last_drift_above_threshold = overclaim_above_threshold
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

        self._cycles_seen += 1

        active_buy_reserved_quote = self._active_reserved_quote_for_buys()
        active_sell_reserved_base = self._active_reserved_base_for_sells()

        # Self-balance deployment model:
        #   - The PnL ledger (owned_quote/owned_base) stays fills-only and invariant
        #     to deposits (managed_fund_value_quote above).
        #   - Deployment each cycle sizes from the LIVE available wallet balance,
        #     bounded by a growth ceiling that starts at the realized seed value and
        #     compounds with the managed fund value, hard-capped at max_fund_value_quote.
        #   - No fixed base/quote ratio after seed: each side deploys what it holds;
        #     the combined deployed value is throttled to the ceiling.
        # This makes idle reserve, later deposits, and fill proceeds all usable on the
        # next cycle without ever deleting the state file.
        seed_value_quote = self._seed_value_quote()
        deploy_ceiling = self._compute_deploy_ceiling(seed_value_quote, managed_fund_value_quote)

        free_buy_budget_quote, free_sell_budget_base, throttle_scale, deploy_headroom = self._compute_deploy_budgets(
            reference_price=reference_price,
            available_quote=available_quote_balance,
            available_base=available_base_balance,
            active_buy_reserved_quote=active_buy_reserved_quote,
            active_sell_reserved_base=active_sell_reserved_base,
            deploy_ceiling=deploy_ceiling,
        )

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
            "reservation_sources_buy": dict(self._buy_reservation_sources),
            "reservation_sources_sell": dict(self._sell_reservation_sources),
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
        self._emit_diagnostic_heartbeat_if_due()


    def _find_executor_by_id(self, executor_id: str):
        """Find an executor by its ID from the controller's executors."""
        for executor in self.executors_info:
            if executor.id == executor_id:
                return executor
        return None

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        stop_actions = self.stop_actions_proposal()
        actions.extend(stop_actions)

        # If we are stopping executors this cycle, defer create decisions until the next
        # cycle when budgets will reflect the freed capital. This prevents compression
        # from running against stale reservations from soon-to-be-cancelled orders.
        if stop_actions:
            has_buy_stops = any(
                self._executor_side(self._find_executor_by_id(a.executor_id)) == TradeType.BUY
                for a in stop_actions
                if hasattr(a, 'executor_id')
            )
            has_sell_stops = any(
                self._executor_side(self._find_executor_by_id(a.executor_id)) == TradeType.SELL
                for a in stop_actions
                if hasattr(a, 'executor_id')
            )
            if has_buy_stops or has_sell_stops:
                self._emit_structured(
                    "range_ladder_create_deferred_for_stops",
                    buy_stops=has_buy_stops,
                    sell_stops=has_sell_stops,
                    stop_count=len(stop_actions),
                )
                return actions

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
                notional = quantized_amount * quantized_price
                if quantized_amount <= Decimal("0") or notional < self.config.min_order_quote:
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
                notional = quantized_amount * quantized_price
                if quantized_amount <= Decimal("0") or notional < self.config.min_order_quote:
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
        if quantized_amount <= Decimal("0") or notional < self.config.min_order_quote:
            self._emit_structured(
                "range_ladder_buy_level_skipped_post_quantization",
                level_id=level_id,
                raw_price=str(price),
                quantized_price=str(quantized_price),
                raw_amount=str(amount),
                quantized_amount=str(quantized_amount),
                notional=str(notional),
                min_order_quote=str(self.config.min_order_quote),
                allocated_quote=str(order_quote),
                reason="quantized_amount_zero" if quantized_amount <= Decimal("0") else "notional_below_min",
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
        return CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config), notional

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
        if quantized_amount <= Decimal("0") or notional < self.config.min_order_quote:
            self._emit_structured(
                "range_ladder_sell_level_skipped_post_quantization",
                level_id=level_id,
                raw_price=str(price),
                quantized_price=str(quantized_price),
                raw_amount=str(order_base),
                quantized_amount=str(quantized_amount),
                notional=str(notional),
                min_order_quote=str(self.config.min_order_quote),
                allocated_base=str(order_base),
                reason="quantized_amount_zero" if quantized_amount <= Decimal("0") else "notional_below_min",
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
        return CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config), quantized_amount

    def _create_buy_actions(self) -> List[CreateExecutorAction]:
        actions: List[CreateExecutorAction] = []
        blocked_levels: Set[str] = self.processed_data["blocked_level_ids"]
        remaining_quote_budget = self.processed_data["free_buy_budget_quote"]

        eligible_buy_indexes: List[int] = []
        previous_filter_reasons = self._buy_level_filter_reasons
        current_filter_reasons: Dict[str, Optional[str]] = {}
        for idx, price in enumerate(self.config.buy_prices):
            level_id = self._buy_level_id(idx)
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
        actions: List[CreateExecutorAction] = []
        blocked_levels: Set[str] = self.processed_data["blocked_level_ids"]
        remaining_base_budget = self.processed_data["free_sell_budget_base"]

        eligible_sell_indexes: List[int] = []
        previous_filter_reasons = self._sell_level_filter_reasons
        current_filter_reasons: Dict[str, Optional[str]] = {}
        for idx, price in enumerate(self.config.sell_prices):
            level_id = self._sell_level_id(idx)
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
        actions.extend(self._create_buy_actions())
        actions.extend(self._create_sell_actions())
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

        # Refresh policy: cancel and recreate resting orders after executor_refresh_time seconds.
        # This ensures orders track the latest ladder prices and budget allocations.
        now = self.market_data_provider.time()

        # Post-refresh settle gate: if we recently emitted refresh cancels, don't
        # emit more until the configured settle window expires. This gives the
        # exchange time to release collateral and balances to converge before
        # the next create wave.
        if now < self._refresh_quiet_until:
            return actions

        refresh_stopped_any = False
        for executor in active_order_executors:
            age = now - executor.timestamp
            if age >= self.config.executor_refresh_time:
                actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
                self._emit_structured(
                    "range_ladder_refresh_stop",
                    executor_id=executor.id,
                    level_id=getattr(executor.config, "level_id", ""),
                    age_s=round(age, 3),
                )
                refresh_stopped_any = True

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
            f"Deployable quote / base: {p['deployable_quote_total']:.6f} {p['quote_asset']} / {p['deployable_base_total']:.6f} {p['base_asset']}",
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
        # Show eligible price levels based on current bid/ask
        eligible_buys = [
            str(price) for price in self.config.buy_prices
            if self._can_place_buy_level(price)
            and self._buy_level_id(self.config.buy_prices.index(price)) not in p["blocked_level_ids"]
        ]
        eligible_sells = [
            str(price) for price in self.config.sell_prices
            if self._can_place_sell_level(price)
            and self._sell_level_id(self.config.sell_prices.index(price)) not in p["blocked_level_ids"]
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
            "deployable_quote_total": str(p["deployable_quote_total"]),
            "deployable_base_total": str(p["deployable_base_total"]),
            "free_buy_budget_quote": str(p["free_buy_budget_quote"]),
            "free_sell_budget_base": str(p["free_sell_budget_base"]),
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
