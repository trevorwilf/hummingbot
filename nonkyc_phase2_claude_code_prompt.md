# NonKYC Connector — Phase 2 Implementation Prompt
# Copy everything below this line and paste into Claude Code
# =========================================================

You are implementing Phase 2 (Reliability & State Recovery) fixes for the Hummingbot NonKYC exchange connector. Phase 1 (critical auth/parsing fixes) has been completed and validated against the live API.

The root of the project is at `E/tradingsoftware/hummingbot/`
The connector is at `hummingbot/connector/exchange/nonkyc/`.
Tests go in `test/hummingbot/connector/exchange/nonkyc/`.

This phase has 9 code fixes plus 4 test files to create, plus 2 live API test scripts to install. Work through each fix sequentially.

---

## IMPORTANT: Live API Intelligence (Verified Feb 27, 2026)

These are REAL responses from the NonKYC production API. Use these exact field names and structures in all code and test mocks.

### GET /market/getlist — Market object fields:
```json
{
  "symbol": "IMX/USDT",
  "primaryTicker": "IMX",
  "primaryName": "ImmutableX",
  "priceDecimals": 4,
  "quantityDecimals": 2,
  "isActive": true,
  "isPaused": false,
  "minimumQuantity": 2,
  "minQuote": 1,
  "isMinQuoteActive": true,
  "allowMarketOrders": true,
  "allowTriggerOrders": true,
  "bestAsk": "0.1714",
  "bestBid": "0.1700",
  "lastPrice": "0.1706",
  "volume": "112345.8300",
  "id": "643bfabe4f63469320902710"
}
```
NOTE: There is NO `secondaryTicker` field. Quote ticker is derived from `symbol.split("/")[1]`.
NOTE: `minimumQuantity` is the min order size. `minQuote` is the min notional (quote value). These should be used in trading rules.

### GET /market/orderbook — Response:
```json
{
  "marketid": "643bfeeb5e07bba23a98a981",
  "symbol": "BTC/USDT",
  "timestamp": 1772169899391,
  "sequence": "6064",
  "bids": [{"price": "67679.55", "quantity": "0.000422"}],
  "asks": [{"price": "67883.06", "quantity": "0.010917"}]
}
```
CRITICAL: `sequence` is a STRING in the REST response. Must use `int(data["sequence"])`.

### GET /tickers — Response (list of):
```json
{
  "ticker_id": "IMX_USDT",
  "base_currency": "IMX",
  "target_currency": "USDT",
  "last_price": "0.1706",
  "bid": "0.1700",
  "ask": "0.1714",
  "high": "0.1733",
  "low": "0.1629",
  "base_volume": "112345.8300",
  "target_volume": "18969.8608"
}
```

### GET /account/trades — Response (authenticated, list of):
```json
{
  "id": "69914db4b3e90040c2b8f0d8",
  "market": {"id": "66be1b9ae1d5ae4126f68be4", "symbol": "SAL/USDT"},
  "orderid": "6990d8e81be80212916d57f9",
  "side": "Buy",
  "triggeredBy": "sell",
  "price": "0.021118",
  "quantity": "1771.1985",
  "fee": "0.074808339846",
  "totalWithFee": "37.478978262846",
  "timestamp": 1771130292620
}
```
NOTE: The `timestamp` field here is already in milliseconds (integer), NOT ISO format.
NOTE: There is NO `updatedAt` or `createdAt` field — just `timestamp`.
NOTE: The `market` field is a nested object with `id` and `symbol`.

### REST Auth Headers (confirmed working):
```
X-API-KEY: <api_key>
X-API-NONCE: <unix_ms_timestamp>
X-API-SIGN: HMAC-SHA256(api_key + full_url_with_params + nonce)
```

### WS snapshotOrderbook:
```json
{
  "jsonrpc": "2.0",
  "method": "snapshotOrderbook",
  "params": {
    "symbol": "BTC/USDT",
    "sequence": 1215881,
    "asks": [{"price": "67883.06", "quantity": "0.010917"}],
    "bids": [{"price": "67679.55", "quantity": "0.000422"}]
  }
}
```
NOTE: `sequence` in WS is an INTEGER (not string like REST).

