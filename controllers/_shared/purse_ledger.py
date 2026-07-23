# filename: controllers/_shared/purse_ledger.py
#
# hbpurse P4 (F1/F2/F13/F9/F16) -- the append-only PURSE JOURNAL, "Purse journal contract v1".
#
# One journal file per controller (`<state_stem>.purse.json`, next to the ladder state file)
# holding the permanent inception-to-date accounting record: opening epochs, declared flows,
# per-epoch fill rollups, reseed epochs, re-anchor cuts and wallet checkpoints. The journal
# OBSERVES the fund -- it never resizes it (the trading ledger stays deposit-excluded).
#
# IO discipline (contract): single writer (the controller loop). Atomic write = temp file +
# flush + fsync + os.replace (the range_inventory_ladder `_save_state` pattern). FAIL CLOSED:
# unlike the fail-safe TradeLedger next door, every load/save failure RAISES to the caller --
# a start-empty / in-memory fallback is FORBIDDEN for money. The controller reacts by setting
# `accounting_degraded` (halting NEW order proposals; existing orders untouched) and retrying
# the write every cycle: the in-memory document IS the pending journal delta, so a retried
# save carries everything that could not land. No prune, no cap, no retention limit -- the
# journal is the permanent record. The ONE documented exception to append-only is the
# per-epoch `fills_rollup` record, which is updated in place to bound file growth;
# flow/epoch/reanchor/checkpoint records are never compacted, edited or removed.

import json
import logging
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

PURSE_SCHEMA_VERSION = 1

OPENING_BASIS_QUALITIES = ("reconstructed", "current_equity_only")
FLOW_KINDS = ("deposit", "withdrawal")
FLOW_CONFIRMATIONS = ("wallet_delta_matched", "drift")
REANCHOR_CLASSIFICATIONS = ("undeclared_outflow", "drift")

# Record kinds that OPEN an accounting epoch (later records' epoch_id must reference one).
EPOCH_OPENING_KINDS = ("opening_epoch", "reseed_epoch")

KNOWN_KINDS = frozenset({
    "opening_epoch", "flow", "fills_rollup", "reseed_epoch", "reanchor", "checkpoint",
})


class PurseError(Exception):
    """Base class for purse journal failures. The controller fails CLOSED on any of these."""


class PurseIOError(PurseError):
    """A read/write of the journal file failed (disk, permissions, torn write)."""


class PurseIntegrityError(PurseError):
    """The journal content violates the contract (schema, identity, sequence, fields)."""


