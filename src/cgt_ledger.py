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


def parse_dt(value: str) -> datetime:
    """Parse a ledger timestamp; naive values are taken as UTC."""
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

def fetch_t212_rows(since: datetime) -> list[Row]:
    """
    One ledger row per T212 order fill. walletImpact.netValue is the GBP cash
    movement including fees, so the pure consideration is netValue -/+ fee
    (BUY/SELL); the engine re-applies the fee for allowable cost / net proceeds.
    """
    import t212

    rows = []
    for item in t212.get_invest_order_history(since):
        order = item.get("order") or {}
        fill = item.get("fill") or {}
        if order.get("status") != "FILLED" or not fill:
            continue
        dt = t212.order_fill_time(item)
        instrument = order.get("instrument") or {}
        side = "BUY" if order.get("side", "").upper() == "BUY" else "SELL"
        qty = abs(_dec(fill.get("quantity"), Decimal(0)))
        if not qty or dt is None:
            print(f"WARN: T212 order {order.get('id')} missing qty/time, skipped")
            continue
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
        rows.append(Row(
            idx=0, dt=dt, type=side,
            asset_out=ticker if side == "SELL" else "",
            qty_out=qty if side == "SELL" else None,
            asset_in=ticker if side == "BUY" else "",
            qty_in=qty if side == "BUY" else None,
            value_gbp=gbp, method=method,
            fee_gbp=fee if fee else None, fee_orig="",
            wallet="T212-Invest", txid=f"T212:{order.get('id')}:{fill.get('id')}",
            income_taxed=None, notes=instrument.get("name", ""),
        ))
    return rows


def fetch_kraken_rows(since: datetime) -> list[Row]:
    import kraken

    since_ts = int(since.timestamp())
    rows = []

    for t in kraken.get_trades_history(since_ts):
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

        rows.append(Row(
            idx=0, dt=dt, type=rtype,
            asset_out=out_a, qty_out=out_q, asset_in=in_a, qty_in=in_q,
            value_gbp=gbp, method=method,
            fee_gbp=fee_gbp if fee_gbp else None, fee_orig=fee_orig,
            wallet="Kraken", txid=f"KRAKEN:{t['id']}",
            income_taxed=None, notes=t["pair"],
        ))

    for l in kraken.get_staking_rewards(since_ts):
        asset = kraken.normalize_asset(l["asset"])
        dt = datetime.fromtimestamp(float(l["time"]), tz=UTC)
        amount = _dec(l["amount"])
        gbp, fx = kraken.get_gbp_value(asset, amount, dt.date())
        rows.append(Row(
            idx=0, dt=dt, type="REWARD",
            asset_out="", qty_out=None, asset_in=asset, qty_in=amount,
            value_gbp=gbp, method=fx,
            fee_gbp=None, fee_orig="",
            wallet="Kraken", txid=f"KRAKEN:{l['id']}",
            income_taxed=gbp, notes="staking/earn reward (income at receipt)",
        ))

    return rows


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


def reconcile_holdings(
    rows: list[Row], asset_pools: dict[str, tuple[Decimal, Decimal]],
    holdings: dict[str, tuple[Decimal, str]], ty_start: date,
) -> list[Row]:
    """
    Stub OPENING rows for assets held in real life but under-explained by the
    ledger's Section 104 pools. Value is left blank so the row is flagged
    (⚠ MISSING VALUE) until the user fills in the real cost basis. Never
    auto-writes the reverse case (ledger tracks MORE than actually held) —
    that could be an untaxable transfer to self-custody, not a disposal —
    it's only printed as a console warning.
    """
    known_txids = {r.txid for r in rows if r.txid}
    stub_dt = datetime(ty_start.year, ty_start.month, ty_start.day, tzinfo=UTC) - timedelta(days=1)
    stubs = []
    for asset, (actual_qty, wallet) in sorted(holdings.items()):
        tracked_qty = asset_pools.get(asset, (Decimal(0), Decimal(0)))[0]
        diff = actual_qty - tracked_qty
        if diff > RECONCILE_TOLERANCE:
            txid = f"MANUAL:NEEDS-INPUT-{asset}"
            if txid in known_txids:
                continue
            stubs.append(Row(
                idx=0, dt=stub_dt, type="OPENING",
                asset_out="", qty_out=None, asset_in=asset, qty_in=diff,
                value_gbp=None, method="",
                fee_gbp=None, fee_orig="", wallet=wallet, txid=txid,
                income_taxed=None,
                notes=(f"Auto-flagged: {wallet} shows {actual_qty.normalize():f} {asset} but "
                       f"the ledger only accounts for {tracked_qty.normalize():f}. Fill in "
                       f"Value (GBP) with the cost basis for the missing {diff.normalize():f} "
                       f"units, and correct the date above if this wasn't all acquired before "
                       f"the tax year start."),
            ))
        elif diff < -RECONCILE_TOLERANCE:
            print(
                f"WARN: {asset} tracked pool ({tracked_qty.normalize():f}) exceeds actual "
                f"{wallet} balance ({actual_qty.normalize():f}) by {(-diff).normalize():f} — "
                f"check for an un-logged disposal, gift, or off-platform transfer. If moved "
                f"to self-custody, no ledger action needed; if sold/spent/gifted, add a "
                f"manual SELL row.", file=sys.stderr,
            )
    return stubs