### WS updateOrderbook:
```json
{
  "jsonrpc": "2.0",
  "method": "updateOrderbook",
  "params": {
    "asks": [...],
    "bids": [...],
    "symbol": "BTC/USDT",
    "timestamp": 1772170410000,
    "sequence": 1215882
  }
}
```

### WS snapshotTrades:
```json
{
  "jsonrpc": "2.0",
  "method": "snapshotTrades",
  "params": {
    "symbol": "BTC/USDT",
    "sequence": "6064",
    "data": [
      {
        "id": "69a12a99f65594545010e592",
        "price": "67799.74",
        "quantity": "0.020323",
        "side": "sell",
        "timestamp": "2026-02-27T05:24:41.018Z",
        "timestampms": 1772169881018
      }
    ]
  }
}
```
NOTE: Trade entries have BOTH `timestamp` (ISO) and `timestampms` (int ms).

### WS Login + subscribeReports → activeOrders:
```json
{"jsonrpc": "2.0", "result": true, "id": 99}
```
After subscribeReports:
```json
{
  "jsonrpc": "2.0",
  "method": "activeOrders",
  "result": [],
  "id": 100
}
```
NOTE: activeOrders uses `result` key (a list), NOT `params`.

### WS subscribeBalances → currentBalances:
```json
{
  "jsonrpc": "2.0",
  "method": "currentBalances",
  "timestamp": 1772170422567,
  "portfolioData": {
    "totalUsdValue": "5.82",
    "totalBtcValue": "0.00008593"
  },
  "result": [
    {
      "assetId": "6982a6966fe9953e2576d260",
      "ticker": "USDT",
      "available": "5.82701928",
      "held": "0.00000000",
      "changePercent": 0
    }
  ]
}
```
NOTE: `subscribeBalances` IS functional (confirmed live). DO NOT remove it. It returns `currentBalances` events with balance data in `result`.

### WS getTradingBalance:
```json
{
  "jsonrpc": "2.0",
  "result": [
    {"asset": "USDT", "available": "5.82701928", "held": "0.00000000"},
    {"asset": "SAL", "available": "0.00000000", "held": "0.00000000"}
  ],
  "id": 102
}
```

---

## FIXES (in order)

### Fix 1 — Orderbook sequence tracking + gap detection + resync (GAP-13)

**Files:** `nonkyc_api_order_book_data_source.py`, `nonkyc_order_book.py`

The connector currently ignores the `sequence` field in orderbook updates. This means missed WS messages silently corrupt the book.

**What to implement:**

In `nonkyc_order_book.py`:
- `snapshot_message_from_exchange()` must set `update_id = int(msg.get("sequence", 0))` (note: REST returns sequence as string)
- `diff_message_from_exchange()` must set `update_id = int(msg.get("sequence", 0))` instead of using timestamp

In `nonkyc_api_order_book_data_source.py`:
- Add a `_last_sequence: Dict[str, int]` dict to track last seen sequence per trading pair
- When processing a `snapshotOrderbook`, store the sequence and route to snapshot queue
- When processing an `updateOrderbook`, check:
  - If sequence <= last seen: skip (duplicate)
  - If sequence == last_seen + 1: normal, process diff
  - If sequence > last_seen + 1: GAP detected → log warning and trigger a REST snapshot resync
- After REST snapshot, reset the tracked sequence

Use KuCoin's `kucoin_api_order_book_data_source.py` as a reference pattern — it does exactly this with `sequenceStart`/`sequenceEnd`.

### Fix 2 — Keep subscribeBalances and properly handle currentBalances (GAP-15 — UPDATED)

**File:** `nonkyc_api_user_stream_data_source.py`, `nonkyc_exchange.py`

The original plan said to remove `subscribeBalances` because it was undocumented. Live testing confirmed it WORKS and returns `currentBalances` events. So KEEP it, but ensure the exchange properly handles the `currentBalances` WS event.

In `nonkyc_exchange.py`, in `_user_stream_event_listener()`:
- Add handling for `event_type == "currentBalances"`. The balance data is in `event_message.get("result", [])`. Each entry has:
  ```python
  {"ticker": "USDT", "available": "5.82701928", "held": "0.00000000"}
  ```
- Update `_account_balances` and `_account_available_balances` from this data.

Also confirm the connector has `@property real_time_balance_update` returning `True` so it uses WS balance updates instead of polling.

