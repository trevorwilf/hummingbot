# NonKYC Connector — Phase 1 Implementation Prompt
# Copy everything below this line and paste into Claude Code
# =========================================================

You are implementing Phase 1 bug fixes for the Hummingbot NonKYC exchange connector. The root of the project is at `E/tradingsoftware/hummingbot/`.  This connector is located at `hummingbot/connector/exchange/nonkyc/` inside the project root. There are 15 code fixes and 3 test files to create.

Work through each fix sequentially. After each fix, briefly confirm what you changed. Do NOT skip any items.

---

## FIXES (in order)

### Fix 1 — WS nonce generation produces Python list repr
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_auth.py` around line 51
**Bug:** `str(random.choices(string.ascii_letters + string.digits, k=14))` returns `"['a', 'b', ...]"` instead of `"ab..."` because `random.choices()` returns a list and `str()` on a list gives the repr.
**Fix:** Change to `"".join(random.choices(string.ascii_letters + string.digits, k=14))`

### Fix 2 — REST auth nonce multiplier is 1e4 instead of 1e3
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_auth.py` around line 64
**Bug:** `int(self.time_provider.time() * 1e4)` produces timestamps 10x too large. NonKYC expects unix milliseconds.
**Fix:** Change `1e4` to `1e3`

### Fix 3 — Server time endpoint and response field are wrong
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_constants.py` around line 18
**Bug:** `SERVER_TIME_API_URL = "https://nonkyc.io/api/v2/getservertime"` — this endpoint is not in the current API docs.
**Fix:** Change to `SERVER_TIME_API_URL = "https://api.nonkyc.io/api/v2/time"`

**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_web_utils.py` around line 74
**Bug:** `server_time = response["unix_microsec"]` — wrong field name.
**Fix:** Change to `server_time = response["serverTime"]`

### Fix 4 — Order type sent as UPPERCASE but API expects lowercase
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 54
**Bug:** `return order_type.name.upper()` sends "LIMIT"/"MARKET" but NonKYC API expects "limit"/"market".
**Fix:** Change to `return order_type.name.lower()`

### Fix 5 — _place_cancel sends client_order_id but REST expects exchange_order_id
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` in the `_place_cancel` method (around line 202-218)
**Bug:** The `order_id` parameter passed by ExchangePyBase is the `client_order_id`. But REST `POST /cancelorder` expects `{"id": <exchange_internal_id>}`.
**Fix:** Change the cancel params to use `tracked_order.exchange_order_id` instead of `order_id`. Example:
```python
api_params = {
    "id": tracked_order.exchange_order_id,
}
```

### Fix 6 — Cancel result checks nonexistent "success" key
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 217
**Bug:** The cancel method checks `cancel_result.get("success")` or similar, but NonKYC returns the cancelled order object (with an "id" and "status" field), not `{"success": true}`.
**Fix:** Change the success check to verify the response contains a valid order object. For example:
```python
if cancel_result.get("id") is not None:
    return True
```
Or check `cancel_result.get("status") in ("Cancelled", "cancelled")`.

### Fix 7 — Wrong exchange_order_id in WS OrderUpdate
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 297
**Bug:** `exchange_order_id=str(message_params["userProvidedId"])` — this sets the exchange_order_id to the CLIENT id. The exchange's internal ID is in `message_params["id"]`.
**Fix:** Change to `exchange_order_id=str(message_params["id"])`

### Fix 8 — WS fill uses cumulative executedQuantity instead of per-trade tradeQuantity
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 283
**Bug:** `fill_base_amount=Decimal(message_params["executedQuantity"])` uses the cumulative total filled, not the individual fill. On partial fills this double/triple counts (e.g., fills of 1,2,3 instead of 1,1,1).
**Fix:** Change to:
```python
fill_base_amount=Decimal(message_params["tradeQuantity"]),
fill_quote_amount=Decimal(message_params["tradeQuantity"]) * Decimal(message_params["tradePrice"]),
```
Also update `fill_price` to use `message_params["tradePrice"]` if it doesn't already.

### Fix 9 — Wrong trade_id in REST fill reconciliation
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 435
**Bug:** `trade_id=str(trade["orderid"])` uses the order ID as the trade ID, causing deduplication to drop fills from the same order.
**Fix:** Change to `trade_id=str(trade["id"])`

### Fix 10 — WS trade stream expects timestampms but API sends ISO timestamp
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_order_book.py` around line 66
**Bug:** `ts = tradedata["timestampms"]` — but WS `updateTrades` sends `"timestamp": "2022-10-19T16:34:25.041Z"` (ISO string), not `timestampms`. This KeyErrors.
**Fix:** Support both formats with a fallback:
```python
if "timestampms" in tradedata:
    ts = float(tradedata["timestampms"])
else:
    # Parse ISO timestamp to milliseconds (UTC)
    from hummingbot.connector.exchange.nonkyc.nonkyc_utils import convert_fromiso_to_unix_timestamp
    ts = convert_fromiso_to_unix_timestamp(tradedata["timestamp"])
```

