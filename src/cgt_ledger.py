#!/usr/bin/env python3
"""
UK Capital Gains Tax ledger — maintains the 'CGT Ledger' sheet tab.

Sources: Trading 212 Invest (GIA) order history + Kraken trades/rewards,
plus manually entered rows (RSUs, anything without an API). The sheet is
the source of truth for input columns A-N; this script appends new API
transactions (deduped on Txn ID, column L) and recomputes the derived
columns O-T for every row using UK share-matching rules: same-day,
30-day bed & breakfast (TCGA92 s106A), then Section 104 pooling.

Input columns are never rewritten, so manual rows always survive.

Usage:
  python3 src/cgt_ledger.py                 # sync from APIs + recompute
  python3 src/cgt_ledger.py --setup         # one-time: create tab, headers, formats
  python3 src/cgt_ledger.py --migrate       # one-time: apply new formatting to an existing tab
  python3 src/cgt_ledger.py --dry-run       # print what would change, write nothing
  python3 src/cgt_ledger.py --recompute-only  # skip APIs, recompute existing rows
  python3 src/cgt_ledger.py --sort          # also physically re-sort rows by date

Manual row conventions:
  - Opening Section 104 pool (holdings pre-dating the tax year): one row per
    asset — Type OPENING, Asset In + Qty In, Value = pool cost in GBP,
    date it 2026-04-05, Txn ID MANUAL:OPENING-<ASSET>.
  - RSU vest: Type REWARD, Asset In/Qty In, Value = market value at vest,
    Income Taxed = amount already taxed through payroll (becomes cost basis).
  - Any manual Txn ID must be unique, prefix MANUAL: recommended.

Reconciliation: each run compares actual T212/Kraken holdings against what
the ledger's Section 104 pools account for. Any asset held in real life but
under-explained by the ledger gets a stub OPENING row appended with Txn ID
MANUAL:NEEDS-INPUT-<ASSET> — Value is left blank on purpose so it's flagged
(⚠ MISSING VALUE) and highlighted (see --migrate) until you fill in the real
cost basis and, if needed, correct the date. This is a point-in-time snapshot:
once appended, the row is yours — later runs never edit or re-check it.
The reverse case (ledger tracks more than you actually hold) is never
auto-written — it may be an untaxable transfer to self-custody rather than
a disposal — only a console warning is printed.

REWARD and TRANSFER_IN/OUT rows are bookkeeping (pool cost, matching and
dedupe correctness) — the tab's basic filter hides them from view; the
tax-year reward income total is surfaced in the summary block instead.
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import os

import sheets

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

HEADERS = [
    "Date", "Type", "Asset Out", "Qty Out", "Asset In", "Qty In",
    "Value (GBP)", "Valuation Method", "Fee (GBP)", "Fee (orig)", "Wallet",
    "Txn ID", "Income Taxed (GBP)", "Notes",
    "Match Rule", "Pool Units Before", "Pool Cost Before (GBP)",
    "Pool Units After", "Pool Cost After (GBP)", "Gain/Loss (GBP)",
]
N_INPUT_COLS = 14   # A-N
N_COLS = 20         # A-T

ANNUAL_EXEMPT_AMOUNT = Decimal("3000")
PROCEEDS_REPORTING_THRESHOLD = Decimal("50000")
RECONCILE_TOLERANCE = Decimal("0.000001")
WARNING_MARK = "⚠ "

DISPOSAL_TYPES = {"SELL", "SWAP"}
ACQUISITION_TYPES = {"BUY", "SWAP", "REWARD", "OPENING"}
KNOWN_TYPES = DISPOSAL_TYPES | ACQUISITION_TYPES | {"TRANSFER_IN", "TRANSFER_OUT", "FEE"}

# Kept in the data (pool cost, matching and dedupe correctness) but hidden
# from view by the tab's basic filter — bookkeeping noise, not decisions.
HIDDEN_ROW_TYPES = ["REWARD", "TRANSFER_IN", "TRANSFER_OUT"]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _dec(value, default=None) -> Decimal | None:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    cleaned = str(value).replace("£", "").replace(",", "").strip()
    if not cleaned:
        return default
    return Decimal(cleaned)


_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d/%m/%y",
]


_SHEETS_EPOCH = datetime(1899, 12, 30, tzinfo=UTC)


def parse_dt(value) -> datetime:
    """Parse a ledger timestamp; naive values are taken as UTC.

    Numeric values are Google Sheets date serials (days since 1899-12-30),
    which is how real Date cells come back from an unformatted read.
    """
    if isinstance(value, (int, float)):
        return _SHEETS_EPOCH + timedelta(days=float(value))
    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"Unparseable date: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_str(dt: datetime) -> str:
    """Calendar date (Europe/London) as unambiguous ISO YYYY-MM-DD, for column A.
    Written USER_ENTERED so Sheets stores it as a real Date type; the column's
    number format (see sheets.format_column_number_format) then displays it
    as dd/mm/yy with no time."""
    return dt.astimezone(LONDON).date().isoformat()


def _num(dec: Decimal | None, places: int = 2):
    """Decimal -> float for the sheet, or '' if None."""
    if dec is None:
        return ""
    return float(round(dec, places))


# ---------------------------------------------------------------------------
# Ledger rows
# ---------------------------------------------------------------------------

@dataclass
class Row:
    idx: int                 # physical sheet row number (1-based; data starts at 2)
    dt: datetime | None
    type: str
    asset_out: str
    qty_out: Decimal | None
    asset_in: str
    qty_in: Decimal | None
    value_gbp: Decimal | None
    method: str
    fee_gbp: Decimal | None
    fee_orig: str
    wallet: str
    txid: str
    income_taxed: Decimal | None
    notes: str
    warning: str = ""
    raw: list | None = None  # original sheet cells A-N, kept verbatim for --sort


def parse_row(idx: int, cells: list) -> Row | None:
    cells = list(cells) + [""] * (N_INPUT_COLS - len(cells))
    if not str(cells[0]).strip() and not str(cells[11]).strip():
        return None  # blank row
    warning = ""
    try:
        dt = parse_dt(cells[0])
    except ValueError:
        dt = None
        warning = "BAD DATE"
    rtype = str(cells[1]).strip().upper()
    if rtype not in KNOWN_TYPES:
        warning = (warning + " " if warning else "") + f"UNKNOWN TYPE '{rtype}'"
    return Row(
        idx=idx, dt=dt, type=rtype,
        asset_out=str(cells[2]).strip(), qty_out=_dec(cells[3]),
        asset_in=str(cells[4]).strip(), qty_in=_dec(cells[5]),
        value_gbp=_dec(cells[6]), method=str(cells[7]).strip(),
        fee_gbp=_dec(cells[8]), fee_orig=str(cells[9]).strip(),
        wallet=str(cells[10]).strip(), txid=str(cells[11]).strip(),
        income_taxed=_dec(cells[12]), notes=str(cells[13]).strip(),
        warning=warning, raw=cells[:N_INPUT_COLS],
    )


def row_to_cells(r: Row) -> list:
    return [
        date_str(r.dt) if r.dt else "", r.type, r.asset_out, _num(r.qty_out, 8), r.asset_in,
        _num(r.qty_in, 8), _num(r.value_gbp), r.method, _num(r.fee_gbp),
        r.fee_orig, r.wallet, r.txid, _num(r.income_taxed), r.notes,
    ]


# ---------------------------------------------------------------------------
# CGT engine (pure) — same-day, 30-day B&B, Section 104 pool
# ---------------------------------------------------------------------------

@dataclass
class _Acq:
    row: Row
    asset: str
    dt: datetime
    qty: Decimal
    cost: Decimal            # total allowable cost (incl. buy fee)
    opening: bool = False    # OPENING rows go straight to the pool
    remaining: Decimal = None
    matched: list = field(default_factory=list)  # (rule, qty)

    def __post_init__(self):
        self.remaining = self.qty

    @property
    def day(self) -> date:
        return self.dt.astimezone(LONDON).date()

    def take(self, qty: Decimal) -> Decimal:
        """Consume qty units, return their pro-rata cost."""
        cost = self.cost * qty / self.qty
        self.remaining -= qty
        return cost


@dataclass
class _Disp:
    row: Row
    asset: str
    dt: datetime
    qty: Decimal
    proceeds: Decimal        # net of sell fee
    remaining: Decimal = None
    components: list = field(default_factory=list)  # (rule, qty, cost)

    def __post_init__(self):
        self.remaining = self.qty

    @property
    def day(self) -> date:
        return self.dt.astimezone(LONDON).date()

    @property
    def gain(self) -> Decimal:
        per_unit = self.proceeds / self.qty
        return sum((per_unit * q - c for _, q, c in self.components), Decimal(0))

    @property
    def rule(self) -> str:
        parts = []
        for rule, q, _ in self.components:
            parts.append(f"{rule} {q.normalize():f}")
        if len(parts) == 1:
            return self.components[0][0]
        return " + ".join(parts)


@dataclass
class Computed:
    match: str = ""
    pool_before: tuple | None = None   # (units, cost)
    pool_after: tuple | None = None
    gain: Decimal | None = None


def _events(rows: list[Row]) -> tuple[list[_Acq], list[_Disp]]:
    acqs, disps = [], []
    for r in rows:
        if r.dt is None or r.warning:
            continue
        fee = r.fee_gbp or Decimal(0)
        value = r.value_gbp or Decimal(0)
        if r.type in ("BUY", "REWARD", "OPENING"):
            if not r.asset_in or not r.qty_in:
                r.warning = "MISSING ASSET/QTY IN"
                continue
            if r.type == "BUY":
                if r.value_gbp is None:
                    r.warning = "MISSING VALUE"
                    continue
                cost = value + fee
            elif r.type == "REWARD":
                if r.value_gbp is None and r.income_taxed is None:
                    r.warning = "MISSING VALUE"
                    continue
                cost = r.income_taxed if r.income_taxed is not None else value
            else:  # OPENING
                if r.value_gbp is None:
                    r.warning = "MISSING VALUE"
                    continue
                cost = value
            acqs.append(_Acq(r, r.asset_in, r.dt, r.qty_in, cost,
                             opening=(r.type == "OPENING")))
        elif r.type == "SELL":
            if not r.asset_out or not r.qty_out:
                r.warning = "MISSING ASSET/QTY OUT"
                continue
            if r.value_gbp is None:
                r.warning = "MISSING VALUE"
                continue
            disps.append(_Disp(r, r.asset_out, r.dt, r.qty_out, value - fee))
        elif r.type == "SWAP":
            if not r.asset_out or not r.qty_out or not r.asset_in or not r.qty_in:
                r.warning = "SWAP NEEDS BOTH SIDES"
                continue
            if r.value_gbp is None:
                r.warning = "MISSING VALUE"
                continue
            disps.append(_Disp(r, r.asset_out, r.dt, r.qty_out, value - fee))
            acqs.append(_Acq(r, r.asset_in, r.dt, r.qty_in, value))
        # TRANSFER_IN/OUT and FEE rows: audit trail only, no CGT event
    return acqs, disps


def compute_cgt(
    rows: list[Row],
) -> tuple[dict[int, Computed], dict[str, tuple[Decimal, Decimal]]]:
    """
    Run UK share matching over all rows.

    Returns (computed values keyed by row idx, final Section 104 pool
    (units, cost) per asset after all matching).
    """
    acqs, disps = _events(rows)
    out: dict[int, Computed] = {r.idx: Computed() for r in rows}
    asset_pools: dict[str, tuple[Decimal, Decimal]] = {}

    assets = sorted({a.asset for a in acqs} | {d.asset for d in disps})
    for asset in assets:
        a_list = sorted((a for a in acqs if a.asset == asset), key=lambda a: a.dt)
        d_list = sorted((d for d in disps if d.asset == asset), key=lambda d: d.dt)

        # 1. Same-day
        for d in d_list:
            for a in a_list:
                if a.opening or a.day != d.day or a.remaining <= 0:
                    continue
                if d.remaining <= 0:
                    break
                take = min(d.remaining, a.remaining)
                cost = a.take(take)
                a.matched.append(("SAME_DAY", take))
                d.components.append(("SAME_DAY", take, cost))
                d.remaining -= take

        # 2. 30-day bed & breakfast: acquisitions in the 30 days AFTER disposal,
        #    earliest first
        for d in d_list:
            if d.remaining <= 0:
                continue
            for a in a_list:
                if a.opening or a.remaining <= 0:
                    continue
                if not (d.day < a.day <= d.day + timedelta(days=30)):
                    continue
                take = min(d.remaining, a.remaining)
                cost = a.take(take)
                a.matched.append(("30_DAY", take))
                d.components.append(("30_DAY", take, cost))
                d.remaining -= take
                if d.remaining <= 0:
                    break

        # 3. Section 104 pool, chronological (openings first on equal timestamps)
        events = sorted(
            [(a.dt, 0 if a.opening else 1, 0, a) for a in a_list]
            + [(d.dt, 1, 1, d) for d in d_list],
            key=lambda e: e[:3],
        )
        pool_u, pool_c = Decimal(0), Decimal(0)
        for _, _, kind, ev in events:
            before = (pool_u, pool_c)
            if kind == 0:  # acquisition: unmatched remainder enters the pool
                if ev.remaining > 0:
                    pool_c += ev.cost * ev.remaining / ev.qty
                    pool_u += ev.remaining
            else:  # disposal: unmatched remainder leaves the pool at average cost
                if ev.remaining > 0:
                    take = min(ev.remaining, pool_u)
                    if take > 0:
                        cost = pool_c * take / pool_u
                        ev.components.append(("S104", take, cost))
                        pool_u -= take
                        pool_c -= cost
                    shortfall = ev.remaining - take
                    if shortfall > 0:
                        ev.components.append(("SHORTFALL", shortfall, Decimal(0)))
                    ev.remaining = Decimal(0)
            after = (pool_u, pool_c)

            comp = out[ev.row.idx]
            # SWAP rows: show the disposed-asset pool (the CGT-relevant side)
            if comp.pool_before is None or kind == 1:
                comp.pool_before, comp.pool_after = before, after

        asset_pools[asset] = (pool_u, pool_c)

        for d in d_list:
            comp = out[d.row.idx]
            comp.gain = d.gain
            comp.match = (WARNING_MARK if "SHORTFALL" in d.rule else "") + d.rule
        for a in a_list:
            comp = out[a.row.idx]
            if a.matched and not comp.match:
                comp.match = "matched: " + " + ".join(
                    f"{rule} {q.normalize():f}" for rule, q in a.matched
                )

    for r in rows:
        if r.warning:
            out[r.idx].match = f"{WARNING_MARK}{r.warning}"
    return out, asset_pools


# ---------------------------------------------------------------------------
# API sources -> Rows (input columns only; idx assigned on append)
# ---------------------------------------------------------------------------

def _t212_item_to_row(item: dict) -> Row | None:
    """
    Ledger row for one T212 order fill. walletImpact.netValue is the GBP cash
    movement including fees, so the pure consideration is netValue -/+ fee
    (BUY/SELL); the engine re-applies the fee for allowable cost / net proceeds.
    """
    import t212

    order = item.get("order") or {}
    fill = item.get("fill") or {}
    if order.get("status") != "FILLED" or not fill:
        return None
    dt = t212.order_fill_time(item)
    instrument = order.get("instrument") or {}
    side = "BUY" if order.get("side", "").upper() == "BUY" else "SELL"
    qty = abs(_dec(fill.get("quantity"), Decimal(0)))
    if not qty or dt is None:
        print(f"WARN: T212 order {order.get('id')} missing qty/time, skipped")
        return None
    impact = fill.get("walletImpact") or {}
    fee = sum((abs(_dec(t.get("quantity"), Decimal(0)))
               for t in impact.get("taxes") or []), Decimal(0))
    net_value = abs(_dec(impact.get("netValue"), None)
                    if impact.get("netValue") is not None
                    else _dec(order.get("filledValue"), Decimal(0)))
    # netValue includes the fee: strip it for BUY cost, add back for SELL gross
    gbp = net_value - fee if side == "BUY" else net_value + fee
    price = fill.get("price")
    fx = impact.get("fxRate")
    method = f"T212 walletImpact GBP (fill {price} {instrument.get('currency', '?')}" + (
        f" @ fx {fx})" if fx else ")"
    )
    ticker = order.get("ticker", "")
    return Row(
        idx=0, dt=dt, type=side,
        asset_out=ticker if side == "SELL" else "",
        qty_out=qty if side == "SELL" else None,
        asset_in=ticker if side == "BUY" else "",
        qty_in=qty if side == "BUY" else None,
        value_gbp=gbp, method=method,
        fee_gbp=fee if fee else None, fee_orig="",
        wallet="T212-Invest", txid=f"T212:{order.get('id')}:{fill.get('id')}",
        income_taxed=None, notes=instrument.get("name", ""),
    )


def fetch_t212_rows(since: datetime) -> list[Row]:
    import t212

    items = t212.get_invest_order_history(since)
    return [r for r in (_t212_item_to_row(i) for i in items) if r]


def _kraken_trade_to_row(t: dict) -> Row:
    import kraken

    base, quote = kraken.pair_assets(t["pair"])
    dt = datetime.fromtimestamp(float(t["time"]), tz=UTC)
    vol = _dec(t["vol"])
    cost = _dec(t["cost"])       # in quote currency
    fee = _dec(t["fee"], Decimal(0))  # in quote currency
    side = t["type"]

    if quote == "GBP":
        gbp, method = cost, "Kraken cost (GBP pair)"
        fee_gbp, fee_orig = fee, ""
    else:
        gbp, fx = kraken.get_gbp_value(quote, cost, dt.date())
        method = f"Kraken cost {cost.normalize():f} {quote}; {fx}"
        fee_gbp = kraken.get_gbp_value(quote, fee, dt.date())[0] if fee else Decimal(0)
        fee_orig = f"{fee.normalize():f} {quote}" if fee else ""

    if quote in kraken.FIAT:
        rtype = "BUY" if side == "buy" else "SELL"
        out_a, out_q = (base, vol) if rtype == "SELL" else ("", None)
        in_a, in_q = (base, vol) if rtype == "BUY" else ("", None)
    else:  # crypto-to-crypto = disposal of one side, acquisition of the other
        rtype = "SWAP"
        if side == "buy":
            out_a, out_q, in_a, in_q = quote, cost, base, vol
        else:
            out_a, out_q, in_a, in_q = base, vol, quote, cost

    return Row(
        idx=0, dt=dt, type=rtype,
        asset_out=out_a, qty_out=out_q, asset_in=in_a, qty_in=in_q,
        value_gbp=gbp, method=method,
        fee_gbp=fee_gbp if fee_gbp else None, fee_orig=fee_orig,
        wallet="Kraken", txid=f"KRAKEN:{t['id']}",
        income_taxed=None, notes=t["pair"],
    )


def _kraken_reward_to_row(l: dict) -> Row:
    import kraken

    asset = kraken.normalize_asset(l["asset"])
    dt = datetime.fromtimestamp(float(l["time"]), tz=UTC)
    # Ledger semantics: balance change = amount - fee, so the units actually
    # credited are net of the staking fee.
    amount = _dec(l["amount"]) - _dec(l.get("fee"), Decimal(0))
    gbp, fx = kraken.get_gbp_value(asset, amount, dt.date())
    return Row(
        idx=0, dt=dt, type="REWARD",
        asset_out="", qty_out=None, asset_in=asset, qty_in=amount,
        value_gbp=gbp, method=fx,
        fee_gbp=None, fee_orig="",
        wallet="Kraken", txid=f"KRAKEN:{l['id']}",
        income_taxed=gbp, notes="staking/earn reward (income at receipt)",
    )


def _kraken_ledger_rows(entries: list[dict], only_asset: str | None = None) -> list[Row]:
    """
    Rows for the ledger-only Kraken activity that TradesHistory can't see:
    instant conversions ("spend"/"receive" pairs, grouped by refid) and
    crypto deposits/withdrawals. Rewards and internal Earn allocations are
    handled elsewhere/skipped. only_asset limits output (and the OHLC
    valuation calls conversion triggers) to groups touching that asset.
    """
    import kraken

    rows: list[Row] = []
    conversions: dict[str, list[dict]] = {}
    for e in entries:
        etype = e.get("type")
        if etype in ("spend", "receive"):
            conversions.setdefault(e["refid"], []).append(e)
        elif etype in ("deposit", "withdrawal"):
            asset = kraken.normalize_asset(e["asset"])
            if asset in kraken.FIAT or (only_asset and asset != only_asset):
                continue
            dt = datetime.fromtimestamp(float(e["time"]), tz=UTC)
            net = _dec(e["amount"]) - _dec(e.get("fee"), Decimal(0))
            if etype == "deposit":
                rows.append(Row(
                    idx=0, dt=dt, type="TRANSFER_IN",
                    asset_out="", qty_out=None, asset_in=asset, qty_in=net,
                    value_gbp=None, method="", fee_gbp=None, fee_orig="",
                    wallet="Kraken", txid=f"KRAKEN:{e['refid']}", income_taxed=None,
                    notes=("external deposit — not a disposal/acquisition; its S104 "
                           "pool cost comes from the original purchase (e.g. Coinbase). "
                           "Add an OPENING/BUY row for it if not already covered."),
                ))
            else:
                rows.append(Row(
                    idx=0, dt=dt, type="TRANSFER_OUT",
                    asset_out=asset, qty_out=-net, asset_in="", qty_in=None,
                    value_gbp=None, method="", fee_gbp=None, fee_orig="",
                    wallet="Kraken", txid=f"KRAKEN:{e['refid']}", income_taxed=None,
                    notes="withdrawal to external wallet — not a disposal; pool unchanged",
                ))

    for refid, pair in conversions.items():
        spend = next((e for e in pair if float(e["amount"]) < 0), None)
        receive = next((e for e in pair if float(e["amount"]) > 0), None)
        if spend is None or receive is None:
            print(f"WARN: unpaired Kraken conversion {refid}, skipped", file=sys.stderr)
            continue
        s_asset = kraken.normalize_asset(spend["asset"])
        r_asset = kraken.normalize_asset(receive["asset"])
        s_fiat, r_fiat = s_asset in kraken.FIAT, r_asset in kraken.FIAT
        if s_fiat and r_fiat:
            continue
        if only_asset and only_asset not in (s_asset, r_asset):
            continue
        dt = datetime.fromtimestamp(float(spend["time"]), tz=UTC)
        s_qty = -(_dec(spend["amount"]) - _dec(spend.get("fee"), Decimal(0)))  # units out, incl fee
        r_qty = _dec(receive["amount"]) - _dec(receive.get("fee"), Decimal(0))
        s_fee = _dec(spend.get("fee"), Decimal(0))

        if s_fiat:  # fiat -> crypto = BUY
            gross = abs(_dec(spend["amount"]))
            if s_asset == "GBP":
                value, method, fee_gbp, fee_orig = gross, "Kraken convert (GBP)", s_fee, ""
            else:
                value, fx = kraken.get_gbp_value(s_asset, gross, dt.date())
                fee_gbp = kraken.get_gbp_value(s_asset, s_fee, dt.date())[0] if s_fee else Decimal(0)
                method = f"Kraken convert {gross.normalize():f} {s_asset}; {fx}"
                fee_orig = f"{s_fee.normalize():f} {s_asset}" if s_fee else ""
            rows.append(Row(
                idx=0, dt=dt, type="BUY",
                asset_out="", qty_out=None, asset_in=r_asset, qty_in=r_qty,
                value_gbp=value, method=method,
                fee_gbp=fee_gbp if fee_gbp else None, fee_orig=fee_orig,
                wallet="Kraken", txid=f"KRAKEN:{refid}", income_taxed=None,
                notes=f"instant convert {s_asset}->{r_asset}",
            ))
        elif r_fiat:  # crypto -> fiat = SELL; gross proceeds = amount before fee
            gross = _dec(receive["amount"])
            r_fee = _dec(receive.get("fee"), Decimal(0))
            if r_asset == "GBP":
                value, method, fee_gbp, fee_orig = gross, "Kraken convert (GBP)", r_fee, ""
            else:
                value, fx = kraken.get_gbp_value(r_asset, gross, dt.date())
                fee_gbp = kraken.get_gbp_value(r_asset, r_fee, dt.date())[0] if r_fee else Decimal(0)
                method = f"Kraken convert {gross.normalize():f} {r_asset}; {fx}"
                fee_orig = f"{r_fee.normalize():f} {r_asset}" if r_fee else ""
            rows.append(Row(
                idx=0, dt=dt, type="SELL",
                asset_out=s_asset, qty_out=s_qty, asset_in="", qty_in=None,
                value_gbp=value, method=method,
                fee_gbp=fee_gbp if fee_gbp else None, fee_orig=fee_orig,
                wallet="Kraken", txid=f"KRAKEN:{refid}", income_taxed=None,
                notes=f"instant convert {s_asset}->{r_asset}",
            ))
        else:  # crypto -> crypto = SWAP; value the disposed side, else the received
            value, method = None, ""
            try:
                value, fx = kraken.get_gbp_value(s_asset, s_qty, dt.date())
                method = f"Kraken convert; disposed side: {fx}"
            except Exception:
                try:
                    value, fx = kraken.get_gbp_value(r_asset, r_qty, dt.date())
                    method = f"Kraken convert; received side: {fx}"
                except Exception:
                    print(f"WARN: no GBP route for {s_asset}->{r_asset} conversion "
                          f"{refid}; Value left blank for manual input", file=sys.stderr)
            rows.append(Row(
                idx=0, dt=dt, type="SWAP",
                asset_out=s_asset, qty_out=s_qty, asset_in=r_asset, qty_in=r_qty,
                value_gbp=value, method=method,
                fee_gbp=None, fee_orig="",
                wallet="Kraken", txid=f"KRAKEN:{refid}", income_taxed=None,
                notes=f"instant convert {s_asset}->{r_asset}",
            ))
    return rows


def fetch_kraken_rows(since: datetime) -> list[Row]:
    import kraken

    since_ts = int(since.timestamp())
    entries = kraken.get_ledger_entries(since_ts)
    return (
        [_kraken_trade_to_row(t) for t in kraken.get_trades_history(since_ts)]
        + _kraken_ledger_rows(entries)
        + [_kraken_reward_to_row(l) for l in kraken.filter_reward_entries(entries)]
    )


# ---------------------------------------------------------------------------
# Holdings reconciliation — compares real T212/Kraken balances against what
# the ledger's Section 104 pools account for, and flags gaps.
# ---------------------------------------------------------------------------

def fetch_current_holdings() -> dict[str, tuple[Decimal, str]]:
    """asset -> (quantity actually held, wallet). Each source fetched
    independently so one API outage doesn't block the other."""
    holdings: dict[str, tuple[Decimal, str]] = {}
    try:
        import kraken

        for asset, qty in kraken.get_balances().items():
            holdings[asset] = (qty, "Kraken")
    except Exception as exc:
        print(f"WARN: Kraken balance fetch failed, skipping reconciliation for crypto: {exc}",
              file=sys.stderr)
    try:
        import t212

        for pos in t212.get_invest_positions():
            qty = _dec(pos.get("quantity"), Decimal(0))
            if qty > Decimal("0.00000001"):
                holdings[pos["ticker"]] = (qty, "T212-Invest")
    except Exception as exc:
        print(f"WARN: T212 positions fetch failed, skipping reconciliation for GIA: {exc}",
              file=sys.stderr)
    return holdings