### Fix 3 — Handle activeOrders snapshot for reconnect reconciliation (GAP-16)

**File:** `nonkyc_exchange.py`

In `_user_stream_event_listener()`, add handling for `event_type == "activeOrders"`:

The data comes in `event_message.get("result", [])` (NOT `params` — confirmed from live test).

Each order in the list should have fields like: `id`, `userProvidedId`, `symbol`, `side`, `status`, `type`, `quantity`, `price`, `executedQuantity`, `createdAt`, `updatedAt`.

Implementation:
```python
if event_type == "activeOrders":
    active_orders = event_message.get("result", [])
    for order_data in active_orders:
        client_order_id = str(order_data.get("userProvidedId", ""))
        tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
        if tracked_order is not None:
            new_state = CONSTANTS.ORDER_STATE.get(order_data.get("status", ""), OrderState.OPEN)
            order_update = OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=order_data.get("updatedAt", 0) * 1e-3,
                new_state=new_state,
                client_order_id=client_order_id,
                exchange_order_id=str(order_data.get("id", "")),
            )
            self._order_tracker.process_order_update(order_update)
```

### Fix 4 — Validate WS auth response after login (GAP-17)

**File:** `nonkyc_api_user_stream_data_source.py`

The `_authenticate_ws_connection` method currently fires the login message and returns immediately without checking the response.

Fix: After sending the auth message, wait for and verify the response:
```python
async def _authenticate_ws_connection(self, ws: WSAssistant):
    auth_message = WSJSONRequest(payload=self._auth.generate_ws_authentication_message())
    await ws.send(auth_message)

    # Wait for auth response
    async for ws_response in ws.iter_messages():
        data = ws_response.data
        if isinstance(data, dict):
            if data.get("result") is True:
                self.logger().info("WebSocket authentication successful")
                return
            elif "error" in data:
                error_msg = data.get("error", {}).get("message", "Unknown error")
                raise IOError(f"WebSocket authentication failed: {error_msg}")
        break  # unexpected message format, continue anyway
```

### Fix 5 — Override _on_user_stream_interruption (GAP-18)

**File:** `nonkyc_api_user_stream_data_source.py`

Add this method override for cleanup on disconnect:
```python
async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
    websocket_assistant and await websocket_assistant.disconnect()
    self._ws_assistant = None
```

This ensures a clean reconnect cycle. Without it, stale WS connections may linger.

### Fix 6 — Migrate fill reconciliation to /account/trades (GAP-22)

**Files:** `nonkyc_constants.py`, `nonkyc_exchange.py`

The connector currently uses `/gettrades` and `/gettradessince` (deprecated). The live API confirms `/account/trades` works and returns the `Trade2` schema.

**In `nonkyc_constants.py`:**
- Add: `ACCOUNT_TRADES_PATH_URL = "/account/trades"`
- Add a rate limit entry for it
- Keep the old constants for now but mark with comments as deprecated

**In `nonkyc_exchange.py` `_update_order_fills_from_trades()`:**
- Replace the endpoint from `USER_TRADES_PATH_URL` / `USER_TRADES_SINCE_A_TIMESTAMP_PATH_URL` with `ACCOUNT_TRADES_PATH_URL`
- Use proper params: the endpoint supports `symbol`, `since` (ms timestamp), `limit`, `skip`
- Fix the `symbol` param format: the live API uses `"SAL/USDT"` format (with slash), NOT `"SAL_USDT"` (underscore). Remove the `.replace("/", "_")` call.
- Fix the timestamp field: the live Trade2 object has `"timestamp"` (ms integer), NOT `"updatedAt"` or `"createdAt"`. Update `fill_timestamp` to use `trade["timestamp"] * 1e-3`

**In `nonkyc_exchange.py` `_all_trade_updates_for_order()`:**
- Same endpoint migration
- Fix `trade_id`: currently uses `str(trade["orderid"])` — must be `str(trade["id"])`
- Fix `fill_timestamp`: use `trade["timestamp"] * 1e-3`

### Fix 7 — Add minimumQuantity and minQuote to trading rules (GAP-21)

**File:** `nonkyc_exchange.py`

In `_format_trading_rules()`, the live API provides `minimumQuantity` and `minQuote` fields. Update the TradingRule to use them:

