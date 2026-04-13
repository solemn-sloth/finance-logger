#!/usr/bin/env python3
"""
Smoke test: verify Kraken API connectivity and total GBP balance fetch.

Run: python tests/test_kraken.py
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kraken

balance = kraken.get_total_balance()
assert isinstance(balance, float), f"Expected float, got {type(balance)}"
assert balance >= 0, f"Expected non-negative balance, got {balance}"
print(f"Kraken total balance (GBP equivalent): £{balance}")
print("OK")