### Fix 11 — snapshotOrderbook WS events silently dropped
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_api_order_book_data_source.py` around line 137-139
**Bug:** `_channel_originating_message()` only routes `updateOrderbook` and `updateTrades`. The `snapshotOrderbook` event (sent after subscribing) is silently discarded, leaving the book empty until a REST snapshot poll.
**Fix:** Add handling for `"snapshotOrderbook"`. Route it to the SNAPSHOT channel (not DIFF). You'll also need to make sure `NonkycOrderBook.snapshot_message_from_exchange()` can handle the WS snapshot format which looks like:
```json
{
  "params": {
    "asks": [{"price": "50100.00", "quantity": "1.500"}],
    "bids": [{"price": "49900.00", "quantity": "2.000"}],
    "symbol": "BTC/USDT",
    "timestamp": "2026-02-26T12:00:00.000Z",
    "sequence": 1000
  }
}
```
Add `"snapshotOrderbook"` to the channel routing and ensure the snapshot parser handles this shape.

### Fix 12 — Naive datetime in ISO timestamp conversion (timezone bug)
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_utils.py` around line 29-31
**Bug:** `datetime.fromisoformat(date_str.rstrip('Z'))` produces a naive datetime. `.timestamp()` then interprets it in the server's local timezone, causing hour-level shifts.
**Fix:** Change to:
```python
def convert_fromiso_to_unix_timestamp(date_str: str) -> float:
    # Handle both "Z" suffix and "+00:00" suffix
    if date_str.endswith("Z"):
        date_str = date_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(date_str)
    return dt.timestamp() * 1e3  # return milliseconds
```
Verify the return value is milliseconds (matching NonKYC convention). Adjust callers if needed.

### Fix 13 — Incomplete ORDER_STATE map
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_constants.py` around line 66-72
**Bug:** Missing `"Expired"` state and lowercase variants. WS may send `"new"`, `"active"`, REST may send `"filled"`.
**Fix:** Expand the map to include all variants:
```python
ORDER_STATE = {
    "New": OrderState.OPEN,
    "new": OrderState.OPEN,
    "Active": OrderState.OPEN,
    "active": OrderState.OPEN,
    "Partly Filled": OrderState.PARTIALLY_FILLED,
    "partly filled": OrderState.PARTIALLY_FILLED,
    "partlyFilled": OrderState.PARTIALLY_FILLED,
    "Filled": OrderState.FILLED,
    "filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "Canceled": OrderState.CANCELED,
    "canceled": OrderState.CANCELED,
    "Expired": OrderState.CANCELED,
    "expired": OrderState.CANCELED,
    "Suspended": OrderState.CANCELED,
    "suspended": OrderState.CANCELED,
}
```
Also, wherever ORDER_STATE is accessed with a key lookup, add a `.get()` with a fallback and a warning log so unknown states don't crash:
```python
state = CONSTANTS.ORDER_STATE.get(raw_status)
if state is None:
    self.logger().warning(f"Unknown order state from NonKYC: {raw_status}")
    state = OrderState.OPEN  # safe default
```

### Fix 14 — Time synchronizer error check uses Binance code -1021
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 120-121
**Bug:** `"-1021" in error_description and "Timestamp for this request" in error_description` — this is a Binance error pattern. NonKYC never returns this, so time sync recovery never triggers.
**Fix:** Replace with NonKYC-appropriate error matching. NonKYC likely returns HTTP 401 or an error message about invalid nonce/signature when timestamps are off. Change to something like:
```python
def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
    error_description = str(request_exception)
    is_time_related = any(phrase in error_description.lower() for phrase in [
        "nonce",
        "timestamp",
        "signature",
        "unauthorized",
    ])
    return is_time_related
