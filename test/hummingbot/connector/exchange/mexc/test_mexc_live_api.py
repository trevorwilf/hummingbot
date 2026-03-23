# -*- coding: utf-8 -*-
"""
MEXC Exchange -- Live API Validation Script
=============================================
Location: test/hummingbot/connector/exchange/mexc/test_mexc_live_api.py

STANDALONE script -- no hummingbot imports required for public tests.
Works both as:
  - Direct run:  python test_mexc_live_api.py
  - Pytest:      pytest test_mexc_live_api.py -v

Setup:
  1. pip install requests websockets python-dotenv
  2. (Optional) Add to repo root .env:
       MEXC_API_KEY=your_key
       MEXC_API_SECRET=your_secret
  3. Public tests (Tier 1) run without keys.
     Authenticated tests (Tier 2) require valid API credentials.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlencode

# ── Find repo root and load .env ────────────────────────────────────────────
_this = Path(__file__).resolve()
for _p in (_this.parent, *_this.parents):
    if (_p / "setup.py").exists() or (_p / "pyproject.toml").exists():
        _REPO_ROOT = _p
        break
else:
    _REPO_ROOT = _this.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

# ── Optional imports ─────────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    requests = None

try:
    import websockets
except ImportError:
    websockets = None

try:
    import pytest
except ImportError:
    pytest = None

# ── Constants ────────────────────────────────────────────────────────────────
BASE_URL = "https://api.mexc.com/api/v3"
WS_URL = "wss://wbs-api.mexc.com/ws"

# ── Auth helpers ─────────────────────────────────────────────────────────────


def _mexc_sign(params: dict, secret_key: str) -> dict:
    """Add timestamp + HMAC-SHA256 signature to params dict."""
    request_params = OrderedDict(params)
    request_params["timestamp"] = int(time.time() * 1000)
    encoded = urlencode(request_params)
    signature = hmac.new(
        secret_key.encode("utf-8"),
        encoded.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    request_params["signature"] = signature
    return dict(request_params)


def _mexc_auth_headers(api_key: str) -> dict:
    return {"X-MEXC-APIKEY": api_key}


def _require_mexc_keys():
    """Return (api_key, api_secret) or pytest.skip() if not set."""
    key = os.environ.get("MEXC_API_KEY", "")
    secret = os.environ.get("MEXC_API_SECRET", "")
    if not key or not secret:
        if pytest is not None:
            pytest.skip("MEXC_API_KEY / MEXC_API_SECRET not set")
        raise RuntimeError("MEXC API keys not available")
    return key, secret


# ── Result tracking (for standalone mode) ────────────────────────────────────
_passed = 0
_failed = 0
_skipped = 0


def result(name, ok, detail=""):
    global _passed, _failed
    tag = "PASS" if ok else "FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    msg = f"  [{tag}] {name}"
    if detail:
        msg += f"  -- {detail}"
    print(msg)


# =============================================================================
# TIER 1: PUBLIC REST (no auth required)
# =============================================================================

# --- Connectivity & Server Info ---

def test_ping():
    """GET /ping returns 200 with empty or minimal JSON."""
    r = requests.get(f"{BASE_URL}/ping", timeout=10)
    assert r.status_code == 200


def test_server_time():
    """GET /time returns serverTime as integer milliseconds."""
    r = requests.get(f"{BASE_URL}/time", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "serverTime" in data
    assert isinstance(data["serverTime"], int)
    # Should be a reasonable timestamp (after 2020, before 2030)
    assert 1577836800000 < data["serverTime"] < 1893456000000


def test_server_time_drift():
    """Server time should be within 30 seconds of local time."""
    r = requests.get(f"{BASE_URL}/time", timeout=10)
    server_ms = r.json()["serverTime"]
    local_ms = int(time.time() * 1000)
    drift_seconds = abs(server_ms - local_ms) / 1000
    assert drift_seconds < 30, f"Server time drift: {drift_seconds:.1f}s"


# --- Exchange Info / Trading Rules ---

def test_exchange_info():
    """GET /exchangeInfo returns symbols array."""
    r = requests.get(f"{BASE_URL}/exchangeInfo", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "symbols" in data
    assert len(data["symbols"]) > 100  # MEXC has hundreds of pairs


def test_exchange_info_single_symbol():
    """GET /exchangeInfo?symbol=BTCUSDT returns filtered info."""
    r = requests.get(f"{BASE_URL}/exchangeInfo", params={"symbol": "BTCUSDT"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "symbols" in data
    symbols = [s["symbol"] for s in data["symbols"]]
    assert "BTCUSDT" in symbols


def test_exchange_info_symbol_structure():
    """Each symbol in exchangeInfo has required fields for trading rules."""
    r = requests.get(f"{BASE_URL}/exchangeInfo", params={"symbol": "BTCUSDT"}, timeout=10)
    data = r.json()
    sym = data["symbols"][0]
    for field in ["symbol", "status", "baseAsset", "quoteAsset",
                  "baseAssetPrecision", "quoteAssetPrecision"]:
        assert field in sym, f"Missing field: {field}"


def test_default_symbols():
    """GET /defaultSymbols returns symbol list."""
    r = requests.get(f"{BASE_URL}/defaultSymbols", timeout=10)
    assert r.status_code == 200


# --- Ticker Data ---

def test_ticker_24hr():
    """GET /ticker/24hr?symbol=BTCUSDT returns 24hr price stats."""
    r = requests.get(f"{BASE_URL}/ticker/24hr", params={"symbol": "BTCUSDT"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    if isinstance(data, list):
        data = data[0]
    assert "lastPrice" in data or "lastPrice" in str(data)


def test_ticker_24hr_has_volume():
    """24hr ticker should include volume fields."""
    r = requests.get(f"{BASE_URL}/ticker/24hr", params={"symbol": "BTCUSDT"}, timeout=10)
    data = r.json()
    if isinstance(data, list):
        data = data[0]
    assert "volume" in data or "quoteVolume" in data


def test_ticker_book():
    """GET /ticker/bookTicker?symbol=BTCUSDT returns best bid/ask."""
    r = requests.get(f"{BASE_URL}/ticker/bookTicker", params={"symbol": "BTCUSDT"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    if isinstance(data, list):
        data = data[0]
    assert "bidPrice" in data
    assert "askPrice" in data


def test_ticker_book_bid_below_ask():
    """Best bid should be less than best ask (sanity check)."""
    r = requests.get(f"{BASE_URL}/ticker/bookTicker", params={"symbol": "BTCUSDT"}, timeout=10)
    data = r.json()
    if isinstance(data, list):
        data = data[0]
    bid = float(data["bidPrice"])
    ask = float(data["askPrice"])
    if bid > 0 and ask > 0:
        assert bid <= ask, f"Bid {bid} > Ask {ask}"


# --- Order Book ---

def test_depth():
    """GET /depth?symbol=BTCUSDT&limit=5 returns bids and asks."""
    r = requests.get(f"{BASE_URL}/depth", params={"symbol": "BTCUSDT", "limit": 5}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "bids" in data
    assert "asks" in data
    assert len(data["bids"]) > 0
    assert len(data["asks"]) > 0


def test_depth_bid_ask_format():
    """Order book entries should be [price, quantity] arrays with string values."""
    r = requests.get(f"{BASE_URL}/depth", params={"symbol": "BTCUSDT", "limit": 5}, timeout=10)
    data = r.json()
    bid = data["bids"][0]
    assert len(bid) >= 2  # [price, qty]
    float(bid[0])
    float(bid[1])


def test_depth_ordering():
    """Bids should be descending, asks ascending."""
    r = requests.get(f"{BASE_URL}/depth", params={"symbol": "BTCUSDT", "limit": 10}, timeout=10)
    data = r.json()
    bids = [float(b[0]) for b in data["bids"]]
    asks = [float(a[0]) for a in data["asks"]]
    assert bids == sorted(bids, reverse=True), "Bids not in descending order"
    assert asks == sorted(asks), "Asks not in ascending order"


# --- Candles / Klines ---

def test_klines():
    """GET /klines returns candle data."""
    r = requests.get(f"{BASE_URL}/klines", params={
        "symbol": "BTCUSDT", "interval": "5m", "limit": 5
    }, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) > 0


def test_klines_candle_structure():
    """Each candle should have at least [openTime, open, high, low, close, volume]."""
    r = requests.get(f"{BASE_URL}/klines", params={
        "symbol": "BTCUSDT", "interval": "60m", "limit": 3
    }, timeout=10)
    data = r.json()
    candle = data[0]
    assert len(candle) >= 6, f"Candle has only {len(candle)} fields"
    assert isinstance(candle[0], (int, float))
    for i in range(1, 6):
        float(candle[i])  # should not raise


def test_klines_chronological():
    """Candles should be in chronological order."""
    r = requests.get(f"{BASE_URL}/klines", params={
        "symbol": "BTCUSDT", "interval": "60m", "limit": 5
    }, timeout=10)
    data = r.json()
    times = [c[0] for c in data]
    assert times == sorted(times), "Candles not in chronological order"


# --- Recent Trades ---

def test_recent_trades():
    """GET /trades returns recent trade data."""
    r = requests.get(f"{BASE_URL}/trades", params={
        "symbol": "BTCUSDT", "limit": 10
    }, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) > 0


def test_recent_trades_fields():
    """Each trade should have price, qty, time fields."""
    r = requests.get(f"{BASE_URL}/trades", params={
        "symbol": "BTCUSDT", "limit": 5
    }, timeout=10)
    data = r.json()
    trade = data[0]
    assert "price" in trade or "p" in trade
    assert "qty" in trade or "q" in trade


# --- Invalid Input Handling ---

def test_invalid_symbol_returns_error():
    """Invalid symbol should return an error, not crash."""
    r = requests.get(f"{BASE_URL}/ticker/24hr", params={"symbol": "FAKEFAKE"}, timeout=10)
    assert r.status_code < 500


def test_depth_with_invalid_limit():
    """Invalid limit should be handled gracefully."""
    r = requests.get(f"{BASE_URL}/depth", params={"symbol": "BTCUSDT", "limit": 999999}, timeout=10)
    assert r.status_code < 500


# =============================================================================
# TIER 1b: PUBLIC WEBSOCKET
# =============================================================================

def test_ws_connect():
    """Can connect to MEXC public WebSocket."""
    if websockets is None:
        if pytest is not None:
            pytest.skip("websockets not installed")
        return

    async def _test():
        async with websockets.connect(WS_URL, close_timeout=5) as ws:
            await ws.send(json.dumps({
                "method": "SUBSCRIPTION",
                "params": ["spot@public.bookTicker.v3.api@BTCUSDT"]
            }))
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            assert len(msg) > 0

    asyncio.get_event_loop().run_until_complete(_test())


def test_ws_trade_stream():
    """Can subscribe to public trade stream and receive data."""
    if websockets is None:
        if pytest is not None:
            pytest.skip("websockets not installed")
        return

    async def _test():
        async with websockets.connect(WS_URL, close_timeout=5) as ws:
            await ws.send(json.dumps({
                "method": "SUBSCRIPTION",
                "params": ["spot@public.deals.v3.api@BTCUSDT"]
            }))
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            assert len(msg) > 0

    asyncio.get_event_loop().run_until_complete(_test())


def test_ws_invalid_subscription():
    """Subscribing to garbage channel should not crash the connection."""
    if websockets is None:
        if pytest is not None:
            pytest.skip("websockets not installed")
        return

    async def _test():
        async with websockets.connect(WS_URL, close_timeout=5) as ws:
            await ws.send(json.dumps({
                "method": "SUBSCRIPTION",
                "params": ["totally@invalid@channel"]
            }))
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass  # No response is acceptable for invalid sub

    asyncio.get_event_loop().run_until_complete(_test())


# =============================================================================
# TIER 1c: CONNECTOR INTEGRATION (uses hummingbot imports, still no auth)
# =============================================================================

def test_connector_constants_integrity():
    """Verify MEXC constants have required fields."""
    from hummingbot.connector.exchange.mexc import mexc_constants as C
    assert C.REST_URL
    assert C.WSS_URL
    assert C.PUBLIC_API_VERSION == "v3"
    assert C.PING_PATH_URL
    assert C.SERVER_TIME_PATH_URL
    assert C.EXCHANGE_INFO_PATH_URL
    assert C.SNAPSHOT_PATH_URL


def test_connector_public_rest_url_format():
    """public_rest_url builds correct MEXC URL."""
    from hummingbot.connector.exchange.mexc import mexc_web_utils as wu
    url = wu.public_rest_url("/ping")
    assert "api.mexc.com" in url
    assert "/api/v3/ping" in url


def test_candle_constants_integrity():
    """Verify MEXC candle feed constants."""
    from hummingbot.data_feed.candles_feed.mexc_spot_candles import constants as C
    assert "mexc.com" in C.REST_URL
    assert "mexc.com" in C.WSS_URL
    assert "5m" in C.INTERVALS
    assert "1h" in C.INTERVALS


def test_post_processor_is_importable():
    """MexcPostProcessor can be imported and instantiated."""
    from hummingbot.connector.exchange.mexc.mexc_post_processor import MexcPostProcessor
    proc = MexcPostProcessor()
    assert hasattr(proc, "post_process")


# =============================================================================
# TIER 2: AUTHENTICATED REST (requires MEXC_API_KEY + MEXC_API_SECRET)
# =============================================================================

def test_auth_account_info():
    """GET /account returns balance data."""
    api_key, api_secret = _require_mexc_keys()
    params = _mexc_sign({}, api_secret)
    headers = _mexc_auth_headers(api_key)
    r = requests.get(f"{BASE_URL}/account", params=params, headers=headers, timeout=10)
    assert r.status_code == 200, f"Account query failed: {r.text}"
    data = r.json()
    assert "balances" in data


def test_auth_listen_key_create():
    """POST /userDataStream creates a listen key."""
    api_key, api_secret = _require_mexc_keys()
    params = _mexc_sign({}, api_secret)
    headers = _mexc_auth_headers(api_key)
    r = requests.post(f"{BASE_URL}/userDataStream", params=params, headers=headers, timeout=10)
    assert r.status_code == 200, f"Listen key creation failed: {r.text}"
    data = r.json()
    assert "listenKey" in data
    assert len(data["listenKey"]) > 10


def test_auth_listen_key_keepalive():
    """PUT /userDataStream keeps listen key alive."""
    api_key, api_secret = _require_mexc_keys()
    params = _mexc_sign({}, api_secret)
    headers = _mexc_auth_headers(api_key)
    r = requests.post(f"{BASE_URL}/userDataStream", params=params, headers=headers, timeout=10)
    listen_key = r.json().get("listenKey", "")
    if not listen_key:
        if pytest is not None:
            pytest.skip("Could not create listen key")
        return
    params = _mexc_sign({"listenKey": listen_key}, api_secret)
    r = requests.put(f"{BASE_URL}/userDataStream", params=params, headers=headers, timeout=10)
    assert r.status_code == 200, f"Listen key keepalive failed: {r.text}"


def test_auth_open_orders():
    """GET /openOrders returns list (may be empty)."""
    api_key, api_secret = _require_mexc_keys()
    params = _mexc_sign({"symbol": "BTCUSDT"}, api_secret)
    headers = _mexc_auth_headers(api_key)
    r = requests.get(f"{BASE_URL}/openOrders", params=params, headers=headers, timeout=10)
    assert r.status_code == 200, f"Open orders query failed: {r.text}"
    assert isinstance(r.json(), list)


def test_auth_my_trades():
    """GET /myTrades returns trade history (may be empty)."""
    api_key, api_secret = _require_mexc_keys()
    params = _mexc_sign({"symbol": "BTCUSDT"}, api_secret)
    headers = _mexc_auth_headers(api_key)
    r = requests.get(f"{BASE_URL}/myTrades", params=params, headers=headers, timeout=10)
    assert r.status_code == 200, f"Trade history query failed: {r.text}"
    assert isinstance(r.json(), list)


def test_auth_bad_key_rejected():
    """Using a fake API key should return 401 or signature error, not 500."""
    params = _mexc_sign({}, "fake_secret_key_12345")
    headers = _mexc_auth_headers("fake_api_key_12345")
    r = requests.get(f"{BASE_URL}/account", params=params, headers=headers, timeout=10)
    assert r.status_code < 500  # Should be 400/401/403, not 500
    assert r.status_code != 200  # Should NOT succeed


# =============================================================================
# STANDALONE RUNNER
# =============================================================================

def _run_standalone():
    """Run all tests in standalone mode (no pytest)."""
    print("=" * 60)
    print("MEXC Live API Tests -- Standalone Mode")
    print("=" * 60)
    print()

    if requests is None:
        print("ERROR: 'requests' package not installed. Run: pip install requests")
        sys.exit(1)

    # Tier 1: Public REST
    print("--- TIER 1: PUBLIC REST ---")
    for name, func in [
        ("ping", test_ping),
        ("server_time", test_server_time),
        ("server_time_drift", test_server_time_drift),
        ("exchange_info", test_exchange_info),
        ("exchange_info_single_symbol", test_exchange_info_single_symbol),
        ("exchange_info_symbol_structure", test_exchange_info_symbol_structure),
        ("default_symbols", test_default_symbols),
        ("ticker_24hr", test_ticker_24hr),
        ("ticker_24hr_has_volume", test_ticker_24hr_has_volume),
        ("ticker_book", test_ticker_book),
        ("ticker_book_bid_below_ask", test_ticker_book_bid_below_ask),
        ("depth", test_depth),
        ("depth_bid_ask_format", test_depth_bid_ask_format),
        ("depth_ordering", test_depth_ordering),
        ("klines", test_klines),
        ("klines_candle_structure", test_klines_candle_structure),
        ("klines_chronological", test_klines_chronological),
        ("recent_trades", test_recent_trades),
        ("recent_trades_fields", test_recent_trades_fields),
        ("invalid_symbol_returns_error", test_invalid_symbol_returns_error),
        ("depth_with_invalid_limit", test_depth_with_invalid_limit),
    ]:
        try:
            func()
            result(name, True)
        except Exception as e:
            result(name, False, str(e))

    # Tier 1b: Public WS
    print()
    print("--- TIER 1b: PUBLIC WEBSOCKET ---")
    for name, func in [
        ("ws_connect", test_ws_connect),
        ("ws_trade_stream", test_ws_trade_stream),
        ("ws_invalid_subscription", test_ws_invalid_subscription),
    ]:
        try:
            func()
            result(name, True)
        except Exception as e:
            result(name, False, str(e))

    # Tier 2: Authenticated
    print()
    print("--- TIER 2: AUTHENTICATED REST ---")
    has_keys = bool(os.environ.get("MEXC_API_KEY")) and bool(os.environ.get("MEXC_API_SECRET"))
    if not has_keys:
        print("  SKIPPED: MEXC_API_KEY / MEXC_API_SECRET not set")
    else:
        for name, func in [
            ("auth_account_info", test_auth_account_info),
            ("auth_listen_key_create", test_auth_listen_key_create),
            ("auth_listen_key_keepalive", test_auth_listen_key_keepalive),
            ("auth_open_orders", test_auth_open_orders),
            ("auth_my_trades", test_auth_my_trades),
        ]:
            try:
                func()
                result(name, True)
            except Exception as e:
                result(name, False, str(e))

    # Always run bad-key test
    try:
        test_auth_bad_key_rejected()
        result("auth_bad_key_rejected", True)
    except Exception as e:
        result("auth_bad_key_rejected", False, str(e))

    print()
    print(f"Results: {_passed} passed, {_failed} failed")
    return _failed == 0


if __name__ == "__main__":
    success = _run_standalone()
    sys.exit(0 if success else 1)
