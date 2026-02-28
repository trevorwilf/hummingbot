# NonKYC.io Exchange Connector

Hummingbot connector for [NonKYC.io](https://nonkyc.io), a non-custodial
centralized exchange. This connector supports spot trading via REST + WebSocket
APIs using HMAC-SHA256 authentication.

## Architecture

| File | Purpose |
|------|---------|
| `nonkyc_exchange.py` | Main connector: order lifecycle, balances, fill polling |
| `nonkyc_auth.py` | HMAC-SHA256 auth for REST (headers) and WS (login message) |
| `nonkyc_api_order_book_data_source.py` | Public WS: order book snapshots/diffs, trade stream |
| `nonkyc_api_user_stream_data_source.py` | Private WS: order reports, balance updates |
| `nonkyc_order_book.py` | Order book message parsing (snapshot, diff, trade) |
| `nonkyc_constants.py` | API endpoints, WS methods, rate limits, order states |
| `nonkyc_utils.py` | Config map, fee schema, symbol validation |
| `nonkyc_web_utils.py` | URL builders, API factory, server time |

Rate oracle source: `hummingbot/core/rate_oracle/sources/nonkyc_rate_source.py`

## NonKYC-Specific Patterns

### Symbol Format
- **Exchange native**: `BTC/USDT` (slash-separated)
- **Hummingbot internal**: `BTC-USDT` (dash-separated)
- Mapping is handled by `_initialize_trading_pair_symbols_from_exchange_info`

### Authentication
- **REST**: HMAC-SHA256 via headers `X-API-KEY`, `X-API-NONCE`, `X-API-SIGN`
  - GET: `sign(apiKey + fullURL + nonce)` (params sorted for canonical form)
  - POST: `sign(apiKey + url + minifiedJSON + nonce)` (body = what was signed)
- **WebSocket**: JSON-RPC 2.0 `login` method with `pKey`, `nonce`, `signature`

### WebSocket Protocol
- JSON-RPC 2.0 over `wss://api.nonkyc.io`
- All requests include `"id"` field for request-response correlation
- Server sends `snapshotOrderbook` (full) then `updateOrderbook` (incremental diffs)
- Sequence numbers for gap detection (string in REST, int in WS -- both handled)
- Auth: send `login`, wait for `{"result": true}`, then subscribe to private channels

### Order Types
- NonKYC supports `limit` and `market` only
- `LIMIT_MAKER` is mapped to `limit` -- **no native post-only support**
- Maker/taker classification uses the `triggeredBy` field from trade history

### Balance Fields
- `available` = free for new orders
- `held` = locked in open orders
- `pending` = unconfirmed deposits/withdrawals (excluded from trading balance)

### Fee System
- Default: 0.15% maker, 0.15% taker (from `nonkyc_utils.DEFAULT_FEES`)
- Dynamic: computed from actual trade history via `_update_trading_fees`
- Supports `alternateFeeAsset` for future fee-token discounts

### Rate Limits
- **Estimated values** -- NonKYC does not publish rate limit documentation
- See `nonkyc_constants.py` for tuning guidance

## Known Limitations

1. **No post-only orders**: LIMIT_MAKER maps to regular limit; can cross spread
2. **Estimated rate limits**: May need tuning for high-frequency strategies
3. **Single domain**: `DEFAULT_DOMAIN` is a compatibility placeholder, not used in URLs
4. **Cancel by symbol required**: `/cancelallorders` requires a `symbol` parameter
5. **No native stop-loss/take-profit**: Only `limit` and `market` order types

## Running Tests

### Unit tests (offline, no API keys needed)
```bash
pytest test/hummingbot/connector/exchange/nonkyc/ -v
```

### Live API validation (uses real endpoints + optionally API keys)
```bash
# Set keys for authenticated tests (optional -- unauthenticated tests still run)
export NONKYC_API_KEY="your_key"
export NONKYC_API_SECRET="your_secret"

python test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py
```

### Auth-only smoke test
```bash
python test/hummingbot/connector/exchange/nonkyc/nonkyc_auth_test.py
```

## Phase History

| Phase | What was built |
|-------|---------------|
| 1 | Base connector: REST, WS skeleton, auth, symbol mapping |
| 2 | Order book data source: snapshot + diff, trade stream |
| 3 | User stream: WS auth, order/balance events, fill polling |
| 4 | Paper trading integration |
| 5A | LIMIT_MAKER mapping, Decimal precision, cancel_all endpoint |
| 5B | WS auth timeout/retry, unsubscribe, defensive parsing |
| 5C | Dynamic fee system from trade history |
| 5D | Crash recovery: batch cancel, orphan detection |
| 6 | Rate oracle source |
| 7A | Auth hardening: POST minify, GET param sort, fee token, etc. |
| 7B | WS JSON-RPC id compliance, code quality, documentation |
