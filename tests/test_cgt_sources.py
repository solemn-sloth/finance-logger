#!/usr/bin/env python3
"""
Smoke test: fetch T212 Invest order history + Kraken trades/rewards since
the tax year start and print counts + first/last items (read-only).

Run: python3 tests/test_cgt_sources.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kraken
import t212

since = datetime.fromisoformat(os.getenv("CGT_TAX_YEAR_START", "2026-04-06")).replace(
    tzinfo=timezone.utc
)
since_ts = int(since.timestamp())

print(f"--- Kraken trades since {since.date()} ---")
trades = kraken.get_trades_history(since_ts)
print(f"{len(trades)} trades")
for t in trades[:2] + trades[-1:] if trades else []:
    print(json.dumps(t, indent=2, default=str)[:600])

print(f"--- Kraken staking/earn rewards ---")
rewards = kraken.get_staking_rewards(since_ts)
print(f"{len(rewards)} rewards")
for r in rewards[:2]:
    print(json.dumps(r, indent=2, default=str)[:600])

print(f"--- T212 Invest orders since {since.date()} (slow: rate-limited) ---")
orders = t212.get_invest_order_history(since)
print(f"{len(orders)} orders")
for o in orders[:2] + orders[-1:] if orders else []:
    print(json.dumps(o, indent=2, default=str)[:900])

print("--- Kraken balances (reconciliation) ---")
balances = kraken.get_balances()
print(balances)
assert "GBP" not in balances and "ZGBP" not in balances, "fiat leaked through"

print("--- T212 Invest positions (reconciliation) ---")
positions = t212.get_invest_positions()
print(json.dumps(positions, indent=2, default=str))
order_tickers = {o.get("order", {}).get("ticker") for o in orders}
for p in positions:
    flag = "(also in this tax year's order history)" if p["ticker"] in order_tickers else ""
    print(f"  {p['ticker']}: qty={p['quantity']} {flag}")

print("OK")