BACKFILL_MARKER = "backfill v2"


def backfill_opening(
    asset: str, wallet: str, missing_qty: Decimal, ty_start: date,
    allow_excess: bool = False,
) -> tuple[list[Row], str]:
    """
    Try to reconstruct the opening Section 104 pool for `asset` from full
    pre-tax-year API history (T212 order fills / Kraken trades, conversions
    and rewards), reusing the CGT engine for the pool maths.

    Returns (rows, note): a completed OPENING row when the reconstructed units
    match missing_qty; plus a manual stub for any portion explained only by
    external deposits (unknown cost basis). Empty rows + a summary note when
    reconstruction can't account for the gap. Never guesses at a value.
    """
    window = datetime(ty_start.year, ty_start.month, ty_start.day, tzinfo=UTC)
    try:
        if wallet == "T212-Invest":
            import t212

            items = t212.get_invest_order_history(None, ticker=asset)
            hist = [r for r in (_t212_item_to_row(i) for i in items)
                    if r and r.dt < window]
            source = "T212 order history"
        else:
            import kraken

            window_ts = window.timestamp()
            trades = [t for t in kraken.get_trades_history(0)
                      if float(t["time"]) < window_ts
                      and asset in kraken.pair_assets(t["pair"])]
            entries = [l for l in kraken.get_ledger_entries(0)
                       if float(l["time"]) < window_ts]
            hist = [_kraken_trade_to_row(t) for t in trades]
            hist += _kraken_ledger_rows(entries, only_asset=asset)
            hist += [_kraken_reward_to_row(l) for l in kraken.filter_reward_entries(entries)
                     if kraken.normalize_asset(l["asset"]) == asset]
            source = "Kraken trade/conversion/reward history"
    except Exception as exc:
        print(f"WARN: {wallet} backfill fetch for {asset} failed: {exc}", file=sys.stderr)
        return [], f"{BACKFILL_MARKER}: {wallet} history fetch failed ({exc})"

    if not hist:
        return [], f"{BACKFILL_MARKER}: no pre-{ty_start} {asset} transactions found in {source}"

    for i, r in enumerate(hist):
        r.idx = i
    _, pools = compute_cgt(hist)
    units, cost = pools.get(asset, (Decimal(0), Decimal(0)))
    deposits = [r for r in hist if r.type == "TRANSFER_IN"]
    deposited = sum((r.qty_in for r in deposits), Decimal(0))
    withdrawn = sum((r.qty_out for r in hist if r.type == "TRANSFER_OUT"), Decimal(0))

    prefix = "T212" if wallet == "T212-Invest" else "KRAKEN"
    opening = Row(
        idx=0, dt=window - timedelta(days=1), type="OPENING",
        asset_out="", qty_out=None, asset_in=asset, qty_in=units,
        value_gbp=cost, method=f"{source} backfill (transactions before {ty_start})",
        fee_gbp=None, fee_orig="", wallet=wallet,
        txid=f"{prefix}:OPENING-{asset}", income_taxed=None,
        notes=(f"Opening pool auto-computed from {len(hist)} pre-{ty_start} "
               f"transaction(s) in {source}."),
    ) if units > 0 else None

    # 0.5% slack: earlier-imported reward rows carry gross (pre-fee) units and
    # Earn allocation fees nibble at balances — unit-perfect matches are rare.
    match_tol = max(RECONCILE_TOLERANCE, abs(missing_qty) * Decimal("0.005"))

    # allow_excess (disposal-triggered reconstruction): the reconstructed pool
    # may legitimately exceed the gap — e.g. part of the holding was withdrawn
    # to self-custody, which keeps it in the S104 pool but off the exchange
    # balance. Accept as long as it covers the gap.
    if allow_excess and units + deposited >= missing_qty - match_tol:
        result = [opening] if opening else []
        if deposited > 0:
            dep_detail = "; ".join(
                f"{r.qty_in.normalize():f} on {date_str(r.dt)}" for r in deposits
            )
            result.append(Row(
                idx=0, dt=window - timedelta(days=1), type="OPENING",
                asset_out="", qty_out=None, asset_in=asset, qty_in=deposited,
                value_gbp=None, method="",
                fee_gbp=None, fee_orig="", wallet=wallet,
                txid=f"MANUAL:NEEDS-INPUT-{asset}", income_taxed=None,
                notes=(f"Matches external deposit(s) of {asset}: {dep_detail}. Fill in "
                       f"Value (GBP) with what you originally paid. [{BACKFILL_MARKER}]"),
            ))
        return result, ""

    if abs(units - missing_qty) <= match_tol:
        return [opening] if opening else [], ""

    if abs(units + deposited - missing_qty) <= match_tol and deposited > 0:
        dep_detail = "; ".join(
            f"{r.qty_in.normalize():f} on {date_str(r.dt)}" for r in deposits
        )
        stub = Row(
            idx=0, dt=window - timedelta(days=1), type="OPENING",
            asset_out="", qty_out=None, asset_in=asset, qty_in=deposited,
            value_gbp=None, method="",
            fee_gbp=None, fee_orig="", wallet=wallet,
            txid=f"MANUAL:NEEDS-INPUT-{asset}", income_taxed=None,
            notes=(f"Matches external deposit(s) of {asset}: {dep_detail}. These came "
                   f"from outside {wallet} (e.g. Coinbase) so their cost basis isn't "
                   f"knowable here — fill in Value (GBP) with what you originally paid, "
                   f"and correct the date to the original acquisition. "
                   f"[{BACKFILL_MARKER}]"),
        )
        return ([opening, stub] if opening else [stub]), ""

    return [], (
        f"{BACKFILL_MARKER}: {source} reconstructs {units.normalize():f} {asset} "
        f"(cost £{cost:.2f})"
        + (f" + {deposited.normalize():f} deposited externally" if deposited else "")
        + (f" − {withdrawn.normalize():f} withdrawn" if withdrawn else "")
        + f", which doesn't add up to the {missing_qty.normalize():f} gap — "
        f"review Notes on this asset's rows and enter the opening cost manually."
    )


