"""
NonKYC Exchange — Live API Smoke Test
======================================
Location: test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py

Setup:
  1. pip install requests websockets python-dotenv
  2. Add to your repo root .env file:
       NONKYC_API_KEY=your_key
       NONKYC_API_SECRET=your_secret
  3. Run from anywhere:
       python test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py

Tier 1 (public endpoints) runs without keys.
Tier 2 (auth) only runs if keys are present in .env.
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

# ---------------------------------------------------------------------------
# Find repo root and load .env
# ---------------------------------------------------------------------------
def find_repo_root():
    """Walk up from this file until we find .env or .git."""
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
    print("  WARNING: python-dotenv not installed. Using environment variables only.")
    print("  Install with: pip install python-dotenv\n")

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

try:
    import websockets
except ImportError:
    websockets = None
    print("WARNING: pip install websockets  (skipping WS tests)\n")


BASE_URL = "https://api.nonkyc.io/api/v2"
WS_URL = "wss://api.nonkyc.io"

# Track results
PASS = 0
FAIL = 0
WARN = 0


def result(test_name, passed, detail="", warn=False):
    global PASS, FAIL, WARN
    if warn:
        WARN += 1
        icon = "⚠"
    elif passed:
        PASS += 1
        icon = "✅"
    else:
        FAIL += 1
        icon = "❌"
    print(f"  {icon} {test_name}")
    if detail:
        for line in detail.split("\n"):
            print(f"       {line}")
    print()


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


# ========================================================================
# TIER 1: Public REST endpoints (no auth)
# ========================================================================

def test_server_time():
    """Verify GET /time returns serverTime in ms."""
    try:
        r = requests.get(f"{BASE_URL}/time", timeout=10)
        data = r.json()

        has_server_time = "serverTime" in data
        result(
            "GET /time — 'serverTime' field exists",
            has_server_time,
            f"Status: {r.status_code}\n"
            f"Response keys: {list(data.keys())}\n"
            f"Full response: {json.dumps(data, indent=2)[:500]}"
        )

        if has_server_time:
            st = data["serverTime"]
            now_ms = int(time.time() * 1000)
            drift = abs(now_ms - int(st))
            result(
                "Server time drift < 30s",
                drift < 30000,
                f"Server: {st}, Local: {now_ms}, Drift: {drift}ms"
            )
        return data
    except Exception as e:
        result("GET /time — reachable", False, str(e))
        return None


def test_also_try_old_time_endpoint():
    """Check if the old getservertime endpoint still works."""
    try:
        r = requests.get("https://nonkyc.io/api/v2/getservertime", timeout=10)
        data = r.json()
        result(
            "GET /getservertime (old endpoint) — still alive?",
            r.status_code == 200,
            f"Status: {r.status_code}\n"
            f"Response keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}\n"
            f"Full response: {json.dumps(data, indent=2)[:500]}",
            warn=True
        )
    except Exception as e:
        result("GET /getservertime — not reachable (expected)", True, str(e))


def test_market_getlist():
    """Verify GET /market/getlist response shape."""
    try:
        r = requests.get(f"{BASE_URL}/market/getlist", timeout=15)
        data = r.json()

        if not isinstance(data, list) or len(data) == 0:
            result(
                "GET /market/getlist — returns list",
                False,
                f"Type: {type(data).__name__}, Length: {len(data) if isinstance(data, list) else 'N/A'}"
            )
            return None

        result("GET /market/getlist — returns non-empty list", True, f"Count: {len(data)} markets")

        sample = data[0]
        keys = list(sample.keys())
        result(
            "First market object — top-level keys",
            True,
            f"Keys: {keys}"
        )

        # Check critical fields our connector uses
        critical_fields = {
            "symbol": "Trading pair string (e.g. BTC/USDT)",
            "primaryTicker": "Base asset ticker (e.g. BTC)",
            "secondaryTicker": "Quote asset ticker (e.g. USDT)",
            "priceDecimals": "Price precision",
            "quantityDecimals": "Quantity precision",
            "isActive": "Market active flag",
        }

        for field, desc in critical_fields.items():
            has_field = field in sample
            val = sample.get(field, "MISSING")
            result(
                f"Field '{field}' exists — {desc}",
                has_field,
                f"Value: {val}"
            )

        # Check for fields we might need
        extra_fields = ["active", "minQuantity", "minNotional", "tickSize", "quantityIncrement"]
        found_extras = {f: sample.get(f, "MISSING") for f in extra_fields}
        result(
            "Optional/useful fields check",
            True,
            "\n".join(f"{k}: {v}" for k, v in found_extras.items()),
            warn=True
        )

        # Print a full sample for manual inspection
        print(f"  📋 Full sample market object (first in list):")
        print(f"       {json.dumps(sample, indent=2)[:2000]}")
        print()

        return data
    except Exception as e:
        result("GET /market/getlist — reachable", False, str(e))
        return None


def test_orderbook(symbol="BTC/USDT"):
    """Verify GET /market/orderbook response shape."""
    try:
        r = requests.get(
            f"{BASE_URL}/market/orderbook",
            params={"symbol": symbol, "limit": 5},
            timeout=10
        )
        data = r.json()

        result(
            f"GET /market/orderbook?symbol={symbol}&limit=5",
            r.status_code == 200,
            f"Status: {r.status_code}\n"
            f"Response keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )

        if isinstance(data, dict):
            has_bids = "bids" in data
            has_asks = "asks" in data
            has_seq = "sequence" in data

            result("Has 'bids' field", has_bids)
            result("Has 'asks' field", has_asks)
            result(
                "Has 'sequence' field",
                has_seq,
                f"Value: {data.get('sequence', 'MISSING')}"
            )

            if has_bids and len(data["bids"]) > 0:
                bid = data["bids"][0]
                result(
                    "Bid entry structure",
                    True,
                    f"Keys: {list(bid.keys()) if isinstance(bid, dict) else 'Not a dict!'}\n"
                    f"Sample: {bid}"
                )
            if has_asks and len(data["asks"]) > 0:
                ask = data["asks"][0]
                result(
                    "Ask entry structure",
                    True,
                    f"Keys: {list(ask.keys()) if isinstance(ask, dict) else 'Not a dict!'}\n"
                    f"Sample: {ask}"
                )

            print(f"  📋 Full orderbook response:")
            print(f"       {json.dumps(data, indent=2)[:1500]}")
            print()

        return data
    except Exception as e:
        result("GET /market/orderbook — reachable", False, str(e))
        return None


def test_also_try_old_orderbook(symbol="BTC_USDT"):
    """Check the CMC-style /orderbook endpoint for comparison."""
    try:
        r = requests.get(
            f"{BASE_URL}/orderbook",
            params={"ticker_id": symbol},
            timeout=10
        )
        data = r.json()
        result(
            f"GET /orderbook?ticker_id={symbol} (CMC-style, old)",
            r.status_code == 200,
            f"Status: {r.status_code}\n"
            f"Response type: {type(data).__name__}\n"
            f"Preview: {json.dumps(data, indent=2)[:500]}",
            warn=True
        )
    except Exception as e:
        result("GET /orderbook (CMC-style) — not reachable", True, str(e))


def test_tickers():
    """Verify GET /tickers response shape."""
    try:
        r = requests.get(f"{BASE_URL}/tickers", timeout=10)
        data = r.json()

        is_list = isinstance(data, list)
        result(
            "GET /tickers — returns data",
            r.status_code == 200 and (len(data) > 0 if is_list else True),
            f"Status: {r.status_code}, Type: {type(data).__name__}, "
            f"Count: {len(data) if is_list else 'N/A'}"
        )

        if is_list and len(data) > 0:
            sample = data[0]
            result(
                "Ticker entry structure",
                True,
                f"Keys: {list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}\n"
                f"Sample: {json.dumps(sample, indent=2)[:500]}"
            )
        elif isinstance(data, dict):
            print(f"  📋 Ticker response (dict, first 3 entries):")
            for i, (k, v) in enumerate(data.items()):
                if i >= 3:
                    break
                print(f"       {k}: {json.dumps(v, indent=2)[:300]}")
            print()

        return data
    except Exception as e:
        result("GET /tickers — reachable", False, str(e))
        return None


def test_account_trades_unauthed():
    """Verify that /account/trades exists (should return 401 without auth)."""
    try:
        r = requests.get(f"{BASE_URL}/account/trades", timeout=10)
        result(
            "GET /account/trades — returns 401 without auth (confirms endpoint exists)",
            r.status_code in (401, 403),
            f"Status: {r.status_code}\nResponse: {r.text[:300]}"
        )
    except Exception as e:
        result("GET /account/trades — reachable", False, str(e))


# ========================================================================
# TIER 2: Authenticated REST (needs API key)
# ========================================================================

def make_auth_headers(api_key, api_secret, method, url, body=""):
    """Generate NonKYC auth headers based on our Phase 1 fix understanding."""
    nonce = str(int(time.time() * 1e3))

    # REST signature: HMAC-SHA256 of (apikey + url + body + nonce)
    message = api_key + url + body + nonce
    signature = hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return {
        "apikey": api_key,
        "signature": signature,
        "nonce": nonce,
    }


def test_auth_balance(api_key, api_secret):
    """Test authenticated GET /balances."""
    url = f"{BASE_URL}/balances"
    try:
        headers = make_auth_headers(api_key, api_secret, "GET", url)
        r = requests.get(url, headers=headers, timeout=10)

        result(
            "GET /balances — authenticated",
            r.status_code == 200,
            f"Status: {r.status_code}\n"
            f"Response preview: {r.text[:500]}"
        )
        return r.status_code == 200
    except Exception as e:
        result("GET /balances — reachable", False, str(e))
        return False


def test_auth_open_orders(api_key, api_secret):
    """Test authenticated GET /account/orders."""
    url = f"{BASE_URL}/account/orders"
    try:
        headers = make_auth_headers(api_key, api_secret, "GET", url)
        r = requests.get(
            url, headers=headers, params={"status": "active"}, timeout=10
        )

        result(
            "GET /account/orders?status=active — authenticated",
            r.status_code == 200,
            f"Status: {r.status_code}\n"
            f"Response preview: {r.text[:500]}"
        )
    except Exception as e:
        result("GET /account/orders — reachable", False, str(e))


def test_auth_trades(api_key, api_secret):
    """Test authenticated GET /account/trades."""
    url = f"{BASE_URL}/account/trades"
    try:
        headers = make_auth_headers(api_key, api_secret, "GET", url)
        r = requests.get(url, headers=headers, timeout=10)

        result(
            "GET /account/trades — authenticated",
            r.status_code == 200,
            f"Status: {r.status_code}\n"
            f"Response preview: {r.text[:500]}"
        )

        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                sample = data[0]
                result(
                    "Trade object structure",
                    True,
                    f"Keys: {list(sample.keys())}\n"
                    f"Sample: {json.dumps(sample, indent=2)[:500]}"
                )
            else:
                result(
                    "Trade response shape",
                    True,
                    f"Type: {type(data).__name__}, "
                    f"Count: {len(data) if isinstance(data, list) else 'N/A'}",
                    warn=True
                )
    except Exception as e:
        result("GET /account/trades — reachable", False, str(e))


# ========================================================================
# TIER 1B: Public WebSocket
# ========================================================================

async def test_ws_public():
    """Connect to WS, subscribe to orderbook and trades, capture messages."""
    if websockets is None:
        result("WebSocket tests", False, "websockets package not installed", warn=True)
        return

    section("TIER 1B: Public WebSocket")

    try:
        async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
            result(f"WS connect to {WS_URL}", True)

            # Subscribe to orderbook
            sub_ob = {
                "method": "subscribeOrderbook",
                "params": {"symbol": "BTC/USDT"},
                "id": 1
            }
            await ws.send(json.dumps(sub_ob))
            print(f"  📤 Sent: subscribeOrderbook BTC/USDT")

            # Subscribe to trades
            sub_trades = {
                "method": "subscribeTrades",
                "params": {"symbol": "BTC/USDT"},
                "id": 2
            }
            await ws.send(json.dumps(sub_trades))
            print(f"  📤 Sent: subscribeTrades BTC/USDT")

            # Collect messages
            snapshot_seen = False
            update_seen = False
            trade_seen = False
            messages_received = 0

            try:
                while messages_received < 20:
                    msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    data = json.loads(msg)
                    messages_received += 1
                    method = data.get("method", data.get("result", "unknown"))

                    if method == "snapshotOrderbook":
                        snapshot_seen = True
                        params = data.get("params", {})
                        bid_sample = params.get("bids", [None])[0] if params.get("bids") else "empty"
                        ask_sample = params.get("asks", [None])[0] if params.get("asks") else "empty"
                        result(
                            "WS snapshotOrderbook received",
                            True,
                            f"Keys in params: {list(params.keys())}\n"
                            f"Has sequence: {'sequence' in params}\n"
                            f"Sequence value: {params.get('sequence', 'MISSING')}\n"
                            f"Bids count: {len(params.get('bids', []))}\n"
                            f"Asks count: {len(params.get('asks', []))}\n"
                            f"Bid sample: {bid_sample}\n"
                            f"Ask sample: {ask_sample}"
                        )

                    elif method == "updateOrderbook":
                        if not update_seen:
                            update_seen = True
                            params = data.get("params", {})
                            result(
                                "WS updateOrderbook received",
                                True,
                                f"Keys in params: {list(params.keys())}\n"
                                f"Has sequence: {'sequence' in params}\n"
                                f"Sequence value: {params.get('sequence', 'MISSING')}"
                            )

                    elif method in ("updateTrades", "snapshotTrades"):
                        if not trade_seen:
                            trade_seen = True
                            params = data.get("params", {})
                            result(
                                f"WS {method} received",
                                True,
                                f"Keys in params: {list(params.keys()) if isinstance(params, dict) else type(params).__name__}\n"
                                f"Full message: {json.dumps(data, indent=2)[:800]}"
                            )
                            # Check timestamp field name
                            trade_data = params.get("data", [params] if isinstance(params, dict) else params)
                            if isinstance(trade_data, list) and len(trade_data) > 0:
                                td = trade_data[0]
                                has_ts = "timestamp" in td
                                has_tsms = "timestampms" in td
                                result(
                                    "Trade timestamp field check",
                                    has_ts or has_tsms,
                                    f"Has 'timestamp': {has_ts}\n"
                                    f"Has 'timestampms': {has_tsms}\n"
                                    f"Trade entry keys: {list(td.keys()) if isinstance(td, dict) else type(td).__name__}"
                                )

                    elif isinstance(data, dict) and "id" in data:
                        print(f"  📥 Sub ack (id={data['id']}): result={data.get('result', 'N/A')}")

                    if snapshot_seen and update_seen and trade_seen:
                        break

            except asyncio.TimeoutError:
                print(f"  ⏱ Timed out after collecting {messages_received} messages")

            result(
                "WS snapshotOrderbook seen",
                snapshot_seen,
                "" if snapshot_seen else "Never received — may need longer wait or different pair"
            )
            result(
                "WS updateOrderbook seen",
                update_seen,
                "" if update_seen else "No diffs during test window"
            )

    except Exception as e:
        result(f"WS connect to {WS_URL}", False, str(e))


# ========================================================================
# TIER 2B: Authenticated WebSocket
# ========================================================================

async def test_ws_auth(api_key, api_secret):
    """Test WS login with corrected nonce generation."""
    if websockets is None:
        return

    section("TIER 2B: Authenticated WebSocket")

    try:
        async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
            # Generate nonce the FIXED way (string, not list repr)
            nonce = "".join(random.choices(string.ascii_letters + string.digits, k=14))
            signature = hmac.new(
                api_secret.encode("utf-8"),
                nonce.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()

            login_msg = {
                "method": "login",
                "params": {
                    "algo": "HS256",
                    "pKey": api_key,
                    "nonce": nonce,
                    "signature": signature,
                },
                "id": 99
            }

            await ws.send(json.dumps(login_msg))
            print(f"  📤 Sent login (nonce: {nonce})")

            try:
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(response)

                is_success = data.get("result") is True or data.get("result", {}) == True
                result(
                    "WS login response",
                    is_success,
                    f"Full response: {json.dumps(data, indent=2)[:500]}"
                )

                if is_success:
                    # Try subscribeReports
                    sub_reports = {
                        "method": "subscribeReports",
                        "params": {},
                        "id": 100
                    }
                    await ws.send(json.dumps(sub_reports))
                    print(f"  📤 Sent subscribeReports")

                    for _ in range(5):
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                            data = json.loads(msg)
                            method = data.get("method", "ack")

                            if method == "activeOrders":
                                params = data.get("params", [])
                                result(
                                    "WS activeOrders snapshot received",
                                    True,
                                    f"Order count: {len(params) if isinstance(params, list) else 'N/A'}\n"
                                    f"Preview: {json.dumps(data, indent=2)[:600]}"
                                )
                            elif method == "report":
                                params = data.get("params", {})
                                result(
                                    "WS report received",
                                    True,
                                    f"Keys: {list(params.keys()) if isinstance(params, dict) else type(params).__name__}\n"
                                    f"Preview: {json.dumps(data, indent=2)[:600]}"
                                )
                            else:
                                print(f"  📥 {method}: {json.dumps(data, indent=2)[:300]}")

                        except asyncio.TimeoutError:
                            break

                    # Try subscribeBalances (undocumented — testing if it works)
                    sub_bal = {
                        "method": "subscribeBalances",
                        "params": {},
                        "id": 101
                    }
                    await ws.send(json.dumps(sub_bal))
                    print(f"  📤 Sent subscribeBalances (undocumented — testing)")

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)
                        result(
                            "WS subscribeBalances response",
                            True,
                            f"Response: {json.dumps(data, indent=2)[:500]}",
                            warn=True
                        )
                    except asyncio.TimeoutError:
                        result(
                            "WS subscribeBalances",
                            False,
                            "No response (likely unsupported)",
                            warn=True
                        )

                    # Try getTradingBalance (documented)
                    get_bal = {
                        "method": "getTradingBalance",
                        "params": {},
                        "id": 102
                    }
                    await ws.send(json.dumps(get_bal))
                    print(f"  📤 Sent getTradingBalance (documented)")

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)
                        result(
                            "WS getTradingBalance response",
                            True,
                            f"Response: {json.dumps(data, indent=2)[:500]}"
                        )
                    except asyncio.TimeoutError:
                        result("WS getTradingBalance", False, "No response")

            except asyncio.TimeoutError:
                result("WS login response", False, "Timed out waiting for login response")

    except Exception as e:
        result(f"WS auth connect", False, str(e))


# ========================================================================
# MAIN
# ========================================================================

def main():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║  NonKYC Exchange — Live API Smoke Test               ║")
    print("║  Testing actual API responses against connector code ║")
    print("╚══════════════════════════════════════════════════════╝")

    # --- TIER 1: Public REST ---
    section("TIER 1: Public REST Endpoints")

    test_server_time()
    test_also_try_old_time_endpoint()
    test_market_getlist()
    test_orderbook()
    test_also_try_old_orderbook()
    test_tickers()
    test_account_trades_unauthed()

    # --- TIER 1B: Public WS ---
    if websockets:
        asyncio.run(test_ws_public())

    # --- TIER 2: Authenticated ---
    api_key = os.environ.get("NONKYC_API_KEY", "")
    api_secret = os.environ.get("NONKYC_API_SECRET", "")

    if api_key and api_secret:
        section("TIER 2: Authenticated REST")
        auth_ok = test_auth_balance(api_key, api_secret)
        if auth_ok:
            test_auth_open_orders(api_key, api_secret)
            test_auth_trades(api_key, api_secret)
        else:
            print("  ⚠ Auth failed — signature format may differ from assumption.")
            print("  ⚠ Check the response body above for error details.")
            print("  ⚠ The connector's nonkyc_auth.py may need further adjustment.\n")

        # TIER 2B: Authenticated WS
        if websockets:
            asyncio.run(test_ws_auth(api_key, api_secret))
    else:
        section("TIER 2: Authenticated (SKIPPED)")
        print("  No API keys found. Add to your .env file at the repo root:")
        print(f"    {REPO_ROOT / '.env'}")
        print()
        print("  NONKYC_API_KEY=your_key")
        print("  NONKYC_API_SECRET=your_secret")
        print()

    # --- SUMMARY ---
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print(f"║  RESULTS: ✅ {PASS} passed  ❌ {FAIL} failed  ⚠ {WARN} warnings     ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    if FAIL > 0:
        print("  ❌ FAILURES found — review output above.")
        print("  Any failed field checks mean the connector code needs adjustment.")
    else:
        print("  ✅ All checks passed. API responses match connector expectations.")

    print()


if __name__ == "__main__":
    main()