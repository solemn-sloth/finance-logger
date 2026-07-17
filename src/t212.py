"""
Trading 212 API — portfolio snapshot.

Auth: HTTP Basic Auth — base64(API_KEY:SECRET_KEY) sent as Authorization header.
Endpoint: ISA/Invest account summary via the T212 public beta API.
"""

import os
import time
from datetime import datetime, timezone
from requests.auth import HTTPBasicAuth

from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

T212_HOST = "https://live.trading212.com"
T212_API = f"{T212_HOST}/api/v0"


def _auth(key_var: str = "T212_API_KEY", secret_var: str = "T212_SECRET_KEY") -> HTTPBasicAuth:
    return HTTPBasicAuth(os.environ[key_var], os.environ[secret_var])


def _fetch_summary(auth: HTTPBasicAuth) -> dict:
    resp = requests.get(
        f"{T212_API}/equity/account/summary",
        auth=auth,
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Trading 212 portfolio fetch failed: {resp.status_code} {resp.text}")
    inv = resp.json()["investments"]
    value = round(float(inv["currentValue"]), 2)
    profit_abs = round(float(inv["unrealizedProfitLoss"]), 2)
    cost = round(float(inv["totalCost"]), 2)
    profit_pct = round((profit_abs / cost * 100) if cost else 0.0, 4)
    return {"value": value, "profit_abs": profit_abs, "profit_pct": profit_pct, "total_cost": cost}


def get_portfolio() -> dict:
    """
    Return total ISA portfolio value and profit.

    Returns:
        {
            "value": float,       # total portfolio value in £
            "profit_abs": float,  # absolute profit in £
            "profit_pct": float,  # profit as a percentage
        }
    """
    return _fetch_summary(_auth())


def get_invest_portfolio() -> dict:
    """Return total Invest (non-ISA) portfolio value and profit."""
    return _fetch_summary(_auth("T212_INVEST_API_KEY", "T212_INVEST_SECRET_KEY"))


# ---------------------------------------------------------------------------
# CGT ledger support: order history + instrument metadata (Invest account).
# History endpoints are heavily rate-limited (~1 req / 5-10 s).
# ---------------------------------------------------------------------------


def _get_json(url: str, auth: HTTPBasicAuth, max_retries: int = 6) -> dict:
    """GET with 429 backoff."""
    for attempt in range(max_retries):
        resp = requests.get(url, auth=auth, timeout=30)
        if resp.status_code == 429:
            wait = max(int(resp.headers.get("Retry-After", 0) or 0), 10 * (attempt + 1))
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RuntimeError(f"Trading 212 GET {url} failed: {resp.status_code} {resp.text}")
        return resp.json()
    raise RuntimeError(f"Trading 212 GET {url}: rate-limited after {max_retries} retries")


def _parse_t212_time(value: str) -> datetime:
    """Parse a T212 ISO timestamp to an aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def order_fill_time(item: dict) -> datetime | None:
    """Execution timestamp of a history item ({order:..., fill:...} shape)."""
    stamp = (item.get("fill") or {}).get("filledAt") or (item.get("order") or {}).get("createdAt")
    return _parse_t212_time(stamp) if stamp else None


def get_invest_positions() -> list[dict]:
    """Current open Invest-account positions: [{ticker, quantity, ...}, ...]."""
    auth = _auth("T212_INVEST_API_KEY", "T212_INVEST_SECRET_KEY")
    return _get_json(f"{T212_API}/equity/portfolio", auth)


def get_invest_order_history(since: datetime) -> list[dict]:
    """
    Invest-account order fills executed at/after `since` (aware UTC).
    Items are {"order": {...}, "fill": {...}} pairs, newest first; pagination
    stops once items predate `since`.
    """
    auth = _auth("T212_INVEST_API_KEY", "T212_INVEST_SECRET_KEY")
    url = f"{T212_API}/equity/history/orders?limit=50"
    items: list[dict] = []
    while url:
        body = _get_json(url, auth)
        page = body.get("items", [])
        reached_since = False
        for item in page:
            stamp = order_fill_time(item)
            if stamp and stamp < since:
                reached_since = True
                break
            items.append(item)
        next_path = body.get("nextPagePath")
        if reached_since or not page or not next_path:
            break
        url = T212_HOST + next_path if next_path.startswith("/") else next_path
        time.sleep(6)
    return items