def reconcile_holdings(
    rows: list[Row], asset_pools: dict[str, tuple[Decimal, Decimal]],
    holdings: dict[str, tuple[Decimal, str]], ty_start: date,
) -> tuple[list[Row], list[Row]]:
    """
    Compare actual holdings against ledger pools. For under-tracked assets,
    first try backfill_opening; if that fails, produce a manual-input stub
    (Value blank so it's highlighted until filled).

    Returns (rows to append, rows to write over an existing stub in place).
    A stub row (MANUAL:NEEDS-INPUT-*) stays script-owned only while its Value
    is blank AND no backfill has been attempted yet; the moment the user fills
    Value it's theirs and is never touched. Never auto-writes the reverse case
    (ledger tracks MORE than actually held) — that could be an untaxable
    transfer to self-custody, not a disposal — console warning only.
    """
    stub_dt = datetime(ty_start.year, ty_start.month, ty_start.day, tzinfo=UTC) - timedelta(days=1)
    to_append, to_replace = [], []

    # Per-asset ledger flows. The true opening gap is against net
    # acquisition/disposal flow, not the floored pool: a disposal exceeding
    # the pool clamps at zero (SHORTFALL) and would understate how much
    # opening history is really missing.
    flows: dict[str, Decimal] = {}
    transfers_in: dict[str, list[Row]] = {}
    row_wallets: dict[str, str] = {}
    for r in rows:
        if r.dt is None or r.warning:
            continue
        if r.type in ("BUY", "REWARD", "OPENING", "SWAP") and r.asset_in:
            flows[r.asset_in] = flows.get(r.asset_in, Decimal(0)) + (r.qty_in or Decimal(0))
        if r.type in ("SELL", "SWAP") and r.asset_out:
            flows[r.asset_out] = flows.get(r.asset_out, Decimal(0)) - (r.qty_out or Decimal(0))
            row_wallets.setdefault(r.asset_out, r.wallet)
        if r.type == "TRANSFER_IN" and r.asset_in:
            transfers_in.setdefault(r.asset_in, []).append(r)

    # Candidates: (needed units, wallet, actual balance or None, allow_excess)
    candidates: dict[str, tuple[Decimal, str, Decimal | None, bool]] = {}
    for asset, (actual_qty, wallet) in holdings.items():
        tracked_qty = asset_pools.get(asset, (Decimal(0), Decimal(0)))[0]
        diff = actual_qty - tracked_qty
        if diff < -RECONCILE_TOLERANCE:
            print(
                f"WARN: {asset} tracked pool ({tracked_qty.normalize():f}) exceeds actual "
                f"{wallet} balance ({actual_qty.normalize():f}) by {(-diff).normalize():f} — "
                f"check for an un-logged disposal, gift, or off-platform transfer. If moved "
                f"to self-custody, no ledger action needed; if sold/spent/gifted, add a "
                f"manual SELL row.", file=sys.stderr,
            )
        elif diff > RECONCILE_TOLERANCE:
            needed = actual_qty - flows.get(asset, Decimal(0))
            candidates[asset] = (needed, wallet, actual_qty, False)
    # Assets disposed beyond their tracked acquisitions but no longer held
    # (e.g. fully swapped away): still need an opening pool for correct gains.
    for asset, flow in flows.items():
        if asset in candidates or asset in holdings:
            continue
        if flow < -RECONCILE_TOLERANCE:
            candidates[asset] = (-flow, row_wallets.get(asset, "Kraken"), None, True)

    for asset, (needed, wallet, actual_qty, allow_excess) in sorted(candidates.items()):
        stub_txid = f"MANUAL:NEEDS-INPUT-{asset}"
        existing = next((r for r in rows if r.txid == stub_txid), None)
        if existing is not None:
            if existing.value_gbp is not None:  # user filled it in — theirs now
                continue
            if BACKFILL_MARKER in existing.notes:  # this version already tried
                continue

        resolved, attempt_note = backfill_opening(asset, wallet, needed, ty_start,
                                                  allow_excess=allow_excess)
        if resolved:
            if existing is not None:
                resolved[0].idx = existing.idx
                to_replace.append(resolved[0])
                to_append.extend(resolved[1:])
            else:
                to_append.extend(resolved)
            continue

        transfer_hint = ""
        asset_transfers = transfers_in.get(asset, [])
        in_window_transfers = sum((r.qty_in for r in asset_transfers), Decimal(0))
        # 1% slack: tiny Earn-allocation fees can nibble at deposited amounts
        hint_tol = max(RECONCILE_TOLERANCE, in_window_transfers * Decimal("0.01"))
        if in_window_transfers and abs(needed - in_window_transfers) <= hint_tol:
            detail = "; ".join(
                f"{r.qty_in.normalize():f} on {date_str(r.dt)}" for r in asset_transfers
            )
            transfer_hint = (
                f" This gap matches your external deposit(s): {detail} — enter what you "
                f"originally paid for those coins (e.g. on Coinbase), and correct the "
                f"date to the original acquisition."
            )

        shown_balance = (f"{wallet} shows {actual_qty.normalize():f} {asset}"
                         if actual_qty is not None
                         else f"{asset} disposals exceed tracked acquisitions")
        stub = Row(
            idx=existing.idx if existing else 0, dt=stub_dt, type="OPENING",
            asset_out="", qty_out=None, asset_in=asset, qty_in=needed,
            value_gbp=None, method="",
            fee_gbp=None, fee_orig="", wallet=wallet, txid=stub_txid,
            income_taxed=None,
            notes=(f"Auto-flagged: {shown_balance} but ledger transactions only account "
                   f"for {flows.get(asset, Decimal(0)).normalize():f}. Fill in Value "
                   f"(GBP) with the cost basis for the missing {needed.normalize():f} "
                   f"units.{transfer_hint} [{attempt_note}]"),
        )
        (to_replace if existing is not None else to_append).append(stub)
    return to_append, to_replace


