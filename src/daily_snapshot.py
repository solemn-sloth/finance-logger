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
from datetime import date, datetime, timezone

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

# Allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))

import monzo
import t212
import wise
import kraken
import barclaycard
import sheets
import expenses


def _upsert_pnl_row(sheet_id: str, tab: str, month_label: str, pnl: dict) -> None:
    """Write or overwrite the P&L row for month_label in columns AA-AC."""
    service = sheets.get_sheet_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!AA4:AA"
    ).execute()
    col_aa = [row[0] if row else "" for row in result.get("values", [])]

    if month_label in col_aa:
        row_idx = col_aa.index(month_label) + 4  # 1-based sheet row
    else:
        row_idx = len(col_aa) + 4
        sheets.update_cell(sheet_id, tab, f"AA{row_idx}", month_label)

    sheets.update_cell(sheet_id, tab, f"AB{row_idx}", pnl["income"])
    sheets.update_cell(sheet_id, tab, f"AC{row_idx}", pnl["expenses"])


def _append_networth_row(sheet_id: str, tab: str, date_str: str, nw: float) -> None:
    """Append a net worth snapshot row to columns W-Y."""
    service = sheets.get_sheet_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!W4:X"
    ).execute()
    rows = result.get("values", [])
    next_row = len(rows) + 4

    prev_nw = float(rows[-1][1]) if rows and len(rows[-1]) > 1 else None
    change = round(nw - prev_nw, 2) if prev_nw is not None else 0.0

    sheets.update_cell(sheet_id, tab, f"W{next_row}", date_str)
    sheets.update_cell(sheet_id, tab, f"X{next_row}", nw)
    sheets.update_cell(sheet_id, tab, f"Y{next_row}", change)


def main():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    tab = os.environ["GOOGLE_SHEET_TAB"]
    monzo_cell = os.environ["GOOGLE_MONZO_CELL"]
    t212_isa_cell = os.environ["GOOGLE_T212_ISA_CELL"]
    t212_invest_value_cell = os.environ["GOOGLE_T212_INVEST_VALUE_CELL"]
    t212_isa_deposits_cell = os.environ["GOOGLE_T212_ISA_DEPOSITS_CELL"]
    isa_carryover = float(os.environ.get("T212_ISA_CARRYOVER", "0"))
    kraken_cell = os.environ["GOOGLE_KRAKEN_CELL"]
    barclaycard_cell = os.environ["GOOGLE_BARCLAYCARD_CELL"]
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

    # 3. Trading 212 — Invest snapshot
    print("  Fetching T212 Invest portfolio...")
    invest = t212.get_invest_portfolio()
    print(f"  T212 Invest value: £{invest['value']}")

    # 4. Write to Sheets
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

    print(f"  Writing T212 Invest value to {tab}!{t212_invest_value_cell}...")
    sheets.update_cell(sheet_id, tab, t212_invest_value_cell, invest["value"])

    # 5. Wise — skipped (no longer using Wise; E6 holds cash balance manually)
    # wise_balance = wise.get_balance()
    # sheets.update_cell(sheet_id, tab, wise_cell, wise_balance)

    # 6. Kraken — total GBP equivalent balance
    print("  Fetching Kraken total balance...")
    kraken_balance = kraken.get_total_balance()
    print(f"  Kraken balance: £{kraken_balance}")

    print(f"  Writing Kraken balance to {tab}!{kraken_cell}...")
    sheets.update_cell(sheet_id, tab, kraken_cell, kraken_balance)

    # 7. Barclaycard — outstanding balance from forwarded email
    barclaycard_balance = 0.0
    try:
        print("  Fetching Barclaycard balance...")
        barclaycard_balance = barclaycard.get_balance()
        print(f"  Barclaycard balance: £{barclaycard_balance}")
        print(f"  Writing Barclaycard balance to {tab}!{barclaycard_cell}...")
        sheets.update_cell(sheet_id, tab, barclaycard_cell, barclaycard_balance)
    except Exception as e:
        print(f"  Barclaycard step skipped: {e}", file=sys.stderr)

    # 8. P&L tracking (income / expenses for current month) + net worth snapshot
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

        if today_date.day == 1:
            nw_str = sheets.read_cell(sheet_id, tab, "E18")
            try:
                nw = round(float(str(nw_str).replace(",", "")), 2)
            except (ValueError, TypeError):
                nw = 0.0
            if nw > 0:
                print(f"  Net worth on {today_date}: £{nw} (from E18) — appending to history...")
                _append_networth_row(sheet_id, tab, today_date.isoformat(), nw)
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
        sys.exit(1)
