#!/usr/bin/env python3
"""
Unit tests for the CGT engine (pure, no APIs / no sheet).

Run: python3 tests/test_cgt_engine.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cgt_ledger import Row, compute_cgt

_IDX = [0]


def row(dt, rtype, out_a="", out_q=None, in_a="", in_q=None, value=None,
        fee=None, taxed=None):
    _IDX[0] += 1
    return Row(
        idx=_IDX[0], dt=datetime.fromisoformat(dt).replace(tzinfo=timezone.utc),
        type=rtype, asset_out=out_a,
        qty_out=Decimal(out_q) if out_q else None,
        asset_in=in_a, qty_in=Decimal(in_q) if in_q else None,
        value_gbp=Decimal(value) if value is not None else None,
        method="", fee_gbp=Decimal(fee) if fee else None, fee_orig="",
        wallet="test", txid=f"TEST:{_IDX[0]}",
        income_taxed=Decimal(taxed) if taxed else None, notes="",
    )


def test_s104_average_cost():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="ABC", in_q="100", value="1000"),
        row("2026-05-01T10:00", "BUY", in_a="ABC", in_q="50", value="600"),
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="75", value="900"),
    ]
    c = compute_cgt(rows)
    sell = c[rows[2].idx]
    assert sell.match == "S104", sell.match
    assert sell.gain == Decimal("100"), sell.gain  # 900 - 1600*75/150
    assert sell.pool_before == (Decimal("150"), Decimal("1600")), sell.pool_before
    assert sell.pool_after == (Decimal("75"), Decimal("800")), sell.pool_after


def test_same_day():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="ABC", in_q="10", value="50"),
        row("2026-05-01T09:00", "BUY", in_a="ABC", in_q="10", value="100"),
        row("2026-05-01T15:00", "SELL", out_a="ABC", out_q="10", value="120"),
    ]
    c = compute_cgt(rows)
    sell = c[rows[2].idx]
    assert sell.match == "SAME_DAY", sell.match
    assert sell.gain == Decimal("20"), sell.gain
    # pool untouched by the matched pair
    assert sell.pool_before == (Decimal("10"), Decimal("50")), sell.pool_before
    assert sell.pool_after == (Decimal("10"), Decimal("50")), sell.pool_after


def test_30_day_bed_and_breakfast():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="ABC", in_q="10", value="100"),
        row("2026-05-01T10:00", "SELL", out_a="ABC", out_q="10", value="150"),
        row("2026-05-11T10:00", "BUY", in_a="ABC", in_q="10", value="100"),
    ]
    c = compute_cgt(rows)
    sell = c[rows[1].idx]
    assert sell.match == "30_DAY", sell.match
    assert sell.gain == Decimal("50"), sell.gain
    assert sell.pool_after == (Decimal("10"), Decimal("100")), sell.pool_after


def test_split_match_30day_plus_pool():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="ABC", in_q="10", value="100"),
        row("2026-05-01T10:00", "SELL", out_a="ABC", out_q="15", value="300"),
        row("2026-05-06T10:00", "BUY", in_a="ABC", in_q="5", value="60"),
    ]
    c = compute_cgt(rows)
    sell = c[rows[1].idx]
    assert "30_DAY 5" in sell.match and "S104 10" in sell.match, sell.match
    assert sell.gain == Decimal("140"), sell.gain  # 300 - (60 + 100)
    assert sell.pool_after == (Decimal("0"), Decimal("0")), sell.pool_after


def test_swap_is_disposal_and_acquisition():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="BTC", in_q="1", value="20000"),
        row("2026-05-01T10:00", "SWAP", out_a="BTC", out_q="0.5",
            in_a="ETH", in_q="10", value="15000"),
        row("2026-06-01T10:00", "SELL", out_a="ETH", out_q="10", value="16000"),
    ]
    c = compute_cgt(rows)
    swap = c[rows[1].idx]
    assert swap.gain == Decimal("5000"), swap.gain      # 15000 - 20000*0.5
    assert swap.pool_after == (Decimal("0.5"), Decimal("10000")), swap.pool_after
    eth_sell = c[rows[2].idx]
    assert eth_sell.gain == Decimal("1000"), eth_sell.gain  # 16000 - 15000


def test_reward_basis_from_income_taxed():
    rows = [
        row("2026-05-01T10:00", "REWARD", in_a="SOL", in_q="1", value="100", taxed="100"),
        row("2026-06-01T10:00", "SELL", out_a="SOL", out_q="1", value="150"),
    ]
    c = compute_cgt(rows)
    assert c[rows[1].idx].gain == Decimal("50"), c[rows[1].idx].gain


def test_fees():
    rows = [
        row("2026-05-01T10:00", "BUY", in_a="ABC", in_q="10", value="100", fee="5"),
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="10", value="200", fee="2"),
    ]
    c = compute_cgt(rows)
    # buy fee adds to cost (105); sell fee reduces proceeds (198)
    assert c[rows[1].idx].gain == Decimal("93"), c[rows[1].idx].gain


def test_shortfall_flagged():
    rows = [row("2026-05-01T10:00", "SELL", out_a="XYZ", out_q="5", value="500")]
    c = compute_cgt(rows)
    assert "SHORTFALL" in c[rows[0].idx].match, c[rows[0].idx].match
    assert c[rows[0].idx].gain == Decimal("500")


def test_transfers_and_unknown_ignored():
    rows = [
        row("2026-05-01T10:00", "TRANSFER_IN", in_a="BTC", in_q="1"),
        row("2026-05-02T10:00", "FEE", out_a="BTC", out_q="0.001", value="20"),
    ]
    c = compute_cgt(rows)
    assert c[rows[0].idx].gain is None
    assert c[rows[1].idx].gain is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"OK — {len(fns)} tests")