# ---------------------------------------------------------------------------
# Sheet I/O
# ---------------------------------------------------------------------------

def read_ledger_rows(sheet_id: str, tab: str) -> list[Row]:
    # Unformatted read: user display formats (e.g. 2dp on quantity columns)
    # must never round the values the engine computes with.
    values = sheets.read_range(sheet_id, tab, "A2:N", unformatted=True)
    rows = []
    for i, cells in enumerate(values):
        row = parse_row(i + 2, cells)
        if row:
            rows.append(row)
    return rows


def write_computed(sheet_id: str, tab: str, rows: list[Row],
                   computed: dict[int, Computed]) -> None:
    if not rows:
        return
    last = max(r.idx for r in rows)
    block = [[""] * 6 for _ in range(last - 1)]  # rows 2..last, cols O-T
    for r in rows:
        c = computed[r.idx]
        block[r.idx - 2] = [
            c.match,
            _num(c.pool_before[0], 8) if c.pool_before else "",
            _num(c.pool_before[1]) if c.pool_before else "",
            _num(c.pool_after[0], 8) if c.pool_after else "",
            _num(c.pool_after[1]) if c.pool_after else "",
            _num(c.gain),
        ]
    sheets.write_range(sheet_id, tab, f"O2:T{last}", block)


def write_dates(sheet_id: str, tab: str, rows: list[Row]) -> None:
    """
    Normalize column A's representation to a real Sheets Date (see date_str),
    for every row with a valid dt. This is a deliberate, narrow exception to
    "input columns are never rewritten": it only changes how a date displays,
    never what date a row represents. Rows with a BAD DATE warning are left
    untouched — never overwrite unparseable text the user needs to fix by hand.

    Also reapplies the dd/mm/yy number format on every run: rows appended via
    INSERT_ROWS don't inherit formatting set on the column by --setup/--migrate
    (they're new grid rows), so without this they'd fall back to Sheets'
    auto-inferred yyyy-mm-dd format. repeatCell formatting is idempotent —
    safe to reapply every run (unlike the conditional-format rule).
    """
    if not rows:
        return
    last = max(r.idx for r in rows)
    block = [[""] for _ in range(last - 1)]
    for r in rows:
        if r.dt is not None and "BAD DATE" not in r.warning:
            block[r.idx - 2] = [date_str(r.dt)]
        elif r.raw:
            block[r.idx - 2] = [r.raw[0]]  # unparseable: leave the user's text untouched
    sheets.write_range(sheet_id, tab, f"A2:A{last}", block, raw=False)
    sheets.format_column_number_format(sheet_id, tab, 0, 1, "dd/mm/yy")


