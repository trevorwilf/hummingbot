# filename: controllers/_shared/trade_ledger.py
#
# CDX-011 / CLA-305 -- persisted, controller-owned trade-cap ledger.
#
# History-derived risk gates (max_trades_per_day, cooldowns) used to count
# `executors_info`, a bot-wide archival buffer that retains only the newest
# ~100 DONE executors (30 in the default v2_with_controllers runtime) and
# starts EMPTY on every restart. A 50-trade cap was structurally unreachable
# and a co-deployed churning controller could evict a quiet controller's
# history in minutes.
#
# This module gives each controller its own timestamped fill ledger:
#   - records only actually-FILLED executors (the buffer over-included
#     never-filled executors, which over-restricts a fills-based cap);
#   - accumulates since controller start, independent of buffer eviction;
#   - is written to disk (atomic write + fsync, the range_inventory_ladder
#     state-file discipline) so caps and cooldowns survive a bot restart.
#
# FAIL-SAFE IO (absolute contract): no read or write failure may ever raise
# into the control loop. Any IO error degrades to the in-memory behavior with
# a WARNING -- the ledger keeps accumulating in memory for the life of the
# process exactly as if persistence did not exist.

import json
import logging
import math
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Default directory for ledger files. Matches the range_inventory_ladder
# state-file convention of composing paths under the bot's data/ directory.
# Tests patch this module attribute to a per-test temporary directory.
DEFAULT_LEDGER_DIR = Path("data")

# Records older than this are pruned at observe time so the file stays small.
# Must comfortably exceed the largest history window any consumer queries
# (the daily trade cap looks back 24h; cooldowns look back minutes).
DEFAULT_RETENTION_SECONDS = 7 * 86400

# Hard bound on stored records (oldest dropped first) as a rotation-style
# backstop against pathological churn; dropping is logged, never silent.
MAX_RECORDS = 10_000

_SCHEMA_VERSION = 1
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_ledger_id(ledger_id: str) -> str:
    """Reduce an arbitrary controller id to a safe file-name fragment."""
    cleaned = _SANITIZE_RE.sub("_", str(ledger_id).strip())
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        cleaned = "default"
    return cleaned[:100]


def _as_float(value) -> float:
    """Best-effort finite float conversion; NaN/Inf/garbage collapse to 0.0."""
    try:
        if isinstance(value, Decimal):
            if not value.is_finite():
                return 0.0
            return float(value)
        result = float(value)
        if not math.isfinite(result):
            return 0.0
        return result
    except (TypeError, ValueError, ArithmeticError):
        return 0.0


