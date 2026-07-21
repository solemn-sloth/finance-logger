#!/usr/bin/env python3
"""
Unit tests for the CGT engine (pure, no APIs / no sheet).

Run: python3 tests/test_cgt_engine.py
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import contextlib
import io

import cgt_ledger
from cgt_ledger import (
    SUMMARY_BLOCK_HEIGHT, SUMMARY_MARKER, Row, aea_for, build_summary_block,
    compute_cgt, fetch_coinbase_rows, summarise_years, tax_year_of,
    _warn_flagged_rows,
)

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
    c, pools = compute_cgt(rows)
    sell = c[rows[2].idx]
    assert sell.match == "S104", sell.match
    assert sell.gain == Decimal("100"), sell.gain  # 900 - 1600*75/150
    assert sell.pool_before == (Decimal("150"), Decimal("1600")), sell.pool_before
    assert sell.pool_after == (Decimal("75"), Decimal("800")), sell.pool_after
    assert pools["ABC"] == (Decimal("75"), Decimal("800")), pools["ABC"]


def test_same_day():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="ABC", in_q="10", value="50"),
        row("2026-05-01T09:00", "BUY", in_a="ABC", in_q="10", value="100"),
        row("2026-05-01T15:00", "SELL", out_a="ABC", out_q="10", value="120"),
    ]
    c, pools = compute_cgt(rows)
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
    c, pools = compute_cgt(rows)
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
    c, pools = compute_cgt(rows)
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
    c, pools = compute_cgt(rows)
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
    c, pools = compute_cgt(rows)
    assert c[rows[1].idx].gain == Decimal("50"), c[rows[1].idx].gain


def test_fees():
    rows = [
        row("2026-05-01T10:00", "BUY", in_a="ABC", in_q="10", value="100", fee="5"),
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="10", value="200", fee="2"),
    ]
    c, pools = compute_cgt(rows)
    # buy fee adds to cost (105); sell fee reduces proceeds (198)
    assert c[rows[1].idx].gain == Decimal("93"), c[rows[1].idx].gain


def test_shortfall_flagged():
    rows = [row("2026-05-01T10:00", "SELL", out_a="XYZ", out_q="5", value="500")]
    c, pools = compute_cgt(rows)
    assert c[rows[0].idx].match.startswith("⚠ "), c[rows[0].idx].match
    assert "SHORTFALL" in c[rows[0].idx].match, c[rows[0].idx].match
    assert c[rows[0].idx].gain == Decimal("500")


def test_transfers_and_unknown_ignored():
    rows = [
        row("2026-05-01T10:00", "TRANSFER_IN", in_a="BTC", in_q="1"),
        row("2026-05-02T10:00", "FEE", out_a="BTC", out_q="0.001", value="20"),
    ]
    c, pools = compute_cgt(rows)
    assert c[rows[0].idx].gain is None
    assert c[rows[1].idx].gain is None


def test_missing_value_flagged_and_skipped():
    rows = [
        row("2026-05-01T10:00", "BUY", in_a="ABC", in_q="10"),  # no value
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="5", value="50"),
    ]
    c, pools = compute_cgt(rows)
    assert c[rows[0].idx].match == "⚠ MISSING VALUE", c[rows[0].idx].match
    # the BUY never entered the pool, so the SELL has nothing to match against
    assert "SHORTFALL" in c[rows[1].idx].match, c[rows[1].idx].match


def _years(rows, losses_bf="0", today=date(2026, 7, 1)):
    c, _ = compute_cgt(rows)
    return summarise_years(rows, c, losses_bf=Decimal(losses_bf), today=today)


def test_tax_year_of_boundaries():
    assert tax_year_of(date(2026, 4, 5)) == 2025
    assert tax_year_of(date(2026, 4, 6)) == 2026
    assert tax_year_of(date(2026, 1, 15)) == 2025
    assert tax_year_of(date(2026, 12, 25)) == 2026


def test_aea_for():
    assert aea_for(2020) == Decimal("12300")
    assert aea_for(2022) == Decimal("12300")
    assert aea_for(2023) == Decimal("6000")
    assert aea_for(2024) == Decimal("3000")
    assert aea_for(2030) == Decimal("3000")


def test_summarise_gain_under_aea():
    rows = [
        row("2026-04-06T00:00", "OPENING", in_a="ABC", in_q="10", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="10", value="3000"),
    ]
    ys = _years(rows)
    assert len(ys) == 1 and ys[0].year == 2026
    y = ys[0]
    assert y.disposals == 1 and y.proceeds == Decimal("3000")
    assert y.net == Decimal("2000")
    assert y.taxable == Decimal("0")
    assert y.losses_cf == Decimal("0")


def test_current_year_losses_offset_first():
    # gains 5000 + losses -4000 -> net 1000 <= AEA; b/f pool untouched
    rows = [
        row("2026-04-06T00:00", "OPENING", in_a="AAA", in_q="1", value="1000"),
        row("2026-05-01T10:00", "SELL", out_a="AAA", out_q="1", value="6000"),
        row("2026-04-06T00:00", "OPENING", in_a="BBB", in_q="1", value="5000"),
        row("2026-05-02T10:00", "SELL", out_a="BBB", out_q="1", value="1000"),
    ]
    y = _years(rows, losses_bf="1000")[0]
    assert y.net == Decimal("1000"), y.net
    assert y.losses_bf_used == Decimal("0")
    assert y.taxable == Decimal("0")
    assert y.losses_cf == Decimal("1000")


def test_carry_forward_chain():
    # year 1 (2025/26): net loss -2000; year 2 (2026/27): gain 6000
    rows = [
        row("2025-04-06T00:00", "OPENING", in_a="AAA", in_q="1", value="3000"),
        row("2025-06-01T10:00", "SELL", out_a="AAA", out_q="1", value="1000"),
        row("2026-04-06T00:00", "OPENING", in_a="BBB", in_q="1", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="BBB", out_q="1", value="7000"),
    ]
    y1, y2 = _years(rows)
    assert y1.net == Decimal("-2000") and y1.losses_cf == Decimal("2000")
    assert y2.losses_bf == Decimal("2000")
    assert y2.losses_bf_used == Decimal("2000")   # 6000 - 3000 AEA = 3000 headroom
    assert y2.taxable == Decimal("1000")          # 6000 - 2000 - 3000
    assert y2.losses_cf == Decimal("0")


def test_bf_losses_stop_at_aea():
    # b/f 10000, net gain 4000 -> only 1000 used (down to AEA), 9000 carried
    rows = [
        row("2026-04-06T00:00", "OPENING", in_a="AAA", in_q="1", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="AAA", out_q="1", value="5000"),
    ]
    y = _years(rows, losses_bf="10000")[0]
    assert y.losses_bf_used == Decimal("1000")
    assert y.taxable == Decimal("0")
    assert y.losses_cf == Decimal("9000")


def test_chain_through_empty_year():
    # loss in 2024/25, nothing in 2025/26, gain in 2026/27 — three contiguous
    # columns, pool intact through the middle
    rows = [
        row("2024-05-01T00:00", "OPENING", in_a="AAA", in_q="1", value="3000"),
        row("2024-06-01T10:00", "SELL", out_a="AAA", out_q="1", value="500"),
        row("2026-04-06T00:00", "OPENING", in_a="BBB", in_q="1", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="BBB", out_q="1", value="9000"),
    ]
    ys = _years(rows)
    assert [y.year for y in ys] == [2024, 2025, 2026]
    assert ys[0].losses_cf == Decimal("2500")
    assert ys[1].disposals == 0 and ys[1].losses_bf == Decimal("2500")
    assert ys[1].losses_cf == Decimal("2500")
    assert ys[2].losses_bf_used == Decimal("2500")
    assert ys[2].taxable == Decimal("2500")  # 8000 - 2500 - 3000


def test_reward_income_split_by_year():
    rows = [
        row("2026-04-05T10:00", "REWARD", in_a="SOL", in_q="1", value="100", taxed="80"),
        row("2026-04-06T10:00", "REWARD", in_a="SOL", in_q="1", value="200"),
    ]
    y1, y2 = _years(rows)
    assert y1.year == 2025 and y1.reward_income == Decimal("80")   # income_taxed preferred
    assert y2.year == 2026 and y2.reward_income == Decimal("200")  # falls back to value


def test_build_summary_block_layout():
    rows = [
        row("2026-04-06T00:00", "OPENING", in_a="ABC", in_q="10", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="10", value="3000"),
    ]
    ys = _years(rows)
    block = build_summary_block(ys, datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert len(block) == SUMMARY_BLOCK_HEIGHT
    assert block[0][0] == SUMMARY_MARKER
    assert block[1] == ["Tax year", "2026/27"]
    assert block[2] == ["Disposals", 1]
    assert isinstance(block[3][1], float) and block[3][1] == 3000.0   # proceeds
    assert block[12][1] == "NO"                                       # >50k flag
    labels = [r[0] for r in block[1:]]
    assert labels == [
        "Tax year", "Disposals", "Total proceeds", "Total gains", "Total losses",
        "Net gain/loss", "Losses b/f available", "Losses b/f used",
        "Annual exempt amount", "Taxable gain (after losses + AEA)",
        "Losses carried forward", "Proceeds > £50k (report even if no tax due)",
        "Reward income (taxed at receipt)",
    ], labels


def test_gap_detector_flags_shortfall():
    # SELL with no prior acquisition => SHORTFALL: exactly the "sold on an
    # unsynced venue" gap. The detector must surface it loudly.
    rows = [row("2026-06-01T10:00", "SELL", out_a="XRP", out_q="100", value="500")]
    c, _ = compute_cgt(rows)
    assert "SHORTFALL" in c[rows[0].idx].match, c[rows[0].idx].match
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        _warn_flagged_rows(rows, c)
    out = buf.getvalue()
    assert "1 flagged row" in out and "XRP" in out and "SHORTFALL" in out, out


def test_gap_detector_silent_when_clean():
    rows = [
        row("2026-04-05T00:00", "OPENING", in_a="ABC", in_q="100", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="ABC", out_q="50", value="700"),
    ]
    c, _ = compute_cgt(rows)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        _warn_flagged_rows(rows, c)
    assert buf.getvalue() == "", buf.getvalue()


def test_coinbase_source_since_filter():
    # fetch_coinbase_rows must drop pre-window txns; the daily pipeline dedupes
    # the rest by COINBASE: txid.
    import coinbase_backfill
    all_rows = [
        row("2026-01-01T10:00", "SELL", out_a="XRP", out_q="10", value="50"),   # pre
        row("2026-06-01T10:00", "SELL", out_a="XRP", out_q="20", value="200"),  # in
    ]
    orig = coinbase_backfill.fetch_coinbase_rows
    coinbase_backfill.fetch_coinbase_rows = lambda: all_rows
    try:
        since = datetime(2026, 4, 6, tzinfo=timezone.utc)
        kept = fetch_coinbase_rows(since)
    finally:
        coinbase_backfill.fetch_coinbase_rows = orig
    assert [r.qty_out for r in kept] == [Decimal("20")], kept
    assert all(r.txid.startswith("TEST:") for r in kept)  # txids preserved for dedupe


def test_backfill_history_collapses_and_reproduces_gain():
    # A pre-window Coinbase history: two XRP buys + one XRP disposal, plus an ETH
    # buy (excluded — the ETH stub already carries it) and an in-window XRP sell
    # (excluded — the daily sync ingests on/after `since`). The backfill must emit
    # one OPENING (whole pool) + the pre-window disposal, and recompute to the
    # exact gain the raw history yields.
    import coinbase_backfill
    since = datetime(2026, 4, 6, tzinfo=timezone.utc)
    history = [
        row("2024-06-01T10:00", "BUY", in_a="XRP", in_q="100", value="100"),
        row("2024-07-01T10:00", "BUY", in_a="XRP", in_q="100", value="300"),
        row("2025-02-01T10:00", "SELL", out_a="XRP", out_q="150", value="600"),
        row("2025-03-01T10:00", "BUY", in_a="ETH", in_q="1", value="1000"),
        row("2026-06-01T10:00", "SELL", out_a="XRP", out_q="20", value="90"),
    ]
    orig = coinbase_backfill.fetch_coinbase_rows
    coinbase_backfill.fetch_coinbase_rows = lambda: history
    try:
        built = coinbase_backfill.build_prewindow_disposal_rows(since)
    finally:
        coinbase_backfill.fetch_coinbase_rows = orig

    assets = {(r.type, r.asset_in or r.asset_out) for r in built}
    assert ("OPENING", "XRP") in assets, built
    assert all(a != "ETH" for _, a in assets), built           # ETH excluded
    assert all(r.dt < since for r in built), built              # in-window excluded
    opening = next(r for r in built if r.type == "OPENING")
    assert opening.qty_in == Decimal("200") and opening.value_gbp == Decimal("400")

    for i, r in enumerate(sorted(built, key=lambda r: r.dt)):
        r.idx = i
    c, pools = compute_cgt(sorted(built, key=lambda r: r.dt))
    sell = next(c[r.idx] for r in built if r.type == "SELL")
    assert sell.gain == Decimal("300"), sell.gain               # 600 - 400*150/200
    assert pools["XRP"] == (Decimal("50"), Decimal("100")), pools["XRP"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"OK — {len(fns)} tests")
