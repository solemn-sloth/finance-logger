#!/usr/bin/env python3
"""
Unit tests for Kraken ledger-entry -> ledger-row conversion (pure, no APIs —
GBP-only fixtures so no OHLC valuation is triggered).

Run: python3 tests/test_kraken_ledger.py
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cgt_ledger import _kraken_ledger_rows, _kraken_reward_to_row

T = 1757253121.0


def entry(etype, asset, amount, fee="0", refid="R1", subtype=""):
    return {"type": etype, "subtype": subtype, "refid": refid, "id": refid,
            "time": T, "asset": asset, "amount": amount, "fee": fee}


def test_gbp_buy_pair():
    rows = _kraken_ledger_rows([
        entry("spend", "ZGBP", "-100.0", "1.0", "RB"),
        entry("receive", "ADA", "159.899855", "0", "RB"),
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r.type == "BUY" and r.asset_in == "ADA", (r.type, r.asset_in)
    assert r.qty_in == Decimal("159.899855")
    assert r.value_gbp == Decimal("100.0")
    assert r.fee_gbp == Decimal("1.0")
    assert r.txid == "KRAKEN:RB"


def test_gbp_hold_treated_as_gbp():
    rows = _kraken_ledger_rows([
        entry("spend", "GBP.HOLD", "-99.01", "0.99", "RH"),
        entry("receive", "LMWR", "1714.424", "0", "RH"),
    ])
    assert rows[0].type == "BUY" and rows[0].value_gbp == Decimal("99.01")
    assert rows[0].fee_gbp == Decimal("0.99")


def test_gbp_sell_pair():
    rows = _kraken_ledger_rows([
        entry("spend", "ADA", "-100", "0.5", "RS"),
        entry("receive", "ZGBP", "50.0", "1.0", "RS"),
    ])
    r = rows[0]
    assert r.type == "SELL" and r.asset_out == "ADA"
    assert r.qty_out == Decimal("100.5")  # units out include the asset-side fee
    assert r.value_gbp == Decimal("50.0")  # gross proceeds
    assert r.fee_gbp == Decimal("1.0")


def test_crypto_deposit_and_withdrawal():
    rows = _kraken_ledger_rows([
        entry("deposit", "XETH", "1.0042936", "0", "RD"),
        entry("withdrawal", "XLTC", "-0.43554563", "0.002", "RW"),
        entry("deposit", "ZGBP", "100", "0", "RF"),  # fiat: skipped
    ])
    assert len(rows) == 2
    dep, wd = rows
    assert dep.type == "TRANSFER_IN" and dep.asset_in == "ETH"
    assert dep.qty_in == Decimal("1.0042936")
    assert wd.type == "TRANSFER_OUT" and wd.asset_out == "LTC"
    assert wd.qty_out == Decimal("0.43754563")  # amount + fee leave the wallet


def test_fiat_fiat_and_autoallocation_skipped():
    rows = _kraken_ledger_rows([
        entry("spend", "ZGBP", "-100", "0", "RX"),
        entry("receive", "ZUSD", "127.0", "0", "RX"),
        entry("transfer", "ADA.F", "159.9", "0", "RT", subtype="autoallocation"),
    ])
    assert rows == []


def test_only_asset_filter():
    entries = [
        entry("spend", "ZGBP", "-100", "0", "R1"),
        entry("receive", "ADA", "150", "0", "R1"),
        entry("spend", "ZGBP", "-50", "0", "R2"),
        entry("receive", "XETH", "0.01", "0", "R2"),
        entry("deposit", "XETH", "1.0", "0", "R3"),
    ]
    rows = _kraken_ledger_rows(entries, only_asset="ETH")
    assert {r.txid for r in rows} == {"KRAKEN:R2", "KRAKEN:R3"}


def test_reward_fee_netted():
    # GBP asset avoids the OHLC call; qty must be net of the staking fee
    r = _kraken_reward_to_row(entry("staking", "ZGBP", "0.05", "0.01", "RR"))
    assert r.qty_in == Decimal("0.04"), r.qty_in
    assert r.value_gbp == Decimal("0.04")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"OK — {len(fns)} tests")
