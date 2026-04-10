#!/usr/bin/env python3
"""
Replay lifecycle JSONL files into PostgreSQL/SQLite OrderLifecycleEvent table.
Idempotent: uses event_id as dedup key, skips already-inserted events.
Handles both .jsonl and .jsonl.gz files.

Usage:
    python scripts/replay_lifecycle_jsonl_to_db.py \
        --file logs/lifecycle/2026-04-09/abc123.jsonl \
        --db-url "postgresql+psycopg2://hbot:password@127.0.0.1:5432/hummingbot_api"

    python scripts/replay_lifecycle_jsonl_to_db.py \
        --dir logs/lifecycle/ \
        --db-url "postgresql+psycopg2://hbot:password@127.0.0.1:5432/hummingbot_api"

    python scripts/replay_lifecycle_jsonl_to_db.py \
        --file logs/lifecycle/2026-04-09/abc123.jsonl \
        --validate-only
"""
import argparse
import gzip
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

# Add parent dir to path so we can import hummingbot modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hummingbot.model import HummingbotBase
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
from hummingbot.model.bot_run import BotRun

REQUIRED_FIELDS = {"event_id", "event_type"}
MAX_KNOWN_VERSION = 1


def parse_args():
    parser = argparse.ArgumentParser(description="Replay lifecycle JSONL into database")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Single JSONL or JSONL.gz file to replay")
    group.add_argument("--dir", help="Directory to recursively scan for .jsonl/.jsonl.gz files")
    parser.add_argument("--db-url", help="SQLAlchemy database URL (required unless --validate-only)")
    parser.add_argument("--validate-only", action="store_true", help="Validate JSONL without DB writes")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch insert size (default: 500)")
    parser.add_argument("--schema-version", type=int, default=MAX_KNOWN_VERSION,
                        help=f"Max accepted event_version (default: {MAX_KNOWN_VERSION})")
    return parser.parse_args()


def find_jsonl_files(directory: str):
    """Recursively find .jsonl and .jsonl.gz files."""
    files = []
    for dirpath, _, filenames in os.walk(directory):
        for fname in sorted(filenames):
            if fname.endswith(".jsonl") or fname.endswith(".jsonl.gz"):
                files.append(os.path.join(dirpath, fname))
    return files


def open_jsonl(filepath: str):
    """Open a .jsonl or .jsonl.gz file."""
    if filepath.endswith(".gz"):
        return gzip.open(filepath, "rt", encoding="utf-8")
    return open(filepath, "r", encoding="utf-8")


def replay_file(filepath: str, session_factory, batch_size: int, schema_version: int,
                validate_only: bool, stats: dict):
    """Replay a single JSONL file. Updates stats dict in place."""
    stats["files_processed"] += 1
    batch = []

    with open_jsonl(filepath) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            stats["lines_read"] += 1

            # Parse JSON
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                stats["invalid_skipped"] += 1
                print(f"  WARNING: {filepath}:{line_no}: malformed JSON: {e}", file=sys.stderr)
                continue

            # Validate required fields
            if not REQUIRED_FIELDS.issubset(event.keys()):
                stats["invalid_skipped"] += 1
                missing = REQUIRED_FIELDS - event.keys()
                print(f"  WARNING: {filepath}:{line_no}: missing fields: {missing}", file=sys.stderr)
                continue

            # Check schema version
            ev_version = event.get("event_version", 1)
            if ev_version > schema_version:
                stats["invalid_skipped"] += 1
                print(f"  WARNING: {filepath}:{line_no}: event_version={ev_version} > max={schema_version}, skipping",
                      file=sys.stderr)
                continue

            stats["valid_events"] += 1

            if validate_only:
                continue

            batch.append(event)
            if len(batch) >= batch_size:
                _flush_batch(batch, session_factory, stats)
                batch = []

    # Flush remaining
    if batch and not validate_only:
        _flush_batch(batch, session_factory, stats)


