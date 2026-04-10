# Lifecycle Event Schema Reference

## Overview

Lifecycle events capture every state transition for every order placed by Hummingbot. They are the authoritative data source for simulator calibration, providing calibration-grade timestamps, fill provenance, and strategy lineage.

Events are emitted by the `LifecycleWriter` component in `hummingbot/persistence/lifecycle_writer.py` and written to both:
1. **JSONL files** (per-run, strict, schema-versioned) - the primary data product
2. **PostgreSQL/SQLite `OrderLifecycleEvent` table** - for live queries

## File Layout

```
logs/lifecycle/
├── 2026-04-09/
│   ├── abc12345-6789-...jsonl      # active run
│   └── def98765-4321-...jsonl.gz   # completed run (auto-compressed after 1 day)
├── 2026-04-08/
│   └── ...
```

Each file corresponds to one bot run (`bot_run_id`). Files are gzipped automatically after 1 day.

## Event Envelope

Fields present on **every** event:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | string (UUID) | yes | Unique event ID, dedup key |
| `event_type` | string | yes | Lifecycle stage (see vocabulary below) |
| `event_version` | int | yes | Schema version (currently `1`) |
| `schema_name` | string | yes | Always `"lifecycle_v1"` |
| `bot_run_id` | string (UUID) | yes | Links event to a specific bot execution session |
| `emitted_ts_ms` | int (Unix ms) | yes | Local wall-clock time when event was recorded |

## Event Type Vocabulary

| event_type | When emitted | Key fields |
|------------|-------------|------------|
| `bot_run_started` | Bot starts | `strategy_name`, `connectors`, `trading_pairs` |
| `bot_run_stopped` | Bot stops | `stop_reason` |
| `submit_acked` | Exchange confirms order creation | `client_order_id`, `exchange_order_id`, `price`, `amount`, `trade_type`, `order_type` |
| `fill` | Partial or full fill | `exchange_trade_id`, `price`, `amount`, `cum_fill_qty`, `fee_json`, `liquidity_role`, `source_channel` |
| `cancel_confirmed` | Cancel confirmed by exchange | `client_order_id`, `exchange_order_id` |
| `completed` | Order fully filled | `client_order_id` |
| `failed` | Order failed | `client_order_id` |
| `expired` | Order expired | `client_order_id` |

### Supplementary Connector Events

These are emitted by the connector layer (NonKYC/MEXC) and provide additional context. They appear in `structured_events.jsonl` but NOT in the per-run lifecycle files.

| event_type | Description |
|------------|-------------|
| `order_submit_requested` | Connector sent order to exchange REST API |
| `order_submit_rejected` | Exchange rejected the order |
| `order_cancel_requested` | Connector sent cancel to exchange REST API |
| `connector_fill_received` | Raw fill event from WS/REST (before MarketsRecorder processing) |

## Full Field Reference

### Identity & Linkage

| Field | Type | When populated | Description |
|-------|------|----------------|-------------|
| `client_order_id` | string | All order events | Hummingbot-assigned order ID |
| `exchange_order_id` | string | After exchange confirms | Exchange-assigned order ID |
| `exchange_trade_id` | string | Fill events only | Exchange-assigned trade/fill ID |
| `bot_run_id` | string (UUID) | Always | Links to `BotRun` table |

### Strategy Lineage

| Field | Type | Description |
|-------|------|-------------|
| `controller_id` | string | Controller instance that originated the order |
| `executor_id` | string | Executor instance (PositionExecutor, DCAExecutor, etc.) |
| `level_id` | string | Grid/level identifier within the executor |

### Order Details

| Field | Type | Description |
|-------|------|-------------|
| `connector` | string | Exchange connector name (e.g., `"nonkyc"`, `"mexc"`) |
| `trading_pair` | string | Hummingbot format: `"BTC-USDT"` |
| `trade_type` | string | `"BUY"` or `"SELL"` |
| `order_type` | string | `"LIMIT"`, `"MARKET"`, etc. |
| `position_action` | string | `"OPEN"`, `"CLOSE"`, `"NIL"` |
| `price` | string | Order price (string-encoded decimal) |
| `amount` | string | Order amount (string-encoded decimal) |
| `cum_fill_qty` | string | Cumulative filled base amount (fill events) |

### Fee & Execution

| Field | Type | Description |
|-------|------|-------------|
| `fee_json` | object | Fee breakdown: `{"percent": 0.001, "flat_fees": [...]}` |
| `fee_in_quote` | string | Total fee expressed in quote currency |
| `liquidity_role` | string | `"maker"` or `"taker"` (from exchange) |
| `source_channel` | string | `"ws"` (WebSocket live feed) or `"rest_poll"` (periodic REST polling) |