def tax_year_window(start: date) -> tuple[date, date]:
    return start, date(start.year + 1, start.month, start.day) - timedelta(days=1)


def summarise(rows: list[Row], computed: dict[int, Computed],
              ty_start: date, ty_end: date) -> list[list]:
    disposals = [r for r in rows
                 if r.type in DISPOSAL_TYPES and r.dt
                 and ty_start <= r.dt.astimezone(LONDON).date() <= ty_end]
    proceeds = sum((r.value_gbp or Decimal(0) for r in disposals), Decimal(0))
    gains = sum((computed[r.idx].gain for r in disposals
                 if computed[r.idx].gain and computed[r.idx].gain > 0), Decimal(0))
    losses = sum((computed[r.idx].gain for r in disposals
                  if computed[r.idx].gain and computed[r.idx].gain < 0), Decimal(0))
    net = gains + losses
    taxable = max(Decimal(0), net - ANNUAL_EXEMPT_AMOUNT)
    aea_left = max(Decimal(0), ANNUAL_EXEMPT_AMOUNT - max(net, Decimal(0)))
    reward_income = sum(
        ((r.income_taxed if r.income_taxed is not None else r.value_gbp) or Decimal(0)
         for r in rows
         if r.type == "REWARD" and r.dt
         and ty_start <= r.dt.astimezone(LONDON).date() <= ty_end),
        Decimal(0),
    )
    return [
        ["Tax year", f"{ty_start.year}/{str(ty_end.year)[2:]} ({ty_start} to {ty_end})"],
        ["Disposals", len(disposals)],
        ["Total proceeds (GBP)", _num(proceeds)],
        ["Total gains (GBP)", _num(gains)],
        ["Total losses (GBP)", _num(losses)],
        ["Net gain/loss (GBP)", _num(net)],
        ["Annual exempt amount", _num(ANNUAL_EXEMPT_AMOUNT)],
        ["AEA remaining", _num(aea_left)],
        ["Taxable gain after AEA", _num(taxable)],
        ["Proceeds > £50k (report even if no tax due)",
         "YES" if proceeds > PROCEEDS_REPORTING_THRESHOLD else "NO"],
        ["Reward income (GBP, taxed at receipt)", _num(reward_income)],
        ["Last updated (UTC)", iso(datetime.now(UTC))],
    ]


