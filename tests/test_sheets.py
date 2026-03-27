#!/usr/bin/env python3
"""
Smoke test: verify Google Sheets connectivity.

Run: python tests/test_sheets.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import sheets

sheet_id = os.environ["GOOGLE_SHEET_ID"]
tab = os.environ["GOOGLE_SHEET_TAB"]
cell = os.environ["GOOGLE_MONZO_CELL"]

print(f"Reading {tab}!{cell}...")
value = sheets.read_cell(sheet_id, tab, cell)
print(f"Value: {value!r}")
print("Sheets OK.")