### Timestamps

| Field | Type | Description |
|-------|------|-------------|
| `emitted_ts_ms` | int | When Hummingbot recorded the event (local wall-clock, Unix ms) |
| `exchange_ts_ms` | int | Exchange-reported event time (Unix ms). May differ from `emitted_ts_ms` due to network latency. |
| `received_ts_ms` | int | When the WS/REST message was received locally (Unix ms) |

**Latency calculation:** `received_ts_ms - exchange_ts_ms` approximates one-way network + processing latency.

### Quote Context (BBO Snapshot)

Captured at the moment the event is recorded. May be `null` if the order book is unavailable (e.g., during reconnect).

| Field | Type | Description |
|-------|------|-------------|
| `best_bid` | string | Best bid price at event time |
| `best_ask` | string | Best ask price at event time |
| `mid_price` | string | `(best_bid + best_ask) / 2` |
| `spread_bps` | string | Spread in basis points: `(best_ask - best_bid) / mid_price * 10000` |

### Catch-all

| Field | Type | Description |
|-------|------|-------------|
| `payload` | object | Connector-specific or event-specific extra data |

## Replay Instructions

```bash
# Install dependencies
pip install sqlalchemy psycopg2-binary

# Replay a single file
python scripts/replay_lifecycle_jsonl_to_db.py \
    --file logs/lifecycle/2026-04-09/abc123.jsonl \
    --db-url "postgresql+psycopg2://user:pass@host:5432/dbname"

# Replay all files in a directory
python scripts/replay_lifecycle_jsonl_to_db.py \
    --dir logs/lifecycle/ \
    --db-url "postgresql+psycopg2://user:pass@host:5432/dbname"

# Validate files without inserting (dry run)
python scripts/replay_lifecycle_jsonl_to_db.py \
    --file logs/lifecycle/2026-04-09/abc123.jsonl \
    --validate-only
```

The replay tool is **idempotent** - running it twice on the same file will not create duplicate rows (dedup by `event_id`).

## Versioning Contract

- `event_version` increments when **breaking** changes are made to the schema
- The replay tool rejects events with `event_version` greater than its known maximum
- Older versions are accepted as-is (forward-compatible additions only)
- New fields may be added without incrementing the version (they will be `null` in older events)

## Example JSONL Lines

### submit_acked
```json
{"event_type":"submit_acked","connector":"nonkyc","trading_pair":"BTC-USDT","event_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","event_version":1,"schema_name":"lifecycle_v1","bot_run_id":"run-abc-123","emitted_ts_ms":1712700000000,"client_order_id":"buy-btc-001","exchange_order_id":"12345678","trade_type":"BUY","order_type":"LIMIT","price":"65000.00","amount":"0.01","best_bid":"64999.50","best_ask":"65000.50","mid_price":"65000.0","spread_bps":"1.54"}
```

### fill
```json
{"event_type":"fill","connector":"nonkyc","trading_pair":"BTC-USDT","event_id":"b2c3d4e5-f6a7-8901-bcde-f12345678901","event_version":1,"schema_name":"lifecycle_v1","bot_run_id":"run-abc-123","emitted_ts_ms":1712700005000,"exchange_ts_ms":1712700004800,"received_ts_ms":1712700004950,"client_order_id":"buy-btc-001","exchange_order_id":"12345678","exchange_trade_id":"t-99887766","trade_type":"BUY","order_type":"LIMIT","price":"65000.00","amount":"0.005","cum_fill_qty":"0.005","fee_json":{"percent":0,"flat_fees":[{"amount":"0.0325","token":"USDT"}]},"liquidity_role":"maker","source_channel":"ws"}
```

### cancel_confirmed
```json
{"event_type":"cancel_confirmed","connector":"mexc","trading_pair":"ETH-USDT","event_id":"c3d4e5f6-a7b8-9012-cdef-123456789012","event_version":1,"schema_name":"lifecycle_v1","bot_run_id":"run-abc-123","emitted_ts_ms":1712700010000,"client_order_id":"sell-eth-002","exchange_order_id":"87654321","trade_type":"SELL"}
```

### bot_run_started
```json
{"event_type":"bot_run_started","event_id":"d4e5f6a7-b8c9-0123-defa-234567890123","event_version":1,"schema_name":"lifecycle_v1","bot_run_id":"run-abc-123","emitted_ts_ms":1712699990000,"strategy_name":"pmm_dynamic","connectors":["nonkyc","mexc"],"trading_pairs":["BTC-USDT","ETH-USDT"]}
```
