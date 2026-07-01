#!/usr/bin/env python3
"""
One-time sheet formatting setup.

Run once to apply:
  - Green text on AB column (Income) from row 4
  - Red text on AC column (Expenses) from row 4
  - Conditional green/red on Y column (Net Worth Change) from row 4

Do NOT run repeatedly — conditional formatting rules accumulate in Sheets.

Run: python src/setup_sheet.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, os.path.dirname(__file__))

import sheets

# Column indices (0-based): W=22, X=23, Y=24, AA=26, AB=27, AC=28
_GREEN = (0.133, 0.545, 0.133)
_RED = (0.8, 0.0, 0.0)


def main():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    tab = os.environ["GOOGLE_SHEET_TAB"]

    print("Applying sheet formatting (run this script only once)...")

    print("  AB (Income): green text from row 4...")
    sheets.format_column_text_color(sheet_id, tab, col=27, row_start=3, rgb=_GREEN)

    print("  AC (Expenses): red text from row 4...")
    sheets.format_column_text_color(sheet_id, tab, col=28, row_start=3, rgb=_RED)

    print("  Y (Net Worth Change): conditional green/red from row 4...")
    sheets.add_conditional_format_positive_negative(sheet_id, tab, row_start=3, col=24)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
