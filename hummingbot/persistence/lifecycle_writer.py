"""
Dual-write lifecycle writer.
Accepts LifecycleEvent objects, writes to:
  1. JSONL file (strict, schema-versioned, per-run)
  2. PostgreSQL/SQLite OrderLifecycleEvent table
  3. StructuredEventLogger (for backward compat)

Design:
  - JSONL write happens first (fast, local, reliable)
  - DB write happens second (may fail for remote PG)
  - Failures increment counters, never crash
  - Periodic health summary emitted to regular logs
"""
import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
from hummingbot.persistence.lifecycle_event import LifecycleEvent

logger = logging.getLogger(__name__)


class LifecycleWriter:
    """Writes LifecycleEvent objects to JSONL + DB. Never crashes the trading loop."""

    def __init__(
        self,
        sql_manager,
        bot_run_id: str,
        log_dir: str = "logs",
        structured_logger=None,
    ):
        self._sql_manager = sql_manager
        self._bot_run_id = bot_run_id
        self._structured_logger = structured_logger

        # Counters
        self.events_total: int = 0
        self.jsonl_writes_ok: int = 0
        self.jsonl_writes_failed: int = 0
        self.db_writes_ok: int = 0
        self.db_writes_failed: int = 0
        self.last_success_ts_ms: int = 0

        # Set up per-run JSONL file
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        run_dir = os.path.join(log_dir, "lifecycle", run_date)
        os.makedirs(run_dir, exist_ok=True)
        self._jsonl_path = os.path.join(run_dir, f"{bot_run_id}.jsonl")
        self._jsonl_file = open(self._jsonl_path, "a", encoding="utf-8")
        self._log_dir = log_dir

    @property
    def jsonl_path(self) -> str:
        return self._jsonl_path

    def write(self, event: LifecycleEvent):
        """Write a lifecycle event to JSONL + DB + structured logger. Never raises."""
        try:
            self.events_total += 1

            # Ensure bot_run_id and event_id are set
            if not event.bot_run_id:
                event.bot_run_id = self._bot_run_id
            if not event.event_id:
                import uuid
                event.event_id = str(uuid.uuid4())

            # 1. JSONL write (fast, local, reliable — always first)
            try:
                d = event.to_dict()
                d["schema_name"] = "lifecycle_v1"
                line = json.dumps(d, default=str, separators=(",", ":"))
                self._jsonl_file.write(line + "\n")
                self._jsonl_file.flush()
                self.jsonl_writes_ok += 1
            except Exception as e:
                self.jsonl_writes_failed += 1
                logger.warning(f"LifecycleWriter JSONL write failed: {e}")

            # 2. DB write to OrderLifecycleEvent table
            try:
                kwargs = event.to_db_kwargs()
                ole = OrderLifecycleEvent(**kwargs)
                with self._sql_manager.get_new_session() as session:
                    with session.begin():
                        session.add(ole)
                self.db_writes_ok += 1
            except Exception as e:
                self.db_writes_failed += 1
                logger.debug(f"LifecycleWriter DB write failed: {e}")

            # 3. Backward-compat structured logger emission
            try:
                if self._structured_logger:
                    self._structured_logger.emit(
                        f"lifecycle_{event.event_type}",
                        **event.to_dict()
                    )
            except Exception:
                pass

            self.last_success_ts_ms = int(time.time() * 1e3)

        except Exception as e:
            logger.warning(f"LifecycleWriter.write() unexpected error: {e}")

    def log_health_summary(self):
        """Log counters to regular logger."""
        logger.info(
            f"LifecycleWriter health: total={self.events_total} "
            f"jsonl_ok={self.jsonl_writes_ok} jsonl_fail={self.jsonl_writes_failed} "
            f"db_ok={self.db_writes_ok} db_fail={self.db_writes_failed} "
            f"last_success_ts={self.last_success_ts_ms}"
        )

    def close(self):
        """Flush and close JSONL file. Log final health summary."""
        try:
            self.log_health_summary()
            self._jsonl_file.close()
            self._compress_old_runs()
        except Exception as e:
            logger.warning(f"LifecycleWriter.close() error: {e}")

    def _compress_old_runs(self):
        """Gzip JSONL files older than 1 day that aren't the current file."""
        try:
            lifecycle_dir = os.path.join(self._log_dir, "lifecycle")
            if not os.path.exists(lifecycle_dir):
                return
            now = time.time()
            one_day_ago = now - 86400
            for dirpath, _dirnames, filenames in os.walk(lifecycle_dir):
                for fname in filenames:
                    if not fname.endswith(".jsonl"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    if fpath == self._jsonl_path:
                        continue
                    if os.path.getmtime(fpath) < one_day_ago:
                        try:
                            gz_path = fpath + ".gz"
                            with open(fpath, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                                f_out.write(f_in.read())
                            os.remove(fpath)
                        except Exception as e:
                            logger.debug(f"Failed to compress {fpath}: {e}")
        except Exception as e:
            logger.debug(f"_compress_old_runs error: {e}")
