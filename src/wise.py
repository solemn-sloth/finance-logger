"""
Wise API client.

Handles GBP balance fetching.
Auth: static personal API token (no rotation needed).
Note: Wise statement/transaction endpoints are blocked for personal accounts
under PSD2 — only balance fetching is supported.
"""

import os

import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

_BASE = "https://api.wise.com"


def _headers() -> dict:
    token = os.environ["WISE_API_TOKEN"]
    return {"Authorization": f"Bearer {token}"}


def get_balance() -> float:
    """Return current GBP available balance from Wise."""
    profile_id = os.environ["WISE_PROFILE_ID"]
    resp = requests.get(
        f"{_BASE}/v4/profiles/{profile_id}/balances",
        params={"types": "STANDARD"},
        headers=_headers(),
    )
    if not resp.ok:
        raise RuntimeError(f"Wise get_balance failed: {resp.status_code} {resp.text}")
    balances = resp.json()
    for b in balances:
        if b.get("amount", {}).get("currency") == "GBP":
            return float(b["amount"]["value"])
    raise RuntimeError("No GBP balance found in Wise account")
