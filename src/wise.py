"""
Wise API client.

Handles GBP balance fetching and domestic GBP transfers.
Auth: static personal API token (no rotation needed).
"""

import os
import uuid

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


def make_payment(amount_pence: int, reference: str) -> dict:
    """
    Send a GBP domestic transfer from Wise balance.

    Steps:
      1. Create quote
      2. Create recipient account
      3. Create transfer
      4. Fund transfer from balance
    """
    profile_id = os.environ["WISE_PROFILE_ID"]
    payee_name = os.environ["WISE_PAYEE_NAME"]
    payee_sort_code = os.environ["WISE_PAYEE_SORT_CODE"]
    payee_account_number = os.environ["WISE_PAYEE_ACCOUNT_NUMBER"]
    amount_pounds = amount_pence / 100

    # 1. Create quote
    quote_resp = requests.post(
        f"{_BASE}/v3/profiles/{profile_id}/quotes",
        headers=_headers(),
        json={
            "sourceCurrency": "GBP",
            "targetCurrency": "GBP",
            "sourceAmount": amount_pounds,
            "profile": int(profile_id),
        },
    )
    if not quote_resp.ok:
        raise RuntimeError(f"Wise create_quote failed: {quote_resp.status_code} {quote_resp.text}")
    quote_id = quote_resp.json()["id"]

    # 2. Create recipient account
    recipient_resp = requests.post(
        f"{_BASE}/v1/accounts",
        headers=_headers(),
        json={
            "profile": int(profile_id),
            "accountHolderName": payee_name,
            "currency": "GBP",
            "type": "sort_code",
            "details": {
                "sortCode": payee_sort_code,
                "accountNumber": payee_account_number,
            },
        },
    )
    if not recipient_resp.ok:
        raise RuntimeError(f"Wise create_recipient failed: {recipient_resp.status_code} {recipient_resp.text}")
    recipient_id = recipient_resp.json()["id"]

    # 3. Create transfer
    transfer_resp = requests.post(
        f"{_BASE}/v1/transfers",
        headers=_headers(),
        json={
            "targetAccount": recipient_id,
            "quoteUuid": quote_id,
            "customerTransactionId": str(uuid.uuid4()),
            "details": {"reference": reference},
        },
    )
    if not transfer_resp.ok:
        raise RuntimeError(f"Wise create_transfer failed: {transfer_resp.status_code} {transfer_resp.text}")
    transfer_id = transfer_resp.json()["id"]

    # 4. Fund transfer from balance
    fund_resp = requests.post(
        f"{_BASE}/v3/profiles/{profile_id}/transfers/{transfer_id}/payments",
        headers=_headers(),
        json={"type": "BALANCE"},
    )
    if not fund_resp.ok:
        raise RuntimeError(f"Wise fund_transfer failed: {fund_resp.status_code} {fund_resp.text}")
    return fund_resp.json()