def _normalize_side(value) -> Optional[str]:
    """Normalize a TradeType / string side to 'BUY' / 'SELL' (or None)."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.upper()
    if isinstance(value, str):
        stripped = value.strip().upper()
        return stripped or None
    return None


class TradeLedger:
    """Controller-owned persisted record of actually-filled trades.

    One instance per controller, keyed by the controller's config id. All
    public methods are exception-safe: they never raise into the caller.
    """

    def __init__(self,
                 ledger_id: str,
                 base_dir: Optional[Path] = None,
                 retention_seconds: float = DEFAULT_RETENTION_SECONDS,
                 logger: Optional[logging.Logger] = None):
        self._ledger_id = str(ledger_id)
        self._base_dir = Path(base_dir) if base_dir is not None else DEFAULT_LEDGER_DIR
        self._path = self._base_dir / f"trade_ledger_{_sanitize_ledger_id(self._ledger_id)}.json"
        self._retention_seconds = float(retention_seconds)
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        # executor_id -> {"executor_id", "timestamp", "side", "level_id", "amount_quote"}
        self._records: Dict[str, Dict] = {}
        self._io_failure_count = 0
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def io_failure_count(self) -> int:
        return self._io_failure_count

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #

    def observe_executors(self, executors: Optional[Iterable], now: float) -> int:
        """Record every executor in `executors` that shows a fill.

        Idempotent per executor id: an executor is counted once; a later
        observation with a LARGER filled amount re-stamps the record at the
        new observation time (partial-fill growth is trade activity, and
        stamping later is the conservative direction for caps/cooldowns).

        Returns the number of newly recorded executors. Never raises.
        """
        new_records = 0
        try:
            now = float(now)
            changed = False
            for executor in executors or []:
                fill = self._extract_fill(executor)
                if fill is None:
                    continue
                existing = self._records.get(fill["executor_id"])
                if existing is None:
                    fill["timestamp"] = now
                    self._records[fill["executor_id"]] = fill
                    new_records += 1
                    changed = True
                elif fill["amount_quote"] > existing["amount_quote"] + 1e-12:
                    existing["amount_quote"] = fill["amount_quote"]
                    existing["timestamp"] = now
                    changed = True
            if changed:
                self._prune(now)
                self._persist()
        except Exception as e:
            self._warn_io(f"trade ledger observe failed ({e}); continuing with in-memory records")
        return new_records

    @staticmethod
    def _extract_fill(executor) -> Optional[Dict]:
        """Return a ledger record for an executor that has filled, else None.

        Fill detection mirrors what the controllers can actually see: the
        public ExecutorInfo.filled_amount_quote, plus the custom_info variant
        some executor types (OrderExecutor POSITION_HOLD) report instead.
        """
        executor_id = getattr(executor, "id", None)
        if not isinstance(executor_id, str) or not executor_id:
            return None
        custom_info = getattr(executor, "custom_info", None)
        if not isinstance(custom_info, dict):
            custom_info = {}
        amount_quote = max(
            _as_float(getattr(executor, "filled_amount_quote", None)),
            _as_float(custom_info.get("filled_amount_quote")),
        )
        if amount_quote <= 0.0:
            return None
        side = _normalize_side(custom_info.get("side"))
        if side is None:
            side = _normalize_side(getattr(getattr(executor, "config", None), "side", None))
        level_id = custom_info.get("level_id")
        if not isinstance(level_id, str) or not level_id:
            level_id = getattr(getattr(executor, "config", None), "level_id", None)
            if not isinstance(level_id, str) or not level_id:
                level_id = None
        return {
            "executor_id": executor_id,
            "timestamp": 0.0,  # stamped by observe_executors
            "side": side,
            "level_id": level_id,
            "amount_quote": amount_quote,
        }

    def _prune(self, now: float):
        cutoff = now - self._retention_seconds
        stale = [key for key, record in self._records.items() if record["timestamp"] < cutoff]
        for key in stale:
            del self._records[key]
        if len(self._records) > MAX_RECORDS:
            overflow = len(self._records) - MAX_RECORDS
            oldest = sorted(self._records.values(), key=lambda r: r["timestamp"])[:overflow]
            for record in oldest:
                del self._records[record["executor_id"]]
            self._logger.warning(
                f"Trade ledger {self._path} exceeded {MAX_RECORDS} records; "
                f"dropped the {overflow} oldest. History older than the dropped "
                f"records no longer counts toward caps/cooldowns.")

    # ------------------------------------------------------------------ #
    # Queries (all exception-safe, fail toward 'no history')
    # ------------------------------------------------------------------ #

    def count_fills_since(self, cutoff: float, side=None) -> int:
        """Number of recorded fills with timestamp >= cutoff (optional side filter)."""
        try:
            side_name = _normalize_side(side)
            return sum(
                1 for record in self._records.values()
                if record["timestamp"] >= cutoff and (side_name is None or record["side"] == side_name)
            )
        except Exception as e:
            self._warn_io(f"trade ledger count query failed ({e}); returning 0")
            return 0

    def last_fill_timestamp(self, side=None, level_id: Optional[str] = None) -> float:
        """Most recent fill timestamp matching the filters; 0.0 when none."""
        try:
            side_name = _normalize_side(side)
            timestamps = [
                record["timestamp"] for record in self._records.values()
                if (side_name is None or record["side"] == side_name)
                and (level_id is None or record["level_id"] == level_id)
            ]
            return max(timestamps) if timestamps else 0.0
        except Exception as e:
            self._warn_io(f"trade ledger timestamp query failed ({e}); returning 0.0")
            return 0.0

    def level_ids_with_fills_since(self, cutoff: float) -> List[str]:
        """Distinct non-empty level_ids that filled at/after cutoff."""
        try:
            return sorted({
                record["level_id"] for record in self._records.values()
                if record["level_id"] and record["timestamp"] >= cutoff
            })
        except Exception as e:
            self._warn_io(f"trade ledger level query failed ({e}); returning []")
            return []

    def record_count(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------------ #
    # Persistence (fail-safe: never raises)
    # ------------------------------------------------------------------ #

    def _load(self):
        try:
            if not self._path.exists():
                return
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("ledger payload must be a JSON object")
            schema_version = raw.get("schema_version")
            if schema_version != _SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version {schema_version!r}")
            if raw.get("ledger_id") != self._ledger_id:
                # Another controller's data under our file name (sanitization
                # collision / copied file). Do not adopt foreign history and do
                # not quarantine what may be someone else's live ledger.
                self._logger.warning(
                    f"Trade ledger {self._path} belongs to ledger_id "
                    f"{raw.get('ledger_id')!r}, not {self._ledger_id!r}; ignoring its "
                    "contents and starting with in-memory records only.")
                return
            records = raw.get("records")
            if not isinstance(records, list):
                raise ValueError("ledger 'records' must be a list")
            loaded: Dict[str, Dict] = {}
            invalid = 0
            for record in records:
                normalized = self._normalize_loaded_record(record)
                if normalized is None:
                    invalid += 1
                    continue
                loaded[normalized["executor_id"]] = normalized
            if invalid:
                self._logger.warning(
                    f"Trade ledger {self._path}: skipped {invalid} invalid record(s) on load.")
            self._records = loaded
        except Exception as e:
            self._warn_io(
                f"trade ledger load from {self._path} failed ({e}); starting empty "
                "(same as the pre-ledger in-memory behavior)")
            self._records = {}
            self._quarantine_corrupt_file()

    @staticmethod
    def _normalize_loaded_record(record) -> Optional[Dict]:
        if not isinstance(record, dict):
            return None
        executor_id = record.get("executor_id")
        if not isinstance(executor_id, str) or not executor_id:
            return None
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not math.isfinite(timestamp):
            return None
        side = record.get("side")
        side = _normalize_side(side) if side is not None else None
        level_id = record.get("level_id")
        if not isinstance(level_id, str) or not level_id:
            level_id = None
        amount_quote = _as_float(record.get("amount_quote"))
        if amount_quote < 0.0:
            amount_quote = 0.0
        return {
            "executor_id": executor_id,
            "timestamp": float(timestamp),
            "side": side,
            "level_id": level_id,
            "amount_quote": amount_quote,
        }

    def _quarantine_corrupt_file(self):
        """Best-effort: move an unreadable ledger aside for post-mortems so the
        next persist does not silently overwrite the evidence."""
        try:
            if self._path.exists():
                os.replace(str(self._path), f"{self._path}.corrupt")
        except OSError:
            pass

    def _persist(self):
        """Atomic write (tmp file + flush + fsync + os.replace). Never raises."""
        try:
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "ledger_id": self._ledger_id,
                "records": sorted(self._records.values(), key=lambda r: (r["timestamp"], r["executor_id"])),
            }
            self._base_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self._base_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, sort_keys=True)
                    # Flush + fsync BEFORE the atomic replace (range_inventory_ladder
                    # discipline): without it a power loss can leave a torn file, and
                    # the load path would then quarantine it and reset the cap history.
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self._path))
                try:
                    dir_fd = os.open(str(self._base_dir), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    # Directory fsync is a POSIX nicety; unavailable on Windows.
                    pass
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            self._warn_io(
                f"trade ledger persist to {self._path} failed ({e}); records are kept "
                "in memory only until the next successful write")

    def _warn_io(self, message: str):
        self._io_failure_count += 1
        # Always surface the first few failures; then sample so a permanently
        # broken disk does not flood the log every tick.
        if self._io_failure_count <= 3 or self._io_failure_count % 100 == 0:
            self._logger.warning(f"{message} (failure #{self._io_failure_count})")