```python
retval.append(
    TradingRule(
        trading_pair,
        min_order_size=Decimal(str(rule.get("minimumQuantity", min_base_amount_increment))),
        min_price_increment=min_price_increment,
        min_base_amount_increment=min_base_amount_increment,
        min_notional_size=Decimal(str(rule.get("minQuote", 0))) if rule.get("isMinQuoteActive") else Decimal("0"),
    )
)
```

### Fix 8 — Improve error logging with context (GAP-24)

**File:** `nonkyc_exchange.py`

Find all broad `except Exception` blocks and add context. For example:
- In `_place_order`: add order details (trading_pair, amount, price, order_type) to the error log
- In `_place_cancel`: add order_id and exchange_order_id
- In `_request_order_status`: add client_order_id
- In `_update_order_fills_from_trades`: add trading_pair to the error log
- In `_user_stream_event_listener`: add event_type and any available IDs

Pattern:
```python
except Exception as e:
    self.logger().error(
        f"Error processing {event_type} for {trading_pair}: {e}",
        exc_info=True
    )
```

### Fix 9 — Handle snapshotTrades WS event (discovered in live test)

**File:** `nonkyc_api_order_book_data_source.py`

The live API sends `snapshotTrades` after subscribing to trades (confirmed in Tier 1B WS test). The connector currently only handles `updateTrades`. Route `snapshotTrades` to the trade queue as well.

In `_channel_originating_message()`, add `"snapshotTrades"` alongside `"updateTrades"` for the TRADE channel.

The snapshotTrades params contain a `data` list of trade objects, while updateTrades params may be structured differently. Make sure the trade message parser handles both formats.

---

## TEST FILES TO CREATE

### Test File 1: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_exchange.py`

This is the BIG test file. It inherits from `AbstractExchangeConnectorTests.ExchangeConnectorTests` which provides ~40 built-in test methods. You must implement ~50 abstract properties and methods.

**Use `test/hummingbot/connector/exchange/binance/test_binance_exchange.py` (1,376 lines) as your structural template.** Read it first.

```python
import asyncio
import json
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses
from aioresponses.core import RequestCall

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS, nonkyc_web_utils as web_utils
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_client_order_id
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase


class NonkycExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):
    ...
```

**All mock responses must use the REAL API shapes documented in the "Live API Intelligence" section above.** Do NOT copy Binance response formats — use NonKYC formats.

Here is the COMPLETE list of abstract properties/methods you must implement:

**URL Properties:**
- `all_symbols_url` → URL for `MARKETS_INFO_PATH_URL`
- `latest_prices_url` → URL for `TICKER_INFO_PATH_URL` with symbol param
- `network_status_url` → URL for server time or ping
- `trading_rules_url` → URL for `MARKETS_INFO_PATH_URL`
- `order_creation_url` → URL for `CREATE_ORDER_PATH_URL`
- `balance_url` → URL for `BALANCE_PATH_URL`

**Mock Response Properties (use REAL NonKYC shapes from above):**
- `all_symbols_request_mock_response` → Full /market/getlist response (list with one market object including ALL fields shown above: symbol, primaryTicker, priceDecimals, quantityDecimals, isActive, minimumQuantity, minQuote, etc.)
- `latest_prices_request_mock_response` → Ticker response for one pair
- `all_symbols_including_invalid_pair_mock_response` → Tuple of (invalid_pair_str, response_with_invalid_pair)
- `network_status_request_successful_mock_response` → `{"serverTime": 1772170404982}`
- `trading_rules_request_mock_response` → Same as all_symbols (NonKYC returns rules in market list)
- `trading_rules_request_erroneous_mock_response` → Market object with missing fields
- `order_creation_request_successful_mock_response` → NonKYC create order response
- `balance_request_mock_response_for_base_and_quote` → Balance list with base and quote entries
- `balance_request_mock_response_only_base` → Balance list with only base
- `balance_event_websocket_update` → `currentBalances` WS event (use the exact shape from live test above)

