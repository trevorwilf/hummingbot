"""
NonKYC — REST Auth Quick Test
Verifies the exact signature format used by the connector.
"""
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
    # Find repo root
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".env").exists():
            load_dotenv(current / ".env")
            print(f"Loaded .env from {current / '.env'}\n")
            break
        current = current.parent
except ImportError:
    print("pip install python-dotenv\n")

import requests

BASE_URL = "https://api.nonkyc.io/api/v2"

api_key = os.environ.get("NONKYC_API_KEY", "")
api_secret = os.environ.get("NONKYC_API_SECRET", "")

if not api_key or not api_secret:
    print("ERROR: Set NONKYC_API_KEY and NONKYC_API_SECRET in .env")
    sys.exit(1)


def sign(secret, message):
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def test_get(endpoint, params=None, label=""):
    url = f"{BASE_URL}{endpoint}"
    nonce = str(int(time.time() * 1e3))

    # GET signature: api_key + full_url_with_params + nonce
    if params:
        full_url = f"{url}?{urlencode(params)}"
    else:
        full_url = url

    message = f"{api_key}{full_url}{nonce}"
    sig = sign(api_secret, message)

    headers = {
        "X-API-KEY": api_key,
        "X-API-NONCE": nonce,
        "X-API-SIGN": sig,
    }

    print(f"{'='*60}")
    print(f"  TEST: GET {endpoint} {label}")
    print(f"{'='*60}")
    print(f"  URL:     {full_url}")
    print(f"  Nonce:   {nonce}")
    print(f"  Message: {api_key[:8]}...{full_url[-30:]}{nonce}")
    print(f"  Sign:    {sig[:20]}...")
    print()

    r = requests.get(full_url, headers=headers, timeout=10)
    print(f"  Status:  {r.status_code}")
    print(f"  Response: {r.text[:600]}")
    print()

    if r.status_code == 200:
        print(f"  ✅ AUTH SUCCESS\n")
    else:
        print(f"  ❌ AUTH FAILED\n")

    return r.status_code


def test_post(endpoint, body, label=""):
    url = f"{BASE_URL}{endpoint}"
    nonce = str(int(time.time() * 1e3))

    # POST signature: api_key + url + json_no_spaces + nonce
    json_str = json.dumps(body, separators=(',', ':'))
    message = f"{api_key}{url}{json_str}{nonce}"
    sig = sign(api_secret, message)

    headers = {
        "X-API-KEY": api_key,
        "X-API-NONCE": nonce,
        "X-API-SIGN": sig,
        "Content-Type": "application/json",
    }

    print(f"{'='*60}")
    print(f"  TEST: POST {endpoint} {label}")
    print(f"{'='*60}")
    print(f"  URL:     {url}")
    print(f"  Body:    {json_str[:100]}")
    print(f"  Nonce:   {nonce}")
    print()

    r = requests.post(url, headers=headers, json=body, timeout=10)
    print(f"  Status:  {r.status_code}")
    print(f"  Response: {r.text[:600]}")
    print()

    if r.status_code == 200:
        print(f"  ✅ AUTH SUCCESS\n")
    else:
        print(f"  ❌ AUTH FAILED\n")

    return r.status_code


# --- Test battery ---
print("╔══════════════════════════════════════════════════════╗")
print("║  NonKYC REST Auth — Signature Format Test            ║")
print("╚══════════════════════════════════════════════════════╝\n")

# Test 1: GET /balances (no query params)
s1 = test_get("/balances", label="(no params)")

# Test 2: GET /account/orders with params
s2 = test_get("/account/orders", params={"status": "active"}, label="(with params)")

# Test 3: GET /account/trades
s3 = test_get("/account/trades", label="(trade history)")

# If all GET tests fail, try alternative nonce formats
if s1 != 200 and s2 != 200:
    print("="*60)
    print("  All GETs failed. Trying alternative signature formats...")
    print("="*60)
    print()

    # Alt 1: nonce as 1e4 (the original buggy value — maybe it was right?)
    url = f"{BASE_URL}/balances"
    nonce_1e4 = str(int(time.time() * 1e4))
    msg = f"{api_key}{url}{nonce_1e4}"
    sig = sign(api_secret, msg)
    headers = {"X-API-KEY": api_key, "X-API-NONCE": nonce_1e4, "X-API-SIGN": sig}
    r = requests.get(url, headers=headers, timeout=10)
    print(f"  Alt A (nonce*1e4): Status={r.status_code}  Response={r.text[:200]}")

    # Alt 2: nonce as seconds (not ms)
    nonce_sec = str(int(time.time()))
    msg = f"{api_key}{url}{nonce_sec}"
    sig = sign(api_secret, msg)
    headers = {"X-API-KEY": api_key, "X-API-NONCE": nonce_sec, "X-API-SIGN": sig}
    r = requests.get(url, headers=headers, timeout=10)
    print(f"  Alt B (nonce*1e0): Status={r.status_code}  Response={r.text[:200]}")

    # Alt 3: lowercase header names
    nonce_ms = str(int(time.time() * 1e3))
    msg = f"{api_key}{url}{nonce_ms}"
    sig = sign(api_secret, msg)
    headers = {"apikey": api_key, "nonce": nonce_ms, "signature": sig}
    r = requests.get(url, headers=headers, timeout=10)
    print(f"  Alt C (lowercase headers): Status={r.status_code}  Response={r.text[:200]}")

    # Alt 4: sign just url+nonce (no api_key in message)
    nonce_ms = str(int(time.time() * 1e3))
    msg = f"{url}{nonce_ms}"
    sig = sign(api_secret, msg)
    headers = {"X-API-KEY": api_key, "X-API-NONCE": nonce_ms, "X-API-SIGN": sig}
    r = requests.get(url, headers=headers, timeout=10)
    print(f"  Alt D (no apikey in sig): Status={r.status_code}  Response={r.text[:200]}")

    # Alt 5: sign nonce only (like WS)
    nonce_ms = str(int(time.time() * 1e3))
    sig = sign(api_secret, nonce_ms)
    headers = {"X-API-KEY": api_key, "X-API-NONCE": nonce_ms, "X-API-SIGN": sig}
    r = requests.get(url, headers=headers, timeout=10)
    print(f"  Alt E (sign nonce only): Status={r.status_code}  Response={r.text[:200]}")

    print()