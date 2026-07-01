"""
Transaction aggregation and transfer exclusion for P&L tracking.

Normalises transactions from Monzo and Wise into a common format,
filters out inter-account transfers, and computes monthly income/expense totals.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WISE_LOG_PATH = Path(__file__).parent.parent / "data" / "wise_log.json"


def log_wise_balance(balance: float, date_str: str) -> None:
    log = {}
    if WISE_LOG_PATH.exists():
        log = json.loads(WISE_LOG_PATH.read_text())
    log[date_str] = balance
    WISE_LOG_PATH.parent.mkdir(exist_ok=True)
    WISE_LOG_PATH.write_text(json.dumps(log, indent=2))


def get_monzo_wise_topups(monzo_txs: list[dict]) -> float:
    """Sum Monzo→Wise bank transfers this month."""
    total = 0.0
    for tx in monzo_txs:
        if tx.get("include_in_spending", True):
            continue
        desc = (
            (tx.get("merchant") or {}).get("name", "")
            or tx.get("description", "")
            or tx.get("notes", "")
        ).lower()
        if "wise" in desc or "transferwise" in desc:
            total += abs(tx["amount"]) / 100
    return round(total, 2)


def compute_wise_monthly_spend(current_balance: float, monzo_topups: float, month: str) -> float:
    """Infer Wise spending = month_start_balance + topups - current_balance.

    month: "YYYY-MM" prefix. Returns 0.0 if no log entry exists for this month yet.
    """
    if not WISE_LOG_PATH.exists():
        return 0.0
    log = json.loads(WISE_LOG_PATH.read_text())
    month_entries = {k: v for k, v in log.items() if k.startswith(month)}
    if not month_entries:
        return 0.0
    start_balance = log[min(month_entries)]
    return max(0.0, round(start_balance + monzo_topups - current_balance, 2))


@dataclass
class Transaction:
    source: str
    amount_gbp: float   # positive = income, negative = expense
    description: str
    raw: dict


def _normalise_monzo(tx: dict) -> Optional[Transaction]:
    # Primary signal: Monzo flags pot transfers, bank transfers, BACS credits
    if not tx.get("include_in_spending", True):
        return None
    if tx.get("category", "") in ("pot_transfer", "transfers"):
        return None
    amount_gbp = round(tx["amount"] / 100, 2)
    description = (tx.get("merchant") or {}).get("name") or tx.get("description", "")
    return Transaction(source="monzo", amount_gbp=amount_gbp, description=description, raw=tx)


_WISE_EXCLUDED_TYPES = {"TRANSFER", "DEPOSIT", "CONVERSION"}


def _normalise_wise(tx: dict) -> Optional[Transaction]:
    # Exclude transfers, deposits (Monzo top-ups), and FX conversions
    if tx.get("type", "") in _WISE_EXCLUDED_TYPES:
        return None
    amount = tx.get("amount", {})
    if amount.get("currency") != "GBP":
        return None
    amount_gbp = float(amount["value"])
    description = tx.get("details", {}).get("description", tx.get("type", ""))
    return Transaction(source="wise", amount_gbp=amount_gbp, description=description, raw=tx)


def aggregate(monzo_txs: list[dict], wise_txs: list[dict]) -> dict:
    """Aggregate transactions from all sources into monthly P&L totals.

    Returns:
        income:   total credits in £ (excluding transfers)
        expenses: total debits in £ as positive number (excluding transfers)
        net:      income - expenses
        tx_count: number of non-excluded transactions
    """
    txs: list[Transaction] = []
    for tx in monzo_txs:
        t = _normalise_monzo(tx)
        if t is not None:
            txs.append(t)
    for tx in wise_txs:
        t = _normalise_wise(tx)
        if t is not None:
            txs.append(t)

    income = round(sum(t.amount_gbp for t in txs if t.amount_gbp > 0), 2)
    expenses = round(abs(sum(t.amount_gbp for t in txs if t.amount_gbp < 0)), 2)
    return {
        "income": income,
        "expenses": expenses,
        "net": round(income - expenses, 2),
        "tx_count": len(txs),
    }