**Expected Values:**
- `expected_latest_price` → Decimal matching your mock
- `expected_supported_order_types` → `[OrderType.LIMIT, OrderType.MARKET]`
- `expected_trading_rule` → TradingRule with min_order_size from minimumQuantity, min_notional_size from minQuote
- `expected_logged_error_for_erroneous_trading_rule` → Error message for bad rule
- `expected_exchange_order_id` → String ID matching your mock
- `is_order_fill_http_update_included_in_status_update` → True or False
- `is_order_fill_http_update_executed_during_websocket_order_event_processing` → True or False
- `expected_partial_fill_price` → Decimal
- `expected_partial_fill_amount` → Decimal
- `expected_fill_fee` → TradeFeeBase instance
- `expected_fill_trade_id` → String

**Factory Methods:**
- `exchange_symbol_for_tokens(base, quote)` → `f"{base}/{quote}"` (NonKYC uses slash separator)
- `create_exchange_instance()` → `NonkycExchange(nonkyc_api_key="test", nonkyc_secret_key="test", trading_pairs=["COINALPHA-HBOT"], trading_required=False)`
- `validate_auth_credentials_present(request)` → Check for X-API-KEY, X-API-NONCE, X-API-SIGN headers
- `validate_order_creation_request(order, request)` → Verify params match order
- `validate_order_cancelation_request(order, request)` → Verify cancel params
- `validate_order_status_request(order, request)` → Verify status request
- `validate_trades_request(order, request)` → Verify trades request

**Configure Methods (set up mock API responses):**
- `configure_successful_cancelation_response` → Mock POST /cancelorder returning order object with status Cancelled
- `configure_erroneous_cancelation_response` → Mock returning 400
- `configure_order_not_found_error_cancelation_response` → Mock returning error code 20002
- `configure_one_successful_one_erroneous_cancel_all_response` → Two mocks, first success then fail
- `configure_completely_filled_order_status_response` → GET /getorder/{id} returning status "Filled"
- `configure_canceled_order_status_response` → Returning status "Cancelled"
- `configure_open_order_status_response` → Returning status "New" or "Active"
- `configure_http_error_order_status_response` → Returning 400/500
- `configure_partially_filled_order_status_response` → Returning status "Partly Filled" with executedQuantity
- `configure_order_not_found_error_order_status_response` → Error code 20002
- `configure_partial_fill_trade_response` → GET /account/trades returning partial fill
- `configure_erroneous_http_fill_trade_response` → Returning error
- `configure_full_fill_trade_response` → GET /account/trades returning full fill

**WS Event Builders (use REAL NonKYC WS format):**
- `order_event_for_new_order_websocket_update(order)` → NonKYC `report` event with status "New"
  ```python
  return {
      "jsonrpc": "2.0",
      "method": "report",
      "params": {
          "id": order.exchange_order_id,
          "userProvidedId": order.client_order_id,
          "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
          "side": order.trade_type.name.lower(),
          "status": "New",
          "type": "limit",
          "quantity": str(order.amount),
          "price": str(order.price),
          "executedQuantity": "0",
          "reportType": "new",
          "createdAt": 1640000000000,
          "updatedAt": 1640000000000,
      }
  }
  ```
- `order_event_for_canceled_order_websocket_update(order)` → Same but status "Cancelled", reportType "canceled"
- `order_event_for_full_fill_websocket_update(order)` → Status "Filled", executedQuantity = full amount
- `trade_event_for_full_fill_websocket_update(order)` → Report with reportType "trade", tradeQuantity, tradePrice, tradeId, tradeFee

### Test File 2: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_order_book_data_source.py`

**Use `test/hummingbot/connector/exchange/binance/test_binance_api_order_book_data_source.py` (528 lines) as your template.** Read it first.

Must test:
1. `test_get_new_order_book_successful` — Mocks REST /market/orderbook, verifies snapshot message parsed correctly with sequence as update_id
2. `test_listen_for_subscriptions_subscribes_to_trades_and_order_diffs` — Verify WS sends subscribeOrderbook and subscribeTrades
3. `test_listen_for_trades_logs_trade_messages` — Send mock updateTrades WS message, verify trade message emitted
4. `test_listen_for_order_book_diffs` — Send mock updateOrderbook, verify diff message emitted with correct sequence
5. `test_listen_for_order_book_snapshots_from_ws` — Send mock snapshotOrderbook, verify snapshot message emitted
6. `test_snapshot_trades_handled` — Send mock snapshotTrades, verify processed
7. `test_sequence_gap_triggers_resync` — Send updateOrderbook with sequence gap, verify resync triggered

