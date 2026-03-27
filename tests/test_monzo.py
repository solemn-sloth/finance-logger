#!/usr/bin/env python3
"""
Quick smoke test for Monzo API connectivity.

Run: python tests/test_monzo.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import monzo


def main():
    print("Testing Monzo API...")

    print("  Refreshing access token...")
    access_token = monzo.refresh_access_token()
    print("  Token refreshed OK")

    print("  Fetching balance...")
    balance = monzo.get_balance(access_token)
    print(f"  Balance: £{balance}")

    assert isinstance(balance, float), "balance should be a float"
    assert balance >= 0, "balance should be non-negative"

    print("\nMonzo OK.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
