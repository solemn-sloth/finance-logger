"""
Monzo API — token management, balance fetch, and payments.

Token lifecycle:
  - Initial refresh token is set in .env (MONZO_REFRESH_TOKEN) during bootstrap.
  - After the first run, tokens.json holds the live refresh token.
  - load_token() reads tokens.json first, falls back to .env.
  - Every access token exchange rotates the refresh token; save_token() persists it.
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

TOKENS_PATH = Path(__file__).parent.parent / "tokens.json"
MONZO_API = "https://api.monzo.com"


def load_token() -> str:
    """Return the current refresh token — tokens.json wins over .env."""
    if TOKENS_PATH.exists():
        data = json.loads(TOKENS_PATH.read_text())
        token = data.get("monzo_refresh_token", "").strip()
        if token:
            return token
    token = os.environ.get("MONZO_REFRESH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("No Monzo refresh token found in tokens.json or .env")
    return token


def save_token(refresh_token: str) -> None:
    """Write the new refresh token to tokens.json (chmod 600)."""
    TOKENS_PATH.write_text(json.dumps({"monzo_refresh_token": refresh_token}))
    TOKENS_PATH.chmod(0o600)


def refresh_access_token() -> str:
    """
    Exchange the current refresh token for a new access token.
    Rotates and saves the new refresh token as a side effect.
    Returns the access token.
    """
    refresh_token = load_token()
    resp = requests.post(
        f"{MONZO_API}/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["MONZO_CLIENT_ID"],
            "client_secret": os.environ["MONZO_CLIENT_SECRET"],
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Monzo token refresh failed: {resp.status_code} {resp.text}")
    data = resp.json()
    save_token(data["refresh_token"])
    return data["access_token"]


def _get_account_id(access_token: str) -> str:
    """Return the first UK retail account ID."""
    resp = requests.get(
        f"{MONZO_API}/accounts",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Monzo accounts fetch failed: {resp.status_code} {resp.text}")
    accounts = [a for a in resp.json()["accounts"] if a.get("type") == "uk_retail"]
    if not accounts:
        raise RuntimeError("No UK retail Monzo account found")
    return accounts[0]["id"]


def get_balance(access_token: str) -> float:
    """Return current account balance in £ (rounded to 2dp)."""
    account_id = _get_account_id(access_token)
    resp = requests.get(
        f"{MONZO_API}/balance",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"account_id": account_id},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Monzo balance fetch failed: {resp.status_code} {resp.text}")
    pence = resp.json()["balance"]
    return round(pence / 100, 2)


def get_transactions(access_token: str, since: str, before: str) -> list[dict]:
    """Return transactions for the given window.

    since/before: ISO 8601, e.g. "2025-07-01T00:00:00Z"
    amount is in pence (negative=debit, positive=credit).
    Requires 'transactions' scope on the OAuth client.
    """
    account_id = _get_account_id(access_token)
    resp = requests.get(
        f"{MONZO_API}/transactions",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "account_id": account_id,
            "since": since,
            "before": before,
            "expand[]": "merchant",
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Monzo transactions fetch failed: {resp.status_code} {resp.text}")
    return resp.json().get("transactions", [])