# ---------------------------------------------------------------------------
# Sheet I/O
# ---------------------------------------------------------------------------

def read_ledger_rows(sheet_id: str, tab: str) -> list[Row]:
    values = sheets.read_range(sheet_id, tab, "A2:N")
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
        ["Last updated (UTC)", iso(datetime.now(UTC))],
    ]


WARNING_HIGHLIGHT_RGB = (1.0, 0.87, 0.68)  # soft orange
WARNING_HIGHLIGHT_FORMULA = '=LEFT($O2,2)="⚠ "'


def _apply_new_formatting(sheet_id: str, tab: str) -> None:
    """Date display format on column A + the ⚠-row highlight rule.
    Not idempotent (conditional-format rules accumulate) — call once only,
    either for a brand-new tab (setup_tab) or via --migrate for an existing one."""
    sheets.format_column_number_format(sheet_id, tab, 0, 1, "dd/mm/yy")
    sheets.add_conditional_format_formula(
        sheet_id, tab, 1, 0, 14, WARNING_HIGHLIGHT_FORMULA, WARNING_HIGHLIGHT_RGB
    )


def setup_tab(sheet_id: str, tab: str) -> None:
    created = sheets.ensure_tab(sheet_id, tab)
    sheets.write_range(sheet_id, tab, "A1:T1", [HEADERS])
    sheets.freeze_rows(sheet_id, tab, 1)
    if created:
        for col in range(14, 20):  # grey the computed columns O-T
            sheets.format_column_text_color(sheet_id, tab, col, 1, (0.45, 0.45, 0.45))
        sheets.add_conditional_format_positive_negative(sheet_id, tab, 1, 19)  # T
        _apply_new_formatting(sheet_id, tab)
    print(f"Tab '{tab}' {'created' if created else 'already existed'}; headers written.")


def migrate_formatting(sheet_id: str, tab: str) -> None:
    """One-time: bring an already-existing tab up to date with the date
    display format + ⚠-row highlight rule, and the renamed header. Re-running
    this duplicates the highlight rule — run it once only."""
    sheets.write_range(sheet_id, tab, "A1:T1", [HEADERS])
    _apply_new_formatting(sheet_id, tab)
    print(f"Migrated formatting for tab '{tab}': date display + warning highlight rule added.")


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

    computed, asset_pools = compute_cgt(rows)

    stubs: list[Row] = []
    if not args.recompute_only:
        holdings = fetch_current_holdings()
        stubs = reconcile_holdings(rows, asset_pools, holdings, ty_start)

    if args.dry_run:
        summary = summarise(rows, computed, ty_start, ty_end)
        print("Summary (existing rows only):")
        for label, value in summary:
            print(f"  {label}: {value}")
        if stubs:
            print("Would flag as needing input:")
            for r in stubs:
                print("  ", row_to_cells(r))
        return

    if stubs:
        next_idx = (max(r.idx for r in rows) + 1) if rows else 2
        for i, r in enumerate(stubs):
            r.idx = next_idx + i
        sheets.append_rows(sheet_id, tab, [row_to_cells(r) for r in stubs])
        rows.extend(stubs)
        print(f"Flagged {len(stubs)} under-tracked holding(s) for manual input: "
              + ", ".join(r.asset_in for r in stubs))
        computed, asset_pools = compute_cgt(rows)

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

    write_dates(sheet_id, tab, rows)
    write_computed(sheet_id, tab, rows, computed)
    summary = summarise(rows, computed, ty_start, ty_end)
    sheets.write_range(sheet_id, tab, "V1:W11", summary)
    print("Computed columns + summary written.")
    net = summary[5][1]
    print(f"Net gain/loss this tax year: £{net}")


if __name__ == "__main__":
    main()
