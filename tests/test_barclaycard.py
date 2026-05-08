#!/usr/bin/env python3
"""
Smoke test: verify Barclaycard balance fetch from Gmail IMAP.

Run: python tests/test_barclaycard.py
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import barclaycard

balance = barclaycard.get_balance()
assert isinstance(balance, float), f"Expected float, got {type(balance)}"
assert balance >= 0, f"Expected non-negative balance, got {balance}"
print(f"Barclaycard outstanding balance: £{balance}")
print("OK")
