# filename: controllers/_shared/candle_freshness.py
#
# CDX-001 / CLA-405 / CLA-406 -- interval-relative candle freshness gate.
#
# `CandlesBase.ready` is length-only: a deque that filled once and then froze
# (dead websocket, silently-stopped poller) stays "ready" forever, and every
# controller that reads `df[...].iloc[-1]` keeps replaying the last bar as a
# live signal -- re-entering on a dead market view every cooldown. This module
# provides the shared fail-closed gate: if the newest bar is older than
# `max_age_intervals * interval_seconds`, the data is STALE and the caller must
# zero its signal / mark data unavailable (with a WARNING) instead of trading.
#
# REGRESSION CAVEAT (both review engines): sparse pairs legitimately have old
# closed bars -- the newest available bar on a low-volume market can be many
# intervals old while the feed itself is healthy (the `_drop_incomplete_last_bar`
# case). The bound is therefore interval-relative, generous by default, and a
# per-controller (hence per-market) config field; `<= 0` disables the gate
# entirely, restoring the legacy behavior byte-for-byte.

import math
import re
from typing import NamedTuple, Optional

import pandas as pd

# Generous default: 48 intervals = 4h on a 5m feed, 2 days on 1h, 8 days on 4h.
# Catches a genuinely dead feed while tolerating sparse markets; operators on
# liquid pairs should tighten it per deployment, sparse pairs may widen it.
DEFAULT_STALE_CANDLE_MAX_AGE_INTERVALS = 48.0

# Rate limit for the stale-data WARNING (per interval key) so a persistent
# outage does not flood the log every tick.
_WARN_INTERVAL_SECONDS = 60.0

_INTERVAL_RE = re.compile(r"^(\d+)(s|m|h|d|w|M)$")
_UNIT_SECONDS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
    "M": 2592000.0,
}


def interval_to_seconds(interval) -> Optional[float]:
    """Parse a hummingbot candle interval ('1s'...'1M') to seconds.

    Covers every interval in CandlesBase.interval_to_seconds without importing
    the framework. Returns None for an unparseable interval string.
    """
    if not isinstance(interval, str):
        return None
    match = _INTERVAL_RE.match(interval.strip())
    if match is None:
        return None
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class FreshnessResult(NamedTuple):
    fresh: bool
    reason: str
    age_seconds: Optional[float]
    max_age_seconds: Optional[float]


class CandleFreshnessGate:
    """Stateless-per-check freshness gate with rate-limited stale warnings.

    One instance per controller (see get_freshness_gate). All parameters are
    passed per check() call so hot-updated config values apply immediately.
    check() never raises: an internal error is reported as STALE (fail-closed
    -- invariant #1: a broken gate must not let a stale feed keep trading).
    """

    def __init__(self, logger=None, context: str = ""):
        self._logger = logger
        self._context = context
        self._last_warn_ts = {}
        self._unknown_interval_warned = set()

    def check(self, df, interval, now, max_age_intervals) -> FreshnessResult:
        try:
            return self._check(df, interval, now, max_age_intervals)
        except Exception as e:  # pragma: no cover - defensive backstop
            result = FreshnessResult(False, f"internal_error:{e}", None, None)
            self._warn_stale(interval, 0.0, result)
            return result

    def _check(self, df, interval, now, max_age_intervals) -> FreshnessResult:
        try:
            bound_intervals = float(max_age_intervals)
        except (TypeError, ValueError):
            bound_intervals = 0.0
        if bound_intervals <= 0.0 or not math.isfinite(bound_intervals):
            # Explicitly disabled: legacy (length-only) behavior, no gating.
            return FreshnessResult(True, "disabled", None, None)

        try:
            now_f = float(now)
        except (TypeError, ValueError):
            now_f = float("nan")
        if not math.isfinite(now_f):
            result = FreshnessResult(False, "clock_unavailable", None, None)
            self._warn_stale(interval, 0.0, result)
            return result

        if df is None or len(df) == 0:
            result = FreshnessResult(False, "no_candles", None, None)
            self._warn_stale(interval, now_f, result)
            return result
        if "timestamp" not in getattr(df, "columns", []):
            result = FreshnessResult(False, "missing_timestamp_column", None, None)
            self._warn_stale(interval, now_f, result)
            return result
        last_ts = float(pd.to_numeric(df["timestamp"], errors="coerce").max())
        if not math.isfinite(last_ts):
            result = FreshnessResult(False, "invalid_timestamps", None, None)
            self._warn_stale(interval, now_f, result)
            return result

        interval_seconds = interval_to_seconds(interval)
        if interval_seconds is None or interval_seconds <= 0:
            # Cannot compute an interval-relative age. All hummingbot-supported
            # intervals parse; warn once per unknown value and do not gate
            # (gating forever on an operator typo would permanently zero the
            # strategy with no stale data actually present).
            self._warn_unknown_interval(interval)
            return FreshnessResult(True, "unknown_interval", None, None)

        max_age_seconds = bound_intervals * interval_seconds
        age_seconds = now_f - last_ts
        if age_seconds > max_age_seconds:
            result = FreshnessResult(False, "stale", age_seconds, max_age_seconds)
            self._warn_stale(interval, now_f, result)
            return result
        return FreshnessResult(True, "fresh", age_seconds, max_age_seconds)

    def _warn_stale(self, interval, now_f: float, result: FreshnessResult):
        if self._logger is None:
            return
        try:
            key = str(interval)
            last = self._last_warn_ts.get(key)
            if last is not None and now_f and (now_f - last) < _WARN_INTERVAL_SECONDS:
                return
            self._last_warn_ts[key] = now_f
            age = f"{result.age_seconds:.0f}s" if result.age_seconds is not None else "n/a"
            bound = f"{result.max_age_seconds:.0f}s" if result.max_age_seconds is not None else "n/a"
            self._logger.warning(
                f"Stale/unavailable candles{f' [{self._context}]' if self._context else ''} "
                f"(interval={interval}, reason={result.reason}, last_bar_age={age}, "
                f"max_age={bound}) -- zeroing the signal / pausing until fresh data arrives. "
                f"Rate-limited to one warning per {_WARN_INTERVAL_SECONDS:.0f}s.")
        except Exception:  # pragma: no cover - logging must never break the gate
            pass

    def _warn_unknown_interval(self, interval):
        if self._logger is None:
            return
        try:
            key = str(interval)
            if key in self._unknown_interval_warned:
                return
            self._unknown_interval_warned.add(key)
            self._logger.warning(
                f"Candle freshness gate{f' [{self._context}]' if self._context else ''}: "
                f"cannot parse interval {interval!r}; freshness gating is DISABLED for this feed.")
        except Exception:  # pragma: no cover
            pass


def get_freshness_gate(controller) -> CandleFreshnessGate:
    """Lazily attach one CandleFreshnessGate to a controller instance.

    Works for controllers constructed via __new__ in tests (no __init__ hook
    needed) and keeps the warn rate-limit state per controller.
    """
    gate = getattr(controller, "_candle_freshness_gate", None)
    if gate is None:
        config = getattr(controller, "config", None)
        context = str(getattr(config, "id", "") or "")
        try:
            logger = controller.logger()
        except Exception:
            logger = None
        gate = CandleFreshnessGate(logger=logger, context=context)
        controller._candle_freshness_gate = gate
    return gate
