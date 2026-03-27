#!/usr/bin/env python3
"""
Smoke test: verify Wise API connectivity and GBP balance fetch.

Run: python tests/test_wise.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import wise

balance = wise.get_balance()
assert isinstance(balance, float), f"Expected float, got {type(balance)}"
assert balance >= 0, f"Expected non-negative balance, got {balance}"
print(f"Wise GBP balance: £{balance}")
print("OK")
