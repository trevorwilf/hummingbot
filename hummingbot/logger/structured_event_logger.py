"""
Structured event logger for execution provenance.
Writes JSONL to logs/structured_events.jsonl for downstream quant analysis.
"""
import json
import logging
import os
import time
import uuid
from logging.handlers import RotatingFileHandler
from typing import Optional


class StructuredEventLogger:
    """
    Singleton logger that writes structured JSON events to a dedicated JSONL file.
    Thread-safe, fail-silent, zero impact on trading loop.
    """
    _instance: Optional["StructuredEventLogger"] = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._session_id = str(uuid.uuid4())[:8]
        self._bot_run_id: Optional[str] = None
        self._logger = logging.getLogger("structured_events")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False  # Don't send to root logger
        self._setup = False

    def setup(self, log_dir: str = "logs", max_bytes: int = 50_000_000, backup_count: int = 5):
        """Initialize the file handler. Call once at startup."""
        if self._setup:
            return
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "structured_events.jsonl")
            handler = RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            handler.setLevel(logging.INFO)
            # No formatter — we write raw JSON lines
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._setup = True
        except Exception as e:
            # Fall back to stderr warning, never crash
            import sys
            print(f"WARNING: StructuredEventLogger setup failed: {e}", file=sys.stderr)

    def set_bot_run_id(self, bot_run_id: str):
        """Set the bot_run_id to include in every subsequent event."""
        self._bot_run_id = bot_run_id

    @property
    def bot_run_id(self) -> Optional[str]:
        return self._bot_run_id

    def emit(self, event_type: str, **payload):
        """Emit a structured event. Never raises."""
        try:
            # Lazy setup on first emit if not already set up
            if not self._setup:
                self.setup()
            event = {
                "event_type": event_type,
                "event_id": str(uuid.uuid4()),
                "event_version": 1,
                "schema_name": "structured_v1",
                "session_id": self._session_id,
                "timestamp_ms": int(time.time() * 1e3),
            }
            if self._bot_run_id is not None:
                event["bot_run_id"] = self._bot_run_id
            event.update(payload)
            self._logger.info(json.dumps(event, default=str, separators=(",", ":")))
        except Exception:
            pass  # Absolutely never break the caller


def get_structured_logger() -> StructuredEventLogger:
    """Get the singleton structured event logger."""
    return StructuredEventLogger()