# --- Conditional formatting (re-pinned idempotently every run) --------------
#
# The highlight formulas evaluate directly on the INPUT columns, so a cell's
# highlight clears the instant the user types into it — no waiting for the
# nightly recompute. Column O's ⚠ status text is the explanation, not the
# trigger.

_ORANGE = {"red": 1.0, "green": 0.87, "blue": 0.68}
_GREEN = {"red": 0.133, "green": 0.545, "blue": 0.133}
_RED = {"red": 0.8, "green": 0.0, "blue": 0.0}


def _highlight_rule(formula: str, col_start: int, col_end: int) -> dict:
    return {
        "ranges": [{"startRowIndex": 1, "startColumnIndex": col_start, "endColumnIndex": col_end}],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
            "format": {"backgroundColor": _ORANGE},
        },
    }


def _gain_color_rule(condition_type: str, color: dict) -> dict:
    return {
        "ranges": [{"startRowIndex": 1, "startColumnIndex": 19, "endColumnIndex": 20}],
        "booleanRule": {
            "condition": {"type": condition_type, "values": [{"userEnteredValue": "0"}]},
            "format": {"textFormat": {"foregroundColor": color}},
        },
    }


_TYPES_ARRAY = '{"BUY";"SELL";"SWAP";"REWARD";"OPENING";"TRANSFER_IN";"TRANSFER_OUT";"FEE"}'
CONDITIONAL_RULES = [
    # Value (G) required but missing
    _highlight_rule(
        '=AND($G2="", OR($B2="BUY",$B2="SELL",$B2="SWAP",$B2="OPENING",'
        ' AND($B2="REWARD",$M2="")))', 6, 7),
    # Date (A) missing on a non-empty row
    _highlight_rule('=AND($A2="", $B2<>"")', 0, 1),
    # Type (B) unrecognised
    _highlight_rule(f'=AND($B2<>"", ISNA(MATCH($B2,{_TYPES_ARRAY},0)))', 1, 2),
    # Asset/Qty Out (C:D) missing on a disposal
    _highlight_rule('=AND(OR($B2="SELL",$B2="SWAP"), OR($C2="",$D2=""))', 2, 4),
    # Asset/Qty In (E:F) missing on an acquisition
    _highlight_rule(
        '=AND(OR($B2="BUY",$B2="REWARD",$B2="OPENING",$B2="SWAP"), OR($E2="",$F2=""))', 4, 6),
    # Gain/Loss (T) green/red
    _gain_color_rule("NUMBER_GREATER", _GREEN),
    _gain_color_rule("NUMBER_LESS", _RED),
]