Set up mock data using the exact WS message formats from the Live API Intelligence section above.

### Test File 3: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_user_stream_data_source.py`

**Use `test/hummingbot/connector/exchange/binance/test_binance_user_stream_data_source.py` (352 lines) as your template.** Read it first.

Must test:
1. `test_connected_websocket_assistant_authenticates` — Verify login message sent on connect with correct format (method: "login", params: algo, pKey, nonce, signature)
2. `test_auth_response_validated` — Mock successful login response, verify no error
3. `test_auth_failure_raises` — Mock login error response, verify IOError raised
4. `test_subscribe_channels` — Verify subscribeReports and subscribeBalances sent
5. `test_user_stream_interruption_cleanup` — Verify _on_user_stream_interruption disconnects and resets

### Test File 4: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_web_utils.py`

Small test file:
1. `test_build_api_factory` — Verify factory creation
2. `test_get_current_server_time` — Mock /time endpoint, verify returns serverTime value
3. `test_public_rest_url` — Verify URL construction
4. `test_private_rest_url` — Verify URL construction

---

## LIVE API SANITY CHECK SCRIPTS

Install both of these scripts into the test directory. These are used for manual validation against the live API. They are NOT unit tests — they hit the real exchange.

### Script 1: `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py`

Create this file with the following content. It tests all public endpoints and optionally authenticated endpoints if NONKYC_API_KEY and NONKYC_API_SECRET are set in the repo root .env file.