def _parse_decimal(value, field_name: str) -> Decimal:
    """Finite Decimal from a journal field value; PurseIntegrityError on anything else."""
    if value is None or value == "" or isinstance(value, bool):
        raise PurseIntegrityError(f"purse field '{field_name}' must be a finite decimal, got {value!r}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PurseIntegrityError(f"purse field '{field_name}' is not a decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise PurseIntegrityError(f"purse field '{field_name}' must be finite, got {value!r}")
    return parsed


def _require_nonneg(value: Decimal, field_name: str) -> Decimal:
    if value < Decimal("0"):
        raise PurseIntegrityError(f"purse field '{field_name}' must be non-negative, got {value}")
    return value


def _require_ts(value, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PurseIntegrityError(f"purse field '{field_name}' must be a number, got {value!r}")
    ts = float(value)
    if ts != ts or ts in (float("inf"), float("-inf")) or ts < 0.0:
        raise PurseIntegrityError(f"purse field '{field_name}' must be a finite non-negative number")
    return ts


def _require_str(value, field_name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise PurseIntegrityError(f"purse field '{field_name}' must be a non-empty string, got {value!r}")
    return value


def _require_enum(value, field_name: str, allowed) -> str:
    if value not in allowed:
        raise PurseIntegrityError(
            f"purse field '{field_name}' must be one of {sorted(allowed)}, got {value!r}"
        )
    return value


# Per-kind money-field specs: field -> True when the contract pins the value non-negative
# (signed fields carry False). Every money value is stored as a decimal STRING.
_MONEY_FIELDS: Dict[str, Dict[str, bool]] = {
    "opening_epoch": {
        "owned_quote": True, "owned_base": True, "seed_value_quote": True,
        "reference_price": True, "wallet_quote_total": True, "wallet_base_total": True,
        "unavailable_quote": True, "unavailable_base": True,
        "contributed_opening_quote": True,
        # A reconstructed opening basis may legitimately carry a negative earned-to-date.
        "earned_opening_quote": False,
    },
    "flow": {
        "native_amount": True, "quote_valuation": True, "valuation_price": True,
    },
    "fills_rollup": {
        # Signed cumulative deltas: base bought-sold, quote received-spent (fees netted).
        "base_delta_cum": False, "quote_delta_cum": False, "fees_quote_cum": True,
    },
    "reseed_epoch": {
        "old_owned_quote": True, "old_owned_base": True, "old_seed_value_quote": True,
        "new_owned_quote": True, "new_owned_base": True, "new_seed_value_quote": True,
        "reference_price": True,
    },
    "reanchor": {
        "old_owned_quote": True, "old_owned_base": True,
        "new_owned_quote": True, "new_owned_base": True,
        "overclaim_quote": True, "wallet_quote_total": True, "wallet_base_total": True,
    },
    "checkpoint": {
        "owned_quote": True, "owned_base": True, "reference_price": True,
        "equity_quote": True, "wallet_quote_total": True, "wallet_base_total": True,
        "external_holds_quote": True, "external_holds_base": True,
    },
}


def purse_path_for_state(state_path: Path) -> Path:
    """Contract naming: `range_inventory_ladder_xmr_usdt.json` -> `..._xmr_usdt.purse.json`."""
    state_path = Path(state_path)
    return state_path.with_name(f"{state_path.stem}.purse.json")


class PurseLedger:
    """Controller-owned append-only purse journal (contract v1). One instance per controller.

    The full document lives in memory once loaded/created; appends and rollup updates mutate
    the in-memory document first and `save()` persists it atomically. A failed save leaves the
    document intact (the pending delta), so the caller's every-cycle retry naturally re-carries
    everything unpersisted. All mutating/inspecting methods raise PurseError subclasses --
    nothing here swallows a failure, because the caller must fail closed on money."""

    def __init__(self, path, *, controller_id: str, controller_name: str,
                 trading_pair: str, logger: Optional[logging.Logger] = None):
        self._path = Path(path)
        self._controller_id = str(controller_id)
        self._controller_name = str(controller_name)
        self._trading_pair = str(trading_pair)
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._doc: Optional[Dict[str, Any]] = None
        self._dirty = False
        self._io_failures = 0

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def loaded(self) -> bool:
        """True once a document is adopted (loaded from disk or created for bootstrap)."""
        return self._doc is not None

    @property
    def dirty(self) -> bool:
        """True while the in-memory document holds a delta not yet durably saved."""
        return self._dirty

    @property
    def io_failures(self) -> int:
        return self._io_failures

    def file_exists(self) -> bool:
        return self._path.exists()

    # ------------------------------------------------------------------ #
    # Load / create
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Adopt the on-disk journal. Raises PurseIOError / PurseIntegrityError; on failure
        the ledger stays un-loaded (the caller degrades and retries -- the corrupt file is
        NEVER moved aside or overwritten; it is the permanent record and the operator decides)."""
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._io_failures += 1
            raise PurseIOError(f"purse journal read failed for {self._path}: {exc}") from exc
        try:
            raw = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise PurseIntegrityError(f"purse journal {self._path} is not valid JSON: {exc}") from exc
        self._doc = self._validate_document(raw)
        self._dirty = False

    def create(self) -> None:
        """Start a NEW in-memory journal for the one-shot bootstrap. Refuses when a document
        is already adopted or a file already exists on disk (never silently re-bootstrap)."""
        if self._doc is not None:
            raise PurseError("purse journal already adopted; refusing to re-create")
        if self.file_exists():
            raise PurseError(
                f"purse journal file {self._path} already exists; load it instead of re-creating"
            )
        self._doc = {
            "purse_schema_version": PURSE_SCHEMA_VERSION,
            "controller_id": self._controller_id,
            "controller_name": self._controller_name,
            "trading_pair": self._trading_pair,
            "sequence": 0,
            "records": [],
        }
        self._dirty = True

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #

    def records(self) -> List[Dict[str, Any]]:
        """Shallow copies of every record, in seq order (reporting/tests; not the live list)."""
        if self._doc is None:
            return []
        return [dict(record) for record in self._doc["records"]]

    def next_epoch_id(self) -> str:
        """The epoch id an epoch-opening record appended NOW would carry (epoch-<its seq>)."""
        self._require_loaded()
        return f"epoch-{int(self._doc['sequence']) + 1}"

    def current_epoch_id(self) -> Optional[str]:
        if self._doc is None:
            return None
        for record in reversed(self._doc["records"]):
            if record.get("kind") in EPOCH_OPENING_KINDS:
                return record.get("epoch_id")
        return None

    def latest_opening_epoch(self) -> Optional[Dict[str, Any]]:
        if self._doc is None:
            return None
        for record in reversed(self._doc["records"]):
            if record.get("kind") == "opening_epoch":
                return dict(record)
        return None

    def first_opening_epoch(self) -> Optional[Dict[str, Any]]:
        if self._doc is None:
            return None
        for record in self._doc["records"]:
            if record.get("kind") == "opening_epoch":
                return dict(record)
        return None

    def opening_basis_quality(self) -> Optional[str]:
        """Quality of the INCEPTION basis (the first opening epoch)."""
        first = self.first_opening_epoch()
        return first.get("opening_basis_quality") if first else None

    def reanchor_count(self) -> int:
        if self._doc is None:
            return 0
        return sum(1 for record in self._doc["records"] if record.get("kind") == "reanchor")

    # ------------------------------------------------------------------ #
    # Mutation (in-memory; `save()` persists)
    # ------------------------------------------------------------------ #

    def append(self, kind: str, fields: Dict[str, Any], *, ts: float) -> Dict[str, Any]:
        """Append one record (stamps seq/ts/kind, validates against the contract). The record
        joins the in-memory document and marks it dirty; call save() to persist."""
        self._require_loaded()
        if kind not in KNOWN_KINDS:
            raise PurseIntegrityError(f"unknown purse record kind {kind!r}")
        seq = int(self._doc["sequence"]) + 1
        record: Dict[str, Any] = {"seq": seq, "ts": float(ts), "kind": kind}
        record.update(fields)
        self._validate_record(record, opened_epochs=self._opened_epoch_ids())
        if kind == "fills_rollup":
            epoch_id = record.get("epoch_id")
            for existing in self._doc["records"]:
                if existing.get("kind") == "fills_rollup" and existing.get("epoch_id") == epoch_id:
                    raise PurseIntegrityError(
                        f"epoch {epoch_id!r} already has a fills_rollup record; update it in "
                        "place via update_fills_rollup"
                    )
        self._doc["records"].append(record)
        self._doc["sequence"] = seq
        self._dirty = True
        return dict(record)

    def update_fills_rollup(self, *, epoch_id: str, d_base: Decimal, d_quote: Decimal,
                            d_fees: Decimal, fills: int, ts: float) -> Dict[str, Any]:
        """Accumulate booked-fill deltas into the epoch's ONE rollup record, in place (the
        single documented append-only exception). Creates the rollup on first use."""
        self._require_loaded()
        if not isinstance(epoch_id, str) or not epoch_id:
            raise PurseIntegrityError("update_fills_rollup requires a non-empty epoch_id")
        # CDX-R05: the in-place path bypasses append()'s record validation, so every incoming
        # value is strict-parsed BEFORE any mutation -- finite-before-persistence is absolute,
        # and a NaN/Infinity delta must raise here, not poison the journal for the next load.
        d_base = _parse_decimal(d_base, "fills_rollup d_base")
        d_quote = _parse_decimal(d_quote, "fills_rollup d_quote")
        d_fees = _require_nonneg(_parse_decimal(d_fees, "fills_rollup d_fees"),
                                 "fills_rollup d_fees")
        if isinstance(fills, bool) or not isinstance(fills, int) or fills < 0:
            raise PurseIntegrityError("fills_rollup fills must be a non-negative integer")
        ts = _require_ts(ts, "fills_rollup ts")
        target = None
        for record in self._doc["records"]:
            if record.get("kind") == "fills_rollup" and record.get("epoch_id") == epoch_id:
                target = record
                break
        if target is None:
            return self.append(
                "fills_rollup",
                {
                    "epoch_id": epoch_id,
                    "base_delta_cum": str(d_base),
                    "quote_delta_cum": str(d_quote),
                    "fees_quote_cum": str(d_fees),
                    "fills_seen": fills,
                    "last_update_ts": ts,
                },
                ts=ts,
            )
        # Compute every cumulative in a local first: the record mutates all-at-once only after
        # the complete candidate validated, so a bad stored value can never leave it half-updated.
        new_base = _parse_decimal(target["base_delta_cum"], "base_delta_cum") + d_base
        new_quote = _parse_decimal(target["quote_delta_cum"], "quote_delta_cum") + d_quote
        new_fees = _parse_decimal(target["fees_quote_cum"], "fees_quote_cum") + d_fees
        for name, value in (("base_delta_cum", new_base), ("quote_delta_cum", new_quote),
                            ("fees_quote_cum", new_fees)):
            if not value.is_finite():
                raise PurseIntegrityError(f"fills_rollup {name} update is non-finite")
        target["base_delta_cum"] = str(new_base)
        target["quote_delta_cum"] = str(new_quote)
        target["fees_quote_cum"] = str(new_fees)
        target["fills_seen"] = int(target["fills_seen"]) + fills
        target["last_update_ts"] = ts
        self._dirty = True
        return dict(target)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def save(self) -> None:
        """Atomic write (tmp + flush + fsync + os.replace + best-effort dir fsync). Raises
        PurseIOError on failure -- the in-memory document (the pending delta) is untouched, so
        the caller's next-cycle retry re-carries it. Clears `dirty` only on success."""
        self._require_loaded()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._doc, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self._path))
                try:
                    dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
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
        except PurseError:
            raise
        except Exception as exc:
            self._io_failures += 1
            raise PurseIOError(f"purse journal save failed for {self._path}: {exc}") from exc
        self._dirty = False

    # ------------------------------------------------------------------ #
    # Derived metrics (contract formulas; reporting only, never stored as authority)
    # ------------------------------------------------------------------ #

    def derived_metrics(self, *, reference_price: Decimal, owned_quote: Decimal,
                        owned_base: Decimal) -> Dict[str, Decimal]:
        """Contract v1 derived metrics at the CURRENT reference price:

            contributed     = sum(opening contributed_opening_quote) + sum(deposit quote_valuation)
            withdrawn       = sum(withdrawal quote_valuation)
            earned_realized = sum(opening earned_opening_quote)
                              + sum over epochs of (quote_delta_cum + base_delta_cum * ref)
            equity          = owned_quote + owned_base * ref
            earned_total    = equity - contributed + withdrawn
            unrealized      = earned_total - earned_realized
            drift           = sum over reanchor records of the per-asset cuts valued at ref
                              (undeclared cuts the records cannot otherwise explain -- ALWAYS
                              surfaced, never silently absorbed)

        A post-quarantine re-init opening epoch declares NO new basis (contributed/earned 0),
        so summing across opening epochs keeps inception continuity while every epoch stays
        an explicit record."""
        self._require_loaded()
        ref = reference_price if reference_price is not None else Decimal("0")
        zero = Decimal("0")
        contributed = zero
        withdrawn = zero
        earned_opening = zero
        realized_flows = zero
        drift = zero
        for record in self._doc["records"]:
            kind = record.get("kind")
            if kind == "opening_epoch":
                contributed += _parse_decimal(record["contributed_opening_quote"], "contributed_opening_quote")
                earned_opening += _parse_decimal(record["earned_opening_quote"], "earned_opening_quote")
            elif kind == "flow":
                valuation = _parse_decimal(record["quote_valuation"], "quote_valuation")
                if record.get("flow_kind") == "deposit":
                    contributed += valuation
                else:
                    withdrawn += valuation
            elif kind == "fills_rollup":
                realized_flows += (
                    _parse_decimal(record["quote_delta_cum"], "quote_delta_cum")
                    + _parse_decimal(record["base_delta_cum"], "base_delta_cum") * ref
                )
            elif kind == "reanchor":
                cut_quote = max(zero, _parse_decimal(record["old_owned_quote"], "old_owned_quote")
                                - _parse_decimal(record["new_owned_quote"], "new_owned_quote"))
                cut_base = max(zero, _parse_decimal(record["old_owned_base"], "old_owned_base")
                               - _parse_decimal(record["new_owned_base"], "new_owned_base"))
                drift += cut_quote + cut_base * ref
        equity = owned_quote + owned_base * ref
        earned_realized = earned_opening + realized_flows
        earned_total = equity - contributed + withdrawn
        return {
            "contributed": contributed,
            "withdrawn": withdrawn,
            "earned_realized": earned_realized,
            "earned_total": earned_total,
            "unrealized": earned_total - earned_realized,
            "drift": drift,
            "equity_quote": equity,
        }

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def _require_loaded(self):
        if self._doc is None:
            raise PurseError("purse journal is not loaded")

    def _opened_epoch_ids(self) -> set:
        return {
            record.get("epoch_id")
            for record in self._doc["records"]
            if record.get("kind") in EPOCH_OPENING_KINDS
        }

    def _validate_document(self, raw) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise PurseIntegrityError("purse journal payload must be a JSON object")
        if raw.get("purse_schema_version") != PURSE_SCHEMA_VERSION:
            raise PurseIntegrityError(
                f"unsupported purse_schema_version {raw.get('purse_schema_version')!r}; "
                f"supported: {PURSE_SCHEMA_VERSION}"
            )
        if raw.get("controller_id") != self._controller_id:
            raise PurseIntegrityError(
                f"purse journal controller_id {raw.get('controller_id')!r} does not match "
                f"{self._controller_id!r}; refusing to adopt a foreign journal"
            )
        if raw.get("controller_name") != self._controller_name:
            raise PurseIntegrityError(
                f"purse journal controller_name {raw.get('controller_name')!r} does not match "
                f"{self._controller_name!r}"
            )
        if raw.get("trading_pair") != self._trading_pair:
            raise PurseIntegrityError(
                f"purse journal trading_pair {raw.get('trading_pair')!r} does not match "
                f"{self._trading_pair!r}"
            )
        records = raw.get("records")
        if not isinstance(records, list):
            raise PurseIntegrityError("purse journal 'records' must be a list")
        opened_epochs: set = set()
        rollup_epochs: set = set()
        # Contract (CDX-R06): seq is 1-based and STRICTLY INCREASING -- no contiguity demand
        # (the sibling API validates against the same pinned text; the engine must not accept
        # a narrower format than the contract defines). The engine's own appends stay
        # contiguous, but a gap in a loaded journal is contract-valid and must load.
        prev_seq = 0
        index = 0
        for record in records:
            index += 1
            if not isinstance(record, dict):
                raise PurseIntegrityError(f"purse record #{index} must be a JSON object")
            seq = record.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) \
                    or (index == 1 and seq != 1) or seq <= prev_seq:
                raise PurseIntegrityError(
                    f"purse record seq {seq!r} violates the 1-based strictly-increasing order "
                    f"(record #{index}, previous seq {prev_seq})"
                )
            prev_seq = seq
            _require_ts(record.get("ts"), f"records[{seq}].ts")
            kind = record.get("kind")
            if kind not in KNOWN_KINDS:
                raise PurseIntegrityError(f"purse record #{seq} has unknown kind {kind!r}")
            self._validate_record(record, opened_epochs=opened_epochs)
            if kind in EPOCH_OPENING_KINDS:
                epoch_id = record.get("epoch_id")
                if epoch_id in opened_epochs:
                    raise PurseIntegrityError(f"epoch id {epoch_id!r} opened twice")
                opened_epochs.add(epoch_id)
            if kind == "fills_rollup":
                epoch_id = record.get("epoch_id")
                if epoch_id in rollup_epochs:
                    raise PurseIntegrityError(
                        f"epoch {epoch_id!r} has more than one fills_rollup record"
                    )
                rollup_epochs.add(epoch_id)
        sequence = raw.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != prev_seq:
            raise PurseIntegrityError(
                f"purse journal 'sequence' {sequence!r} does not match the highest record seq "
                f"({prev_seq})"
            )
        if not records or records[0].get("kind") != "opening_epoch":
            # CDX-R03: an on-disk journal with no opening_epoch (including records: []) IS the
            # forbidden start-empty fallback -- only create() may hold a transiently empty
            # document, and only in memory during the one-shot bootstrap.
            raise PurseIntegrityError("purse journal must begin with an opening_epoch record")
        return raw

    def _validate_record(self, record: Dict[str, Any], *, opened_epochs: set) -> None:
        kind = record["kind"]
        seq = record.get("seq")
        for field, non_negative in _MONEY_FIELDS[kind].items():
            value = _parse_decimal(record.get(field), f"records[{seq}].{field}")
            if non_negative:
                _require_nonneg(value, f"records[{seq}].{field}")
        if kind == "opening_epoch":
            _require_str(record.get("epoch_id"), f"records[{seq}].epoch_id")
            _require_enum(record.get("opening_basis_quality"),
                          f"records[{seq}].opening_basis_quality", OPENING_BASIS_QUALITIES)
            # CDX-R03: `predecessor` and `note` are contract-REQUIRED fields whose VALUES may
            # be null -- the key must exist, so presence is checked before nullability.
            for nullable_field in ("predecessor", "note"):
                if nullable_field not in record:
                    raise PurseIntegrityError(
                        f"records[{seq}].{nullable_field} is required (null is allowed, "
                        "absence is not)"
                    )
            predecessor = record.get("predecessor")
            if predecessor is not None and not isinstance(predecessor, str):
                raise PurseIntegrityError(
                    f"records[{seq}].predecessor must be a string or null, got {predecessor!r}"
                )
            note = record.get("note")
            if note is not None and not isinstance(note, str):
                raise PurseIntegrityError(f"records[{seq}].note must be a string or null")
        elif kind == "flow":
            _require_str(record.get("token"), f"records[{seq}].token")
            _require_enum(record.get("flow_kind"), f"records[{seq}].flow_kind", FLOW_KINDS)
            _require_str(record.get("asset"), f"records[{seq}].asset")
            _require_ts(record.get("valuation_ts"), f"records[{seq}].valuation_ts")
            _require_enum(record.get("confirmation"), f"records[{seq}].confirmation",
                          FLOW_CONFIRMATIONS)
        elif kind == "fills_rollup":
            self._require_known_epoch(record, seq, opened_epochs)
            fills_seen = record.get("fills_seen")
            if isinstance(fills_seen, bool) or not isinstance(fills_seen, int) or fills_seen < 0:
                raise PurseIntegrityError(
                    f"records[{seq}].fills_seen must be a non-negative integer"
                )
            _require_ts(record.get("last_update_ts"), f"records[{seq}].last_update_ts")
        elif kind == "reseed_epoch":
            _require_str(record.get("epoch_id"), f"records[{seq}].epoch_id")
            if "prev_epoch_id" not in record:
                raise PurseIntegrityError(
                    f"records[{seq}].prev_epoch_id is required (null is allowed, absence is not)"
                )
            prev_epoch = record.get("prev_epoch_id")
            if prev_epoch is not None and not isinstance(prev_epoch, str):
                raise PurseIntegrityError(
                    f"records[{seq}].prev_epoch_id must be a string or null"
                )
            _require_str(record.get("token"), f"records[{seq}].token")
        elif kind == "reanchor":
            self._require_known_epoch(record, seq, opened_epochs)
            _require_enum(record.get("classification"), f"records[{seq}].classification",
                          REANCHOR_CLASSIFICATIONS)
        elif kind == "checkpoint":
            self._require_known_epoch(record, seq, opened_epochs)

    @staticmethod
    def _require_known_epoch(record: Dict[str, Any], seq, opened_epochs: set) -> None:
        epoch_id = record.get("epoch_id")
        _require_str(epoch_id, f"records[{seq}].epoch_id")
        if epoch_id not in opened_epochs:
            raise PurseIntegrityError(
                f"records[{seq}] references epoch {epoch_id!r} that no opening_epoch/"
                f"reseed_epoch record opened"
            )