HEADER_NOTES = {
    "A1": "Transaction date, dd/mm/yy (Europe/London calendar day; time not tracked).",
    "L1": ("Txn ID — the sync's dedupe key (stops the daily run re-importing the same "
           "transaction). Leave blank for manual rows, or use MANUAL:<anything-unique>."),
    "M1": ("Income already taxed (GBP) — for RSU vests and staking rewards: the amount "
           "taxed as income at receipt. Becomes the acquisition cost basis."),
    "O1": ("HMRC share-matching rule applied to this disposal:\n"
           "SAME_DAY — matched against acquisitions on the same day\n"
           "30_DAY — bed & breakfast rule, matched against buys in the next 30 days\n"
           "S104 — Section 104 pool (average cost of all prior holdings)\n"
           "⚠ — row needs attention (see highlighted cells)\n\n"
           "Columns O-T are recomputed by the daily 8:30am sync — after editing "
           "inputs, values here refresh on the next run."),
}


def _apply_formatting(sheet_id: str, tab: str) -> None:
    """Full tab formatting. Everything here is idempotent — safe to re-run."""
    sheets.write_range(sheet_id, tab, "A1:T1", [HEADERS])
    sheets.freeze_rows(sheet_id, tab, 1)
    for col in range(14, 20):  # grey the computed columns O-T
        sheets.format_column_text_color(sheet_id, tab, col, 1, (0.45, 0.45, 0.45))
    sheets.format_column_number_format(sheet_id, tab, 0, 1, "dd/mm/yy")
    for col in (3, 5, 15, 17):  # quantity columns: full precision, no fake 0.00
        sheets.format_column_number_format(sheet_id, tab, col, 1, "0.########", "NUMBER")
    for col in (6, 8, 12, 16, 18, 19):  # GBP columns
        sheets.format_column_number_format(sheet_id, tab, col, 1, "£#,##0.00", "CURRENCY")
    sheets.replace_all_conditional_format_rules(sheet_id, tab, CONDITIONAL_RULES)
    sheets.set_basic_filter(sheet_id, tab, {1: HIDDEN_ROW_TYPES})
    sheets.set_column_hidden(sheet_id, tab, 11)  # Txn ID: machine bookkeeping
    for cell, note in HEADER_NOTES.items():
        col = ord(cell[0]) - ord("A")
        sheets.set_cell_note(sheet_id, tab, int(cell[1:]) - 1, col, note)