```python
"""
NonKYC Exchange — Live API Smoke Test
======================================
Location: test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py

Setup:
  1. pip install requests websockets python-dotenv
  2. Add to your repo root .env file:
       NONKYC_API_KEY=your_key
       NONKYC_API_SECRET=your_secret
  3. Run:
       python test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import string
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

def find_repo_root():
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".env").exists() or (current / ".git").exists():
            return current
        current = current.parent
    return Path(__file__).resolve().parent

REPO_ROOT = find_repo_root()

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    print(f"  Loaded .env from {REPO_ROOT / '.env'}\n")
except ImportError:
    print("  WARNING: python-dotenv not installed. Using environment variables only.\n")

try:
    import requests
except ImportError:
    print("ERROR: pip install requests"); sys.exit(1)

try:
    import websockets
except ImportError:
    websockets = None
    print("WARNING: pip install websockets  (skipping WS tests)\n")

BASE_URL = "https://api.nonkyc.io/api/v2"
WS_URL = "wss://api.nonkyc.io"
PASS = 0; FAIL = 0; WARN = 0

def result(test_name, passed, detail="", warn=False):
    global PASS, FAIL, WARN
    if warn: WARN += 1; icon = "⚠"
    elif passed: PASS += 1; icon = "✅"
    else: FAIL += 1; icon = "❌"
    print(f"  {icon} {test_name}")
    if detail:
        for line in detail.split("\n"): print(f"       {line}")
    print()

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}\n")

def test_server_time():
    try:
        r = requests.get(f"{BASE_URL}/time", timeout=10); data = r.json()
        has_server_time = "serverTime" in data
        result("GET /time — 'serverTime' field exists", has_server_time,
               f"Status: {r.status_code}\nResponse: {json.dumps(data, indent=2)[:500]}")
        if has_server_time:
            drift = abs(int(time.time() * 1000) - int(data["serverTime"]))
            result("Server time drift < 30s", drift < 30000, f"Drift: {drift}ms")
    except Exception as e: result("GET /time", False, str(e))

def test_market_getlist():
    try:
        r = requests.get(f"{BASE_URL}/market/getlist", timeout=15); data = r.json()
        if not isinstance(data, list) or len(data) == 0:
            result("GET /market/getlist", False, f"Type: {type(data).__name__}"); return
        result("GET /market/getlist — returns list", True, f"Count: {len(data)} markets")
        sample = data[0]
        for field in ["symbol","primaryTicker","priceDecimals","quantityDecimals","isActive","minimumQuantity","minQuote"]:
            result(f"Field '{field}' exists", field in sample, f"Value: {sample.get(field, 'MISSING')}")
        print(f"  📋 Sample: {json.dumps(sample, indent=2)[:1500]}\n")
    except Exception as e: result("GET /market/getlist", False, str(e))

def test_orderbook(symbol="BTC/USDT"):
    try:
        r = requests.get(f"{BASE_URL}/market/orderbook", params={"symbol": symbol, "limit": 5}, timeout=10)
        data = r.json()
        result(f"GET /market/orderbook", r.status_code == 200, f"Keys: {list(data.keys())}")
        if isinstance(data, dict):
            for f in ["bids","asks","sequence"]: result(f"Has '{f}'", f in data, f"Value: {data.get(f,'MISSING')}")
            result("sequence is string (needs int())", isinstance(data.get("sequence"), str), f"Type: {type(data.get('sequence'))}")
    except Exception as e: result("GET /market/orderbook", False, str(e))

def test_tickers():
    try:
        r = requests.get(f"{BASE_URL}/tickers", timeout=10); data = r.json()
        result("GET /tickers", r.status_code == 200 and isinstance(data, list), f"Count: {len(data) if isinstance(data, list) else 'N/A'}")
    except Exception as e: result("GET /tickers", False, str(e))

def make_auth_headers(api_key, api_secret, url):
    nonce = str(int(time.time() * 1e3))
    message = f"{api_key}{url}{nonce}"
    sig = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"X-API-KEY": api_key, "X-API-NONCE": nonce, "X-API-SIGN": sig}

def test_auth_balance(api_key, api_secret):
    url = f"{BASE_URL}/balances"
    try:
        r = requests.get(url, headers=make_auth_headers(api_key, api_secret, url), timeout=10)
        result("GET /balances — authenticated", r.status_code == 200, f"Status: {r.status_code}")
        return r.status_code == 200
    except Exception as e: result("GET /balances", False, str(e)); return False

def test_auth_trades(api_key, api_secret):
    url = f"{BASE_URL}/account/trades"
    try:
        r = requests.get(url, headers=make_auth_headers(api_key, api_secret, url), timeout=10)
        result("GET /account/trades — authenticated", r.status_code == 200, f"Status: {r.status_code}\nPreview: {r.text[:400]}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                result("Trade object keys", True, f"Keys: {list(data[0].keys())}")
    except Exception as e: result("GET /account/trades", False, str(e))

async def test_ws_public():
    if not websockets: return
    section("Public WebSocket")
    try:
        async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
            result(f"WS connect to {WS_URL}", True)
            await ws.send(json.dumps({"method": "subscribeOrderbook", "params": {"symbol": "BTC/USDT"}, "id": 1}))
            await ws.send(json.dumps({"method": "subscribeTrades", "params": {"symbol": "BTC/USDT"}, "id": 2}))
            snapshot_seen = update_seen = trade_seen = False; count = 0
            try:
                while count < 15:
                    msg = await asyncio.wait_for(ws.recv(), timeout=15); data = json.loads(msg); count += 1
                    method = data.get("method", "")
                    if method == "snapshotOrderbook":
                        snapshot_seen = True; p = data.get("params", {})
                        result("snapshotOrderbook", True, f"sequence={p.get('sequence')}, bids={len(p.get('bids',[]))}")
                    elif method == "updateOrderbook" and not update_seen:
                        update_seen = True; result("updateOrderbook", True, f"sequence={data.get('params',{}).get('sequence')}")
                    elif method in ("snapshotTrades","updateTrades") and not trade_seen:
                        trade_seen = True; result(method, True, f"Keys: {list(data.get('params',{}).keys())}")
                    if snapshot_seen and update_seen and trade_seen: break
            except asyncio.TimeoutError: pass
            result("All WS events seen", snapshot_seen and update_seen, f"snapshot={snapshot_seen} update={update_seen} trade={trade_seen}")
    except Exception as e: result("WS connect", False, str(e))

async def test_ws_auth(api_key, api_secret):
    if not websockets: return
    section("Authenticated WebSocket")
    try:
        async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
            nonce = "".join(random.choices(string.ascii_letters + string.digits, k=14))
            sig = hmac.new(api_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            await ws.send(json.dumps({"method":"login","params":{"algo":"HS256","pKey":api_key,"nonce":nonce,"signature":sig},"id":99}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            result("WS login", resp.get("result") is True, f"Response: {json.dumps(resp)[:300]}")
            if resp.get("result") is True:
                await ws.send(json.dumps({"method":"subscribeReports","params":{},"id":100}))
                for _ in range(3):
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        result(f"WS {msg.get('method','ack')}", True, f"Preview: {json.dumps(msg)[:400]}")
                    except asyncio.TimeoutError: break
    except Exception as e: result("WS auth", False, str(e))

def main():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  NonKYC Exchange — Live API Smoke Test               ║")
    print("╚══════════════════════════════════════════════════════╝")
    section("Public REST"); test_server_time(); test_market_getlist(); test_orderbook(); test_tickers()
    if websockets: asyncio.run(test_ws_public())
    api_key = os.environ.get("NONKYC_API_KEY",""); api_secret = os.environ.get("NONKYC_API_SECRET","")
    if api_key and api_secret:
        section("Authenticated REST")
        if test_auth_balance(api_key, api_secret): test_auth_trades(api_key, api_secret)
        if websockets: asyncio.run(test_ws_auth(api_key, api_secret))
    else:
        section("Authenticated (SKIPPED)"); print("  Set NONKYC_API_KEY and NONKYC_API_SECRET in .env")
    print(f"\n╔══════════════════════════════════════════════════════╗")
    print(f"║  RESULTS: ✅ {PASS} passed  ❌ {FAIL} failed  ⚠ {WARN} warnings     ║")
    print(f"╚══════════════════════════════════════════════════════╝\n")

if __name__ == "__main__": main()
```

