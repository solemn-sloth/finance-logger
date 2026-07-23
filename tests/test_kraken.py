#!/usr/bin/env python3
"""
Smoke test: verify Kraken API connectivity and total GBP balance fetch.

Run: python tests/test_kraken.py
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decimal import Decimal

import kraken


def test_total_balance_includes_staked_offline():
    # Regression: staked ETH (ETH2.S) lives in the Earn wallet and is absent from
    # TradeBalance's 'eb', which made crypto show as ~£0. get_total_balance must
    # sum the Balance endpoint (staked variants included) at live prices.
    raw = {"ETH2.S": "4.3296595346",   # staked ETH
           "XETH": "0.5",               # spot ETH — merges with the staked units
           "ADA": "1.08420860",
           "ZGBP": "5",                 # fiat cash counts at face value
           "XXBT": "0.0000000001"}      # dust — dropped
    prices = {"ETH": Decimal("1400"), "ADA": Decimal("0.13")}
    orig_post, orig_val = kraken._private_post, kraken.current_gbp_value
    kraken._private_post = lambda ep, params=None: raw if ep == "Balance" else orig_post(ep, params)
    kraken.current_gbp_value = lambda a, amt: (Decimal(str(amt)) if a == "GBP"
                                               else Decimal(str(amt)) * prices[a])
    try:
        total = kraken.get_total_balance()
    finally:
        kraken._private_post, kraken.current_gbp_value = orig_post, orig_val
    # expected derived from the inputs above — no hand-typed constant to drift.
    # merges staked+spot ETH, counts fiat, and omits XXBT dust: if the code
    # regressed on any of those, total would diverge from expected.
    expected = round(float(
        (Decimal(raw["ETH2.S"]) + Decimal(raw["XETH"])) * prices["ETH"]
        + Decimal(raw["ADA"]) * prices["ADA"]
        + Decimal(raw["ZGBP"])
    ), 2)
    assert abs(total - expected) < 0.01, (total, expected)
    print(f"offline staked-balance test OK: £{total}")


if __name__ == "__main__":
    test_total_balance_includes_staked_offline()

    balance = kraken.get_total_balance()
    assert isinstance(balance, float), f"Expected float, got {type(balance)}"
    assert balance >= 0, f"Expected non-negative balance, got {balance}"
    print(f"Kraken total balance (GBP equivalent): £{balance}")
    print("OK")