def _flush_batch(batch: list, session_factory, stats: dict):
    """Insert a batch of events, handling dedup via ON CONFLICT."""
    session = session_factory()
    try:
        for event in batch:
            event_type = event.get("event_type", "")

            # Handle bot_run events specially
            if event_type in ("bot_run_started", "bot_run_stopped"):
                _upsert_bot_run(session, event, stats)
                continue

            # Build OLE kwargs — only include fields that the model accepts
            ole_fields = {c.name for c in OrderLifecycleEvent.__table__.columns} - {"id"}
            kwargs = {k: v for k, v in event.items() if k in ole_fields}
            # Remove schema_name since it's not a DB column
            kwargs.pop("schema_name", None)

            try:
                # Check for existing event_id to avoid unique constraint violation
                existing = session.query(OrderLifecycleEvent).filter(
                    OrderLifecycleEvent.event_id == kwargs.get("event_id")
                ).first()
                if existing:
                    stats["duplicates_skipped"] += 1
                    continue
                ole = OrderLifecycleEvent(**kwargs)
                session.add(ole)
                session.flush()
                stats["inserted"] += 1
            except Exception as e:
                session.rollback()
                stats["errors"] += 1
                if stats["errors"] <= 5:
                    print(f"  ERROR inserting event: {e}", file=sys.stderr)

        session.commit()
    except Exception as e:
        session.rollback()
        stats["errors"] += 1
        print(f"  ERROR in batch commit: {e}", file=sys.stderr)
    finally:
        session.close()


def _upsert_bot_run(session, event: dict, stats: dict):
    """Upsert a BotRun row from bot_run_started/stopped event."""
    try:
        bot_run_id = event.get("bot_run_id")
        if not bot_run_id:
            return
        existing = session.query(BotRun).filter(BotRun.id == bot_run_id).first()
        if event["event_type"] == "bot_run_started":
            if not existing:
                br = BotRun(
                    id=bot_run_id,
                    started_ts_ms=event.get("emitted_ts_ms") or event.get("timestamp_ms"),
                    strategy_name=event.get("strategy_name"),
                    connectors=event.get("connectors"),
                    trading_pairs=event.get("trading_pairs"),
                )
                session.add(br)
                stats["inserted"] += 1
            else:
                stats["duplicates_skipped"] += 1
        elif event["event_type"] == "bot_run_stopped":
            if existing:
                existing.ended_ts_ms = event.get("emitted_ts_ms") or event.get("timestamp_ms")
                existing.stop_reason = event.get("stop_reason")
                stats["inserted"] += 1
            else:
                stats["duplicates_skipped"] += 1
        session.flush()
    except Exception as e:
        session.rollback()
        stats["errors"] += 1


def main():
    args = parse_args()

    if not args.validate_only and not args.db_url:
        print("ERROR: --db-url is required unless --validate-only is set", file=sys.stderr)
        sys.exit(1)

    # Collect files
    if args.file:
        files = [args.file]
    else:
        files = find_jsonl_files(args.dir)

    if not files:
        print("No JSONL files found.")
        sys.exit(0)

    print(f"Found {len(files)} file(s) to process")

    # Set up DB
    session_factory = None
    if not args.validate_only:
        engine = create_engine(args.db_url)
        # Ensure tables exist
        HummingbotBase.metadata.create_all(engine, tables=[
            OrderLifecycleEvent.__table__,
            BotRun.__table__,
        ])
        session_factory = sessionmaker(bind=engine)

    stats = {
        "files_processed": 0,
        "lines_read": 0,
        "valid_events": 0,
        "invalid_skipped": 0,
        "inserted": 0,
        "duplicates_skipped": 0,
        "errors": 0,
    }

    for filepath in files:
        print(f"  Processing: {filepath}")
        replay_file(filepath, session_factory, args.batch_size, args.schema_version,
                     args.validate_only, stats)

    # Summary
    print(f"\nReplay complete:")
    print(f"  Files processed: {stats['files_processed']}")
    print(f"  Lines read: {stats['lines_read']:,}")
    print(f"  Valid events: {stats['valid_events']:,}")
    print(f"  Invalid/skipped: {stats['invalid_skipped']:,}")
    if not args.validate_only:
        print(f"  Inserted: {stats['inserted']:,}")
        print(f"  Duplicates (skipped): {stats['duplicates_skipped']:,}")
        print(f"  Errors: {stats['errors']:,}")

    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
