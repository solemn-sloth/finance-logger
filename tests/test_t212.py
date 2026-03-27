#!/usr/bin/env python3
"""
Quick smoke test for Trading 212 API connectivity.

Run: python tests/test_t212.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import t212


def main():
    print("Testing Trading 212 API...")

    portfolio = t212.get_portfolio()

    print(f"  Portfolio value:  £{portfolio['value']}")
    print(f"  Profit (abs):     £{portfolio['profit_abs']}")
    print(f"  Profit (%):        {portfolio['profit_pct']}%")

    assert isinstance(portfolio["value"], float), "value should be a float"
    assert isinstance(portfolio["profit_pct"], float), "profit_pct should be a float"

    print("\nT212 OK.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
