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
from datetime import date

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

    # 2. Trading 212 — ISA snapshot
    print("  Fetching T212 ISA portfolio...")
    isa = t212.get_portfolio()
    print(f"  T212 ISA value: £{isa['value']}")

    # 3. Trading 212 — Invest snapshot
    print("  Fetching T212 Invest portfolio...")
    invest = t212.get_invest_portfolio()
    print(f"  T212 Invest value: £{invest['value']}")

    # 4. Write to Sheets
    print(f"  Writing Monzo balance to {tab}!{monzo_cell}...")
    sheets.update_cell(sheet_id, tab, monzo_cell, balance)

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
    try:
        print("  Fetching Barclaycard balance...")
        barclaycard_balance = barclaycard.get_balance()
        print(f"  Barclaycard balance: £{barclaycard_balance}")
        print(f"  Writing Barclaycard balance to {tab}!{barclaycard_cell}...")
        sheets.update_cell(sheet_id, tab, barclaycard_cell, barclaycard_balance)
    except Exception as e:
        print(f"  Barclaycard step skipped: {e}", file=sys.stderr)

    print(f"[{today}] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
