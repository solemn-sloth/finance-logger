#!/usr/bin/env python3
"""
Quick smoke test for Google Sheets connectivity.

Run: python tests/test_sheets.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sheets


def main():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    tab = os.environ.get("GOOGLE_SHEET_TAB")
    charity_cell = os.environ.get("GOOGLE_CHARITY_CELL")

    if not all([sheet_id, tab, charity_cell]):
        from pathlib import Path
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / "config" / ".env")
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        tab = os.environ["GOOGLE_SHEET_TAB"]
        charity_cell = os.environ["GOOGLE_CHARITY_CELL"]

    print("Testing Google Sheets connectivity...")

    print(f"  Reading charity cell ({tab}!{charity_cell})...")
    value = sheets.read_cell(sheet_id, tab, charity_cell)
    print(f"  Raw value: {value!r}")

    if value:
        annual = float(str(value).replace("£", "").replace(",", "").strip())
        monthly = round(annual / 12, 2)
        print(f"  Annual: £{annual}  →  Monthly: £{monthly}")

    print("\nSheets OK.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
