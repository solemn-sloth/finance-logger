#!/usr/bin/env python3
"""
Daily snapshot job.

Pulls Monzo balance and Trading 212 portfolio value, then writes
each to a fixed cell in Google Sheets.

Run: python src/daily_snapshot.py
Cron: 0 8 * * *
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

# Allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))

import monzo
import t212
import wise
import kraken
import sheets
import expenses
import notify


_SHEETS_EPOCH = date(1899, 12, 30)


def _upsert_pnl_row(sheet_id: str, tab: str, month_label: str, pnl: dict) -> None:
    """Merge month_label's P&L into AA-AC and rewrite the block newest-first.

    Values-only write (updateCells) — never inserts grid rows. Month labels
    are stored as sheet date serials so the cells' date format survives.
    """
    serial = (date.fromisoformat(month_label) - _SHEETS_EPOCH).days
    rows: dict[int, list] = {}
    for r in sheets.read_range(sheet_id, tab, "AA4:AC", unformatted=True):
        if not r or r[0] == "":
            continue
        vals = list(r[1:]) + [""] * (2 - len(r[1:]))
        rows[int(r[0])] = vals
    rows[serial] = [pnl["income"], pnl["expenses"]]
    block = [[s, *vals] for s, vals in sorted(rows.items(), reverse=True)]
    sheets.write_values_grid(sheet_id, tab, 4, 27, block)  # AA = col 27


def _upsert_networth_row(sheet_id: str, tab: str, date_str: str, nw: float) -> None:
    """Ensure one net-worth row for date_str's month in W-Y, newest first.

    Values-only write (updateCells) — never inserts grid rows. If the month
    already has a row it is left as-is; Change is recomputed for every row
    (vs the next-older entry) so ordering stays consistent.
    """
    serial = (date.fromisoformat(date_str) - _SHEETS_EPOCH).days
    rows: dict[int, float] = {}
    for r in sheets.read_range(sheet_id, tab, "W4:X", unformatted=True):
        if not r or r[0] == "" or len(r) < 2:
            continue
        rows[int(r[0])] = float(r[1])

    month_of = lambda s: (_SHEETS_EPOCH + timedelta(days=s)).strftime("%Y-%m")
    if month_of(serial) not in {month_of(s) for s in rows}:
        rows[serial] = nw

    ordered = sorted(rows.items(), reverse=True)
    block = []
    for i, (s, v) in enumerate(ordered):
        prev = ordered[i + 1][1] if i + 1 < len(ordered) else None
        change = round(v - prev, 2) if prev is not None else 0.0
        block.append([s, v, change])
    sheets.write_values_grid(sheet_id, tab, 4, 23, block)  # W = col 23


def main():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    tab = os.environ["GOOGLE_SHEET_TAB"]
    monzo_cell = os.environ["GOOGLE_MONZO_CELL"]
    t212_isa_cell = os.environ["GOOGLE_T212_ISA_CELL"]
    t212_isa_deposits_cell = os.environ["GOOGLE_T212_ISA_DEPOSITS_CELL"]
    isa_carryover = float(os.environ.get("T212_ISA_CARRYOVER", "0"))
    kraken_cell = os.environ["GOOGLE_KRAKEN_CELL"]
    today = date.today().isoformat()

    print(f"[{today}] Starting daily snapshot...")

    # 1. Monzo — refresh token then fetch balance
    print("  Refreshing Monzo token...")
    access_token = monzo.refresh_access_token()

    print("  Fetching Monzo balance...")
    balance = monzo.get_balance(access_token)
    print(f"  Monzo balance: £{balance}")

    print("  Fetching Wise balance...")
    wise_balance = 0.0
    try:
        wise_balance = wise.get_balance()
        print(f"  Wise balance: £{wise_balance}")
    except Exception as e:
        print(f"  Wise balance skipped: {e}", file=sys.stderr)

    # 2. Trading 212 — ISA snapshot
    print("  Fetching T212 ISA portfolio...")
    isa = t212.get_portfolio()
    print(f"  T212 ISA value: £{isa['value']}")

    # 3. Write to Sheets
    total_cash = round(balance + wise_balance, 2)
    print(f"  Total cash (Monzo + Wise): £{total_cash}")
    print(f"  Writing total cash to {tab}!{monzo_cell}...")
    sheets.update_cell(sheet_id, tab, monzo_cell, total_cash)

    print(f"  Writing T212 ISA value to {tab}!{t212_isa_cell}...")
    sheets.update_cell(sheet_id, tab, t212_isa_cell, isa["value"])

    isa_deposits = round(isa["total_cost"] - isa_carryover, 2)
    print(f"  Writing T212 ISA deposits to {tab}!{t212_isa_deposits_cell} "
          f"(total_cost £{isa['total_cost']} − carryover £{isa_carryover} = £{isa_deposits})...")
    sheets.update_cell(sheet_id, tab, t212_isa_deposits_cell, isa_deposits)

    # 4. Wise — skipped (no longer using Wise; E6 holds cash balance manually)
    # wise_balance = wise.get_balance()
    # sheets.update_cell(sheet_id, tab, wise_cell, wise_balance)

    # 5. Kraken — total GBP equivalent balance
    print("  Fetching Kraken total balance...")
    kraken_balance = kraken.get_total_balance()
    print(f"  Kraken balance: £{kraken_balance}")

    print(f"  Writing Kraken balance to {tab}!{kraken_cell}...")
    sheets.update_cell(sheet_id, tab, kraken_cell, kraken_balance)

    # 6. P&L tracking (income / expenses for current month) + net worth snapshot
    try:
        today_date = date.today()
        month_label = date(today_date.year, today_date.month, 1).isoformat()
        month_start = (
            datetime(today_date.year, today_date.month, 1, tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        month_end = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        print("  Fetching Monzo transactions...")
        monzo_txs = monzo.get_transactions(access_token, month_start, month_end)
        print(f"  Got {len(monzo_txs)} Monzo transactions")

        expenses.log_wise_balance(wise_balance, today_date.isoformat())
        monzo_wise_topups = expenses.get_monzo_wise_topups(monzo_txs)
        wise_spend = expenses.compute_wise_monthly_spend(wise_balance, monzo_wise_topups, today_date.strftime("%Y-%m"))
        if wise_spend:
            print(f"  Wise inferred spend: £{wise_spend} (topups: £{monzo_wise_topups})")

        pnl = expenses.aggregate(monzo_txs, [])
        pnl["expenses"] = round(pnl["expenses"] + wise_spend, 2)
        pnl["net"] = round(pnl["income"] - pnl["expenses"], 2)
        print(f"  P&L: income=£{pnl['income']}, expenses=£{pnl['expenses']}, net=£{pnl['net']}")

        print(f"  Writing P&L to {tab}!AA-AC (month {month_label})...")
        _upsert_pnl_row(sheet_id, tab, month_label, pnl)

        # Net worth: one row per month, written by the first successful run of
        # the month (not day-1 only, so a crashed day-1 run can't lose a month)
        nw_str = sheets.read_cell(sheet_id, tab, "E18")
        try:
            nw = round(float(str(nw_str).replace(",", "").replace("£", "")), 2)
        except (ValueError, TypeError):
            nw = 0.0
        if nw > 0:
            print(f"  Net worth on {today_date}: £{nw} (from E18) — upserting history...")
            _upsert_networth_row(sheet_id, tab, today_date.isoformat(), nw)
        else:
            print(f"  Net worth sanity check failed (E18={nw_str!r}), skipping.", file=sys.stderr)
    except Exception as e:
        print(f"  P&L / net worth step failed: {e}", file=sys.stderr)

    print(f"[{today}] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        try:
            notify.send_alert("daily_snapshot failed", str(e))
        except Exception as notify_err:
            print(f"ALSO FAILED to send alert: {notify_err}", file=sys.stderr)
        sys.exit(1)