```

### Fix 15 — print() statement in production code
**File:** `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py` around line 495
**Bug:** `print("initializing nonkyc pairs")` — should use the logger.
**Fix:** Change to `self.logger().info("Initializing NonKYC trading pairs")` or remove entirely.

---

## TEST FILES TO CREATE

Create all test files under `test/hummingbot/connector/exchange/nonkyc/`.

### Test File 1: `test/hummingbot/connector/exchange/nonkyc/__init__.py`
Create an empty `__init__.py`.

### Test File 2: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_auth.py`

Create a test file for NonkycAuth. Use `test/hummingbot/connector/exchange/binance/test_binance_auth.py` as the structural reference. Tests must cover:

1. **test_rest_authenticate_get** — Given a known API key, secret, and mocked time (e.g., `1234567890.000`), verify that after calling `rest_authenticate()` on a GET request:
   - The `nonce` header/param is `1234567890000` (time * 1e3, integer milliseconds)
   - The signature is `HMAC-SHA256(api_key + full_url_with_params + str(nonce))` using the secret
   - The `apikey` header is set correctly

2. **test_rest_authenticate_post** — Same but for POST, where signature base includes the JSON body with no spaces: `HMAC-SHA256(api_key + url + json_body_no_spaces + str(nonce))`

3. **test_ws_authenticate_message** — Verify the WS login payload:
   - `method` is `"login"`
   - `params.nonce` is a 14-character alphanumeric string (NOT a list repr)
   - `params.signature` is `HMAC-SHA256(nonce_string)` using the secret
   - `params.apikey` is the API key

4. **test_ws_nonce_is_string_not_list_repr** — Explicitly verify the nonce does NOT contain `[`, `]`, `'`, or `,` characters. This is a regression test for the GAP-01 fix.

Look at the actual `nonkyc_auth.py` to see exactly how it structures requests and match your mock setup accordingly.

### Test File 3: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_order_book.py`

Create a test file for NonkycOrderBook. Use `test/hummingbot/connector/exchange/binance/test_binance_order_book.py` as structural reference. Tests must cover:

1. **test_snapshot_message_from_exchange** — Pass a mock NonKYC REST `/market/orderbook` response:
   ```python
   msg = {
       "bids": [{"price": "49900.00", "quantity": "2.000"}],
       "asks": [{"price": "50100.00", "quantity": "1.500"}],
       "symbol": "BTC/USDT",
       "sequence": 1000,
       "timestamp": "2026-02-26T12:00:00.000Z"
   }
   ```
   Verify: correct trading_pair, type is SNAPSHOT, bids/asks parsed correctly, `update_id` uses `sequence` (1000).

2. **test_snapshot_message_from_ws** — Same but using the WS `snapshotOrderbook` `params` shape (should be identical structure).

3. **test_diff_message_from_exchange** — Pass a mock WS `updateOrderbook` payload. Verify `update_id` is set from `sequence`, not `timestamp`.

4. **test_trade_message_from_exchange_with_iso_timestamp** — Pass a trade with `"timestamp": "2022-10-19T16:34:25.041Z"` (no `timestampms`). Verify it parses correctly to a UTC millisecond value without KeyError.

5. **test_trade_message_from_exchange_with_timestampms** — Pass a trade with `"timestampms": 1666197265041`. Verify backward compatibility.

### Test File 4: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_utils.py`

Tests for utility functions:

1. **test_convert_fromiso_to_unix_timestamp_with_z_suffix** — Input: `"2022-10-19T16:34:25.041Z"`. Verify output is `1666197265041.0` (UTC milliseconds).

2. **test_convert_fromiso_to_unix_timestamp_with_offset** — Input: `"2022-10-19T16:34:25.041+00:00"`. Same expected output.

3. **test_convert_fromiso_to_unix_timestamp_timezone_correctness** — Verify the output is the SAME regardless of the machine's local timezone. This is the regression test for GAP-12. You can test this by comparing against a known UTC epoch value.

4. **test_is_exchange_information_valid** — If there's a market active filter function, test it with `{"isActive": True}` and `{"isActive": False}`.

---

## IMPORTANT NOTES

- Do NOT modify any files outside `hummingbot/connector/exchange/nonkyc/` and `test/hummingbot/connector/exchange/nonkyc/`.
- After all fixes, run: `python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v` to verify the new tests pass.
- If any test imports fail due to missing dependencies in the test environment, note what's missing but don't modify project-level configs.
- Commit message suggestion: `fix(nonkyc): Phase 1 — critical auth, order, and parsing fixes + initial test suite`
- Create a feature branch before starting: `git checkout -b fix/nonkyc-phase1`
