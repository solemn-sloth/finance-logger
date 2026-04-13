"""
Trading 212 API — portfolio snapshot.

Auth: HTTP Basic Auth — base64(API_KEY:SECRET_KEY) sent as Authorization header.
Endpoint: ISA/Invest account summary via the T212 public beta API.
"""

import os
from requests.auth import HTTPBasicAuth

from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

T212_API = "https://live.trading212.com/api/v0"


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