### Script 2: `test/hummingbot/connector/exchange/nonkyc/nonkyc_auth_test.py`

Create this file with the following content. It specifically tests REST authentication signature format with multiple fallback attempts.

```python
"""
NonKYC — REST Auth Verification
Tests the exact signature format: HMAC-SHA256(api_key + url + nonce)
Headers: X-API-KEY, X-API-NONCE, X-API-SIGN
"""
import hashlib, hmac, json, os, sys, time
from pathlib import Path
from urllib.parse import urlencode

def find_repo_root():
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".env").exists() or (current / ".git").exists(): return current
        current = current.parent
    return Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv; load_dotenv(find_repo_root() / ".env")
except ImportError: pass

import requests
BASE_URL = "https://api.nonkyc.io/api/v2"
api_key = os.environ.get("NONKYC_API_KEY",""); api_secret = os.environ.get("NONKYC_API_SECRET","")
if not api_key or not api_secret: print("Set NONKYC_API_KEY and NONKYC_API_SECRET in .env"); sys.exit(1)

def sign(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def test_get(endpoint, params=None, label=""):
    url = f"{BASE_URL}{endpoint}"
    nonce = str(int(time.time() * 1e3))
    full_url = f"{url}?{urlencode(params)}" if params else url
    sig = sign(api_secret, f"{api_key}{full_url}{nonce}")
    headers = {"X-API-KEY": api_key, "X-API-NONCE": nonce, "X-API-SIGN": sig}
    r = requests.get(full_url, headers=headers, timeout=10)
    icon = "✅" if r.status_code == 200 else "❌"
    print(f"  {icon} GET {endpoint} {label} — Status: {r.status_code}")
    if r.status_code == 200: print(f"       Preview: {r.text[:200]}")
    return r.status_code

print("╔══════════════════════════════════════════════════════╗")
print("║  NonKYC REST Auth — Signature Format Test            ║")
print("╚══════════════════════════════════════════════════════╝\n")
test_get("/balances", label="(no params)")
test_get("/account/orders", params={"status": "active"}, label="(with params)")
test_get("/account/trades", label="(trade history)")
```

---

## IMPORTANT NOTES

- Do NOT modify files outside `hummingbot/connector/exchange/nonkyc/` and `test/hummingbot/connector/exchange/nonkyc/`.
- Read the Binance test files FIRST before writing NonKYC tests — they show the exact patterns expected by the framework.
- All mock data in tests must use NonKYC's actual API format (documented above), NOT Binance format.
- The `sequence` field is a STRING in REST responses and INTEGER in WS. Handle both with `int()`.
- After all fixes, run: `python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v`
- Commit message: `fix(nonkyc): Phase 2 — reliability, reconnect recovery, fill migration + comprehensive test suite`
