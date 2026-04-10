import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


@dataclass
class LifecycleEvent:
    """Canonical order lifecycle event. Single source of truth for DB + JSONL writes."""
    event_type: str                                    # submit_acked, fill, cancel_confirmed, etc.
    connector: str
    trading_pair: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_version: int = 1
    bot_run_id: Optional[str] = None
    emitted_ts_ms: int = field(default_factory=lambda: int(time.time() * 1e3))
    exchange_ts_ms: Optional[int] = None
    received_ts_ms: Optional[int] = None
    source_channel: Optional[str] = None
    client_order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    exchange_trade_id: Optional[str] = None
    controller_id: Optional[str] = None
    executor_id: Optional[str] = None
    level_id: Optional[str] = None
    trade_type: Optional[str] = None                   # BUY/SELL
    order_type: Optional[str] = None                   # LIMIT/MARKET
    position_action: Optional[str] = None
    price: Optional[str] = None
    amount: Optional[str] = None
    cum_fill_qty: Optional[str] = None
    fee_json: Optional[Dict] = None
    fee_in_quote: Optional[str] = None
    liquidity_role: Optional[str] = None
    best_bid: Optional[str] = None
    best_ask: Optional[str] = None
    mid_price: Optional[str] = None
    spread_bps: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSONL. Drops None values for compact output."""
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    def to_db_kwargs(self) -> Dict[str, Any]:
        """Serialize for OrderLifecycleEvent ORM constructor."""
        return asdict(self)