def setup_tab(sheet_id: str, tab: str) -> None:
    created = sheets.ensure_tab(sheet_id, tab)
    _apply_formatting(sheet_id, tab)
    print(f"Tab '{tab}' {'created' if created else 'already existed'}; formatting applied.")


def migrate_formatting(sheet_id: str, tab: str) -> None:
    """Bring an existing tab up to date with current formatting. Idempotent."""
    _apply_formatting(sheet_id, tab)
    print(f"Formatting refreshed for tab '{tab}'.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="UK CGT ledger sync + compute")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--migrate", action="store_true",
                        help="one-time: apply new formatting to an existing tab")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recompute-only", action="store_true")
    parser.add_argument("--sort", action="store_true",
                        help="physically re-sort sheet rows by date (full rewrite)")
    args = parser.parse_args()

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    tab = os.getenv("CGT_SHEET_TAB", "CGT Ledger")
    ty_start = date.fromisoformat(os.getenv("CGT_TAX_YEAR_START", "2026-04-06"))
    ty_end = tax_year_window(ty_start)[1]
    since = datetime(ty_start.year, ty_start.month, ty_start.day, tzinfo=UTC)

    if args.setup:
        setup_tab(sheet_id, tab)
        return

    if args.migrate:
        migrate_formatting(sheet_id, tab)
        return

    rows = read_ledger_rows(sheet_id, tab)
    known = {r.txid for r in rows if r.txid}
    print(f"{len(rows)} existing rows in '{tab}'")

    new_rows: list[Row] = []
    if not args.recompute_only:
        for name, fetch in (("T212", fetch_t212_rows), ("Kraken", fetch_kraken_rows)):
            try:
                fetched = fetch(since)
                fresh = [r for r in fetched if r.txid not in known]
                known.update(r.txid for r in fresh)
                new_rows.extend(fresh)
                print(f"{name}: {len(fetched)} transactions, {len(fresh)} new")
            except Exception as exc:
                print(f"WARN: {name} fetch failed, skipping source: {exc}", file=sys.stderr)
        new_rows.sort(key=lambda r: r.dt)

    if new_rows:
        if args.dry_run:
            print("Would append:")
            for r in new_rows:
                print("  ", row_to_cells(r))
        else:
            next_idx = (max(r.idx for r in rows) + 1) if rows else 2
            for i, r in enumerate(new_rows):
                r.idx = next_idx + i
            sheets.append_rows(sheet_id, tab, [row_to_cells(r) for r in new_rows])
            rows.extend(new_rows)
            print(f"Appended {len(new_rows)} rows")

    # Reconciliation (pass-1 pools feed it; needs live APIs)
    to_append: list[Row] = []
    to_replace: list[Row] = []
    if not args.recompute_only:
        _, asset_pools = compute_cgt(rows)
        holdings = fetch_current_holdings()
        to_append, to_replace = reconcile_holdings(rows, asset_pools, holdings, ty_start)

    if args.dry_run:
        computed, _ = compute_cgt(rows)
        summary = summarise(rows, computed, ty_start, ty_end)
        print("Summary (existing rows only):")
        for label, value in summary:
            print(f"  {label}: {value}")
        for verb, batch in (("append", to_append), ("update in place", to_replace)):
            if batch:
                print(f"Would {verb}:")
                for r in batch:
                    print("  ", row_to_cells(r))
        return

    # Apply reconciliation results (all row mutations happen BEFORE final compute)
    if to_replace:
        by_idx = {r.idx: r for r in to_replace}
        rows = [by_idx.get(r.idx, r) for r in rows]
        for r in to_replace:
            sheets.write_range(sheet_id, tab, f"A{r.idx}:N{r.idx}", [row_to_cells(r)])
        print(f"Updated {len(to_replace)} stub row(s) in place: "
              + ", ".join(r.asset_in for r in to_replace))
    if to_append:
        next_idx = (max(r.idx for r in rows) + 1) if rows else 2
        for i, r in enumerate(to_append):
            r.idx = next_idx + i
        sheets.append_rows(sheet_id, tab, [row_to_cells(r) for r in to_append])
        rows.extend(to_append)
        print(f"Added {len(to_append)} holding row(s): "
              + ", ".join(r.asset_in for r in to_append))

    if args.sort and rows:
        rows.sort(key=lambda r: (r.dt or datetime.max.replace(tzinfo=UTC)))
        for i, r in enumerate(rows):
            r.idx = i + 2
        full = [
            [date_str(r.dt) if r.dt else (r.raw[0] if r.raw else "")]
            + (r.raw[1:] if r.raw else row_to_cells(r)[1:])
            + [""] * 6
            for r in rows
        ]
        sheets.write_range(sheet_id, tab, f"A2:T{len(rows) + 1}", full)

    # Final compute AFTER every row mutation, so O-T always aligns with rows
    computed, _ = compute_cgt(rows)

    write_dates(sheet_id, tab, rows)
    write_computed(sheet_id, tab, rows, computed)
    sheets.replace_all_conditional_format_rules(sheet_id, tab, CONDITIONAL_RULES)
    sheets.set_basic_filter(sheet_id, tab, {1: HIDDEN_ROW_TYPES})
    summary = summarise(rows, computed, ty_start, ty_end)
    sheets.write_range(sheet_id, tab, f"V1:W{len(summary)}", summary)
    print("Computed columns + summary written.")
    net = summary[5][1]
    print(f"Net gain/loss this tax year: £{net}")


if __name__ == "__main__":
    main()
