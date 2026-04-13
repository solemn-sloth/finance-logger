"""
Kraken API client.

Fetches total account balance equivalent in GBP using the private
TradeBalance endpoint with HMAC-SHA512 request signing.

Auth: API Key + Private Key (base64-encoded secret).
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

_BASE = "https://api.kraken.com"


def _sign(path: str, data: dict, secret: str) -> str:
    """Return the HMAC-SHA512 signature for a private Kraken API request."""
    encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _private_post(endpoint: str, params: dict | None = None) -> dict:
    """Send a signed POST to a Kraken private endpoint and return the result dict."""
    api_key = os.environ["KRAKEN_API_KEY"]
    api_secret = os.environ["KRAKEN_PRIVATE_KEY"]
    path = f"/0/private/{endpoint}"
    data = {"nonce": str(int(time.time() * 1000))}
    if params:
        data.update(params)
    headers = {
        "API-Key": api_key,
        "API-Sign": _sign(path, data, api_secret),
    }
    resp = requests.post(_BASE + path, data=data, headers=headers, timeout=10)
    if not resp.ok:
        raise RuntimeError(f"Kraken {endpoint} failed: {resp.status_code} {resp.text}")
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken {endpoint} error: {body['error']}")
    return body["result"]


def get_total_balance() -> float:
    """Return total account value in GBP (equivalent balance via TradeBalance)."""
    result = _private_post("TradeBalance", {"asset": "ZGBP"})
    # 'eb' = equivalent balance: total value of all holdings in the base currency
    return round(float(result["eb"]), 2)
