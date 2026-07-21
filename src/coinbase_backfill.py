#!/usr/bin/env python3
"""
Coinbase → CGT ledger opening-cost backfill. ONE-OFF tool.

Coinbase IS a daily source now: cgt_ledger.fetch_coinbase_rows() reuses this
module's history mapper for transactions on/after CGT_TAX_YEAR_START. This
script covers the *other* half — reconstructing the GBP cost basis of coins
bought on Coinbase BEFORE CGT_TAX_YEAR_START and later moved to
Kraken/self-custody, to fill the MANUAL:NEEDS-INPUT-<ASSET> OPENING stub rows.
The ty_start boundary keeps the daily sync and this backfill from
double-counting (daily = on/after; backfill = strictly before).

Pulls full Coinbase history (retail v2 transactions + Advanced Trade fills),
maps it to ledger Rows, reruns the CGT engine to reconstruct each asset's
Section 104 pool as of the tax year start, and matches the result against the
sheet's NEEDS-INPUT stubs.

Auth: CDP API key (Ed25519) — JWT Bearer per request, EdDSA, 2-minute expiry.
Key needs View (read-only) permission only.

Usage:
  python3 src/coinbase_backfill.py                # report only (read-only)
  python3 src/coinbase_backfill.py --asset BTC    # limit to one asset
  python3 src/coinbase_backfill.py --write        # fill blank stub Values
  python3 src/coinbase_backfill.py --write --dry-run  # show cell writes only
"""

import argparse
import base64
import os
import secrets
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import cgt_ledger
import kraken
import sheets
from cgt_ledger import Row, _dec, date_str

UTC = timezone.utc
_HOST = "api.coinbase.com"
_THROTTLE = 0.35  # seconds between requests, well under Coinbase limits


# ---------------------------------------------------------------------------
# Auth + HTTP
# ---------------------------------------------------------------------------

def _private_key():
    """Ed25519 private key from the base64 CDP secret (64 bytes = seed+pub)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = base64.b64decode(os.environ["COINBASE_API_PRIVATE_KEY"])
    seed = raw[:32] if len(raw) == 64 else raw
    return Ed25519PrivateKey.from_private_bytes(seed)


def _build_jwt(method: str, path: str) -> str:
    """Per-request CDP JWT (EdDSA). uri claim excludes the query string."""
    import jwt

    key_name = os.environ["COINBASE_API_KEY_NAME"]
    now = int(time.time())
    return jwt.encode(
        {"iss": "cdp", "sub": key_name, "nbf": now, "exp": now + 120,
         "uri": f"{method} {_HOST}{path}"},
        _private_key(), algorithm="EdDSA",
        headers={"kid": key_name, "nonce": secrets.token_hex(16)},
    )


_last_call = 0.0


def _get(path: str, params: dict | None = None, max_retries: int = 5) -> dict:
    """Authed GET; throttled, retries on 429, raises with body text on error."""
    global _last_call
    for attempt in range(max_retries):
        wait = _THROTTLE - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        headers = {
            "Authorization": f"Bearer {_build_jwt('GET', path)}",
            "CB-VERSION": "2024-01-01",
        }
        resp = requests.get(f"https://{_HOST}{path}", params=params or {},
                            headers=headers, timeout=30)
        if resp.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        if resp.status_code == 401:
            raise RuntimeError(
                f"Coinbase auth failed (401) on {path}: {resp.text}\n"
                "Check COINBASE_API_KEY_NAME (full organizations/.../apiKeys/... "
                "path) and COINBASE_API_PRIVATE_KEY (base64 Ed25519 secret); "
                "JWT claims used: iss=cdp, sub/kid=key name, uri='GET <host><path>'."
            )
        if not resp.ok:
            raise RuntimeError(f"Coinbase GET {path} failed: {resp.status_code} {resp.text}")
        return resp.json()
    raise RuntimeError(f"Coinbase GET {path}: rate-limited after {max_retries} retries")


def _v2_paginated(path: str) -> list[dict]:
    """All pages of a v2 collection endpoint (follows pagination.next_uri)."""
    out: list[dict] = []
    params: dict | None = {"limit": 100}
    while path:
        body = _get(path, params)
        out.extend(body.get("data", []))
        next_uri = (body.get("pagination") or {}).get("next_uri")
        if not next_uri:
            break
        parsed = urllib.parse.urlparse(next_uri)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))
    return out


def _fills_paginated() -> list[dict]:
    """All Advanced Trade fills (cursor pagination)."""
    fills: list[dict] = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        body = _get("/api/v3/brokerage/orders/historical/fills", params)
        fills.extend(body.get("fills", []))
        cursor = body.get("cursor")
        if not cursor:
            break
    return fills


# ---------------------------------------------------------------------------
# History -> ledger Rows
# ---------------------------------------------------------------------------

def _parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def _native_gbp(tx: dict, dt: datetime) -> tuple[Decimal, str]:
    """GBP value of a v2 transaction from native_amount (converted if needed)."""
    native = tx.get("native_amount") or {}
    amt = abs(_dec(native.get("amount"), Decimal(0)))
    cur = native.get("currency", "")
    if cur == "GBP":
        return amt, "Coinbase native_amount (GBP)"
    gbp, fx = kraken.get_gbp_value(cur, amt, dt.date())
    return gbp, f"Coinbase native_amount {amt.normalize():f} {cur}; {fx}"


# v2 types treated as pure transfers (audit only, no CGT event)
_TRANSFER_TYPES = {"send", "receive", "pro_deposit", "pro_withdrawal",
                   "exchange_deposit", "exchange_withdrawal",
                   "vault_withdrawal", "transfer"}
_REWARD_TYPES = {"staking_reward", "interest", "inflation_reward",
                 "incentives_shared_clawback", "earn_payout"}
_SKIP_TYPES = {"advanced_trade_fill",  # taken from the fills endpoint instead
               "fiat_deposit", "fiat_withdrawal",
               # internal wallet<->staked-balance moves, mirrored ± pairs (net zero)
               "staking_transfer", "unstaking_transfer"}


def _v2_tx_to_rows(tx: dict, trades: dict[str, list]) -> list[Row]:
    """Map one v2 transaction to ledger Rows (convert legs are collected into
    `trades` keyed by trade id and paired later)."""
    ttype = tx.get("type", "")
    if ttype in _SKIP_TYPES or tx.get("status") != "completed":
        return []
    dt = _parse_time(tx["created_at"])
    asset = (tx.get("amount") or {}).get("currency", "")
    qty = _dec((tx.get("amount") or {}).get("amount"), Decimal(0))
    if asset in kraken.FIAT:
        return []  # fiat mirror legs — crypto legs carry the CGT info
    txid = f"COINBASE:{tx['id']}"

    if ttype == "trade":
        trade_id = (tx.get("trade") or {}).get("id") or f"unpaired-{tx['id']}"
        trades.setdefault(trade_id, []).append(tx)
        return []

    if ttype == "buy" or ttype == "sell":
        gbp, method = _native_gbp(tx, dt)
        side = "BUY" if ttype == "buy" else "SELL"
        return [Row(
            idx=0, dt=dt, type=side,
            asset_out=asset if side == "SELL" else "",
            qty_out=abs(qty) if side == "SELL" else None,
            asset_in=asset if side == "BUY" else "",
            qty_in=abs(qty) if side == "BUY" else None,
            value_gbp=gbp, method=method, fee_gbp=None, fee_orig="",
            wallet="Coinbase", txid=txid, income_taxed=None,
            notes=tx.get("details", {}).get("title", "") or f"Coinbase {ttype}",
        )]

    if ttype in _REWARD_TYPES:
        gbp, method = _native_gbp(tx, dt)
        return [Row(
            idx=0, dt=dt, type="REWARD",
            asset_out="", qty_out=None, asset_in=asset, qty_in=abs(qty),
            value_gbp=gbp, method=method, fee_gbp=None, fee_orig="",
            wallet="Coinbase", txid=txid, income_taxed=gbp,
            notes=f"Coinbase {ttype} (income at receipt)",
        )]

    if ttype in _TRANSFER_TYPES:
        direction = "TRANSFER_IN" if qty > 0 else "TRANSFER_OUT"
        return [Row(
            idx=0, dt=dt, type=direction,
            asset_out=asset if direction == "TRANSFER_OUT" else "",
            qty_out=abs(qty) if direction == "TRANSFER_OUT" else None,
            asset_in=asset if direction == "TRANSFER_IN" else "",
            qty_in=abs(qty) if direction == "TRANSFER_IN" else None,
            value_gbp=None, method="", fee_gbp=None, fee_orig="",
            wallet="Coinbase", txid=txid, income_taxed=None,
            notes=f"Coinbase {ttype}",
        )]

    print(f"WARN: unhandled Coinbase transaction type '{ttype}' "
          f"({tx['id']}, {qty} {asset}) — skipped", file=sys.stderr)
    return []


def _paired_trade_rows(trades: dict[str, list]) -> list[Row]:
    """SWAP rows from v2 convert legs paired by trade id (spend<0, receive>0)."""
    rows = []
    for trade_id, legs in trades.items():
        spend = next((t for t in legs
                      if _dec(t["amount"]["amount"], Decimal(0)) < 0), None)
        receive = next((t for t in legs
                        if _dec(t["amount"]["amount"], Decimal(0)) > 0), None)
        if spend is None or receive is None:
            print(f"WARN: unpaired Coinbase convert {trade_id}, skipped", file=sys.stderr)
            continue
        dt = _parse_time(spend["created_at"])
        gbp, method = _native_gbp(spend, dt)
        rows.append(Row(
            idx=0, dt=dt, type="SWAP",
            asset_out=spend["amount"]["currency"],
            qty_out=abs(_dec(spend["amount"]["amount"])),
            asset_in=receive["amount"]["currency"],
            qty_in=_dec(receive["amount"]["amount"]),
            value_gbp=gbp, method=f"disposed side: {method}",
            fee_gbp=None, fee_orig="",
            wallet="Coinbase", txid=f"COINBASE:{trade_id}", income_taxed=None,
            notes=(f"Coinbase convert {spend['amount']['currency']}->"
                   f"{receive['amount']['currency']}"),
        ))
    return rows


def _fill_to_row(f: dict) -> Row | None:
    """Advanced Trade fill -> BUY/SELL (fiat quote) or SWAP (crypto quote)."""
    base, quote = f["product_id"].split("-")
    dt = _parse_time(f["trade_time"])
    price = _dec(f["price"])
    size = _dec(f["size"], Decimal(0))
    if not size or not price:
        print(f"WARN: Coinbase fill {f.get('entry_id')} missing size/price, skipped",
              file=sys.stderr)
        return None
    if f.get("size_in_quote"):
        quote_qty, base_qty = size, size / price
    else:
        base_qty, quote_qty = size, size * price
    commission = _dec(f.get("commission"), Decimal(0))
    side = f.get("side", "").upper()
    txid = f"COINBASE:{f['entry_id']}"

    if quote in kraken.FIAT:
        if quote == "GBP":
            gbp, method = quote_qty, "Coinbase fill (GBP pair)"
            fee_gbp, fee_orig = commission, ""
        else:
            gbp, fx = kraken.get_gbp_value(quote, quote_qty, dt.date())
            method = f"Coinbase fill {quote_qty.normalize():f} {quote}; {fx}"
            fee_gbp = (kraken.get_gbp_value(quote, commission, dt.date())[0]
                       if commission else Decimal(0))
            fee_orig = f"{commission.normalize():f} {quote}" if commission else ""
        return Row(
            idx=0, dt=dt, type="BUY" if side == "BUY" else "SELL",
            asset_out=base if side == "SELL" else "",
            qty_out=base_qty if side == "SELL" else None,
            asset_in=base if side == "BUY" else "",
            qty_in=base_qty if side == "BUY" else None,
            value_gbp=gbp, method=method,
            fee_gbp=fee_gbp if fee_gbp else None, fee_orig=fee_orig,
            wallet="Coinbase", txid=txid, income_taxed=None,
            notes=f"Advanced Trade {f['product_id']}",
        )

    # crypto-crypto product = SWAP; value the quote side via Kraken OHLC
    gbp, fx = kraken.get_gbp_value(quote, quote_qty, dt.date())
    if side == "BUY":
        out_a, out_q, in_a, in_q = quote, quote_qty, base, base_qty
    else:
        out_a, out_q, in_a, in_q = base, base_qty, quote, quote_qty
    return Row(
        idx=0, dt=dt, type="SWAP",
        asset_out=out_a, qty_out=out_q, asset_in=in_a, qty_in=in_q,
        value_gbp=gbp, method=f"Coinbase fill; quote side: {fx}",
        fee_gbp=None, fee_orig="",
        wallet="Coinbase", txid=txid, income_taxed=None,
        notes=f"Advanced Trade {f['product_id']}",
    )


def get_balances() -> dict[str, Decimal]:
    """asset -> quantity currently held on Coinbase (non-fiat, non-dust).

    Used by cgt_ledger.fetch_current_holdings() so reconciliation can flag any
    Coinbase-held asset the ledger doesn't yet account for.
    """
    balances: dict[str, Decimal] = {}
    for acct in _v2_paginated("/v2/accounts"):
        code = (acct.get("currency") or {}).get("code", "")
        if code in kraken.FIAT:
            continue
        qty = _dec((acct.get("balance") or {}).get("amount"), Decimal(0))
        if qty > Decimal("0.00000001"):
            balances[code] = balances.get(code, Decimal(0)) + qty
    return balances


def fetch_coinbase_rows() -> list[Row]:
    """Full Coinbase history as ledger Rows (unsorted, idx unassigned)."""
    accounts = _v2_paginated("/v2/accounts")
    print(f"Coinbase: {len(accounts)} accounts")
    rows: list[Row] = []
    trades: dict[str, list] = {}
    for acct in accounts:
        code = (acct.get("currency") or {}).get("code", "")
        if code in kraken.FIAT:
            continue
        txs = _v2_paginated(f"/v2/accounts/{acct['id']}/transactions")
        if txs:
            print(f"  {code}: {len(txs)} v2 transactions")
        for tx in txs:
            rows.extend(_v2_tx_to_rows(tx, trades))
    rows.extend(_paired_trade_rows(trades))

    try:
        fills = _fills_paginated()
        print(f"  Advanced Trade: {len(fills)} fills")
        rows.extend(r for r in (_fill_to_row(f) for f in fills) if r)
    except RuntimeError as exc:
        print(f"WARN: Advanced Trade fills fetch failed ({exc}); retail v2 "
              f"history only — cross-check against Coinbase statements.",
              file=sys.stderr)

    rows.sort(key=lambda r: r.dt)
    return rows


def build_prewindow_disposal_rows(since: datetime) -> list[Row]:
    """Compact ledger rows for Coinbase disposals that happened *before* `since`
    (the ledger's tax-year start) — the coins that were bought and (partly or
    fully) sold on Coinbase in closed tax years, which the daily sync (on/after
    `since`) never sees and which therefore silently miss the CGT record.

    Per asset we emit ONE `OPENING` carrying the whole pre-window Section 104
    pool (every buy / earn-reward / swap-in collapsed into total units + total
    GBP cost, incl. fees) followed by the real disposals as `SELL` rows. This
    reproduces the exact per-year gain/loss the CGT engine computes over the raw
    ~250-row history, without flooding the ledger with sub-penny reward rows.

    ETH is deliberately excluded: the full Coinbase ETH pool equals the ledger's
    existing ETH OPENING stub (it was transferred to Kraken, not sold), so
    re-adding it — including ETH received via crypto-to-crypto swaps — would
    double-count. For that reason swap disposals are recorded as `SELL` (the
    in-leg is dropped): the received asset is either ETH (already carried) or is
    itself an independently-tracked asset whose own acquisitions are captured by
    its own OPENING. Residual dust pools (e.g. ATOM/DOT/SHIB) are preserved so
    any on/after-`since` dust disposal the daily sync ingests has a cost basis.
    """
    from collections import defaultdict

    full = sorted((r for r in fetch_coinbase_rows() if r.dt and r.dt < since),
                  key=lambda r: r.dt)
    for i, r in enumerate(full):
        r.idx = i
    _, pools = cgt_ledger.compute_cgt(full)

    by_asset: dict[str, list[Row]] = defaultdict(list)
    for r in full:
        for side in (r.asset_in, r.asset_out):
            if side:
                by_asset[side].append(r)

    out: list[Row] = []
    for asset in sorted(by_asset):
        if asset == "ETH":
            continue
        rs = by_asset[asset]
        acq = [r for r in rs if r.asset_in == asset]
        disp = [r for r in rs
                if r.asset_out == asset and r.type in ("SELL", "SWAP")]
        if not disp:
            continue  # still-held-only assets keep their existing stub/backfill
        acq_units = sum((r.qty_in or Decimal(0)) for r in acq)
        acq_cost = sum(((r.value_gbp or Decimal(0)) + (r.fee_gbp or Decimal(0))
                        for r in acq), Decimal(0))
        disposed = sum((r.qty_out or Decimal(0)) for r in disp)
        resid_units = pools.get(asset, (Decimal(0), Decimal(0)))[0]
        # never leave a sub-unit rounding shortfall: pool must cover disposals
        open_units = max(acq_units, disposed + resid_units)
        first_disp = min(r.dt for r in disp)
        open_dt = min((r.dt for r in acq), default=first_disp)
        open_dt = min(open_dt, first_disp - timedelta(days=1))
        if open_units > 0:
            out.append(Row(
                idx=0, dt=open_dt, type="OPENING", asset_out="", qty_out=None,
                asset_in=asset, qty_in=open_units, value_gbp=acq_cost, method="",
                fee_gbp=None, fee_orig="", wallet="Coinbase",
                txid=f"COINBASE:OPENING-{asset}", income_taxed=None,
                notes=(f"Coinbase pre-{since.date()} pool: {len(acq)} "
                       f"acquisition(s) (buys/earn/swap-in) collapsed; basis "
                       f"reconstructed from full Coinbase history"),
                warning="", raw=None))
        for r in disp:
            kind = ("crypto-to-crypto swap = disposal at market value"
                    if r.type == "SWAP" else "sale")
            out.append(Row(
                idx=0, dt=r.dt, type="SELL", asset_out=asset, qty_out=r.qty_out,
                asset_in="", qty_in=None, value_gbp=r.value_gbp, method="",
                fee_gbp=r.fee_gbp, fee_orig=r.fee_orig, wallet="Coinbase",
                txid=r.txid, income_taxed=None,
                notes=f"Coinbase historic disposal ({kind})", warning="",
                raw=None))
    return out


# ---------------------------------------------------------------------------
# Report / write
# ---------------------------------------------------------------------------

def _acq_summary(rows: list[Row], asset: str, verbose: bool = False) -> str:
    acqs = [r for r in rows
            if r.type in ("BUY", "REWARD", "SWAP") and r.asset_in == asset]
    if not acqs:
        return "no acquisitions found"
    total_q = sum((r.qty_in for r in acqs), Decimal(0))
    total_c = sum(((r.value_gbp or Decimal(0)) + (r.fee_gbp or Decimal(0))
                   for r in acqs), Decimal(0))
    head = (f"{len(acqs)} acquisition(s), {total_q.normalize():f} units, "
            f"£{total_c:.2f}, {date_str(acqs[0].dt)} → {date_str(acqs[-1].dt)}")
    if not verbose:
        return head
    parts = [f"{r.qty_in.normalize():f} on {date_str(r.dt)} "
             f"(£{(r.value_gbp or Decimal(0)) + (r.fee_gbp or Decimal(0)):.2f})"
             for r in acqs]
    return head + ": " + "; ".join(parts)


def _disp_summary(rows: list[Row], asset: str) -> str:
    disps = [r for r in rows
             if r.type in ("SELL", "SWAP") and r.asset_out == asset]
    if not disps:
        return "no disposals"
    total = sum((r.qty_out for r in disps), Decimal(0))
    return (f"{len(disps)} disposal(s), {total.normalize():f} units, "
            f"{date_str(disps[0].dt)} → {date_str(disps[-1].dt)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--asset", help="limit to one asset symbol, e.g. BTC")
    ap.add_argument("--write", action="store_true",
                    help="fill blank NEEDS-INPUT stub Values in the sheet")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --write: print intended cell writes, write nothing")
    args = ap.parse_args()

    ty_start = datetime.strptime(
        os.environ.get("CGT_TAX_YEAR_START", "2026-04-06"), "%Y-%m-%d"
    ).replace(tzinfo=UTC)

    all_rows = fetch_coinbase_rows()
    if args.asset:
        a = args.asset.upper()
        all_rows = [r for r in all_rows if a in (r.asset_in, r.asset_out)]

    # Sanity check: account is supposed to be historical-only.
    post = [r for r in all_rows if r.dt >= ty_start]
    if post:
        print("\n" + "=" * 70)
        print(f"NOTE: {len(post)} Coinbase transaction(s) ON/AFTER "
              f"{ty_start.date()} — these are handled by the daily cgt_ledger.py "
              f"sync (fetch_coinbase_rows), not by this backfill tool:")
        for r in post:
            print(f"  {date_str(r.dt)} {r.type} {r.asset_out or r.asset_in} "
                  f"{r.qty_out or r.qty_in} ({r.txid})")
        print("=" * 70 + "\n")
    hist = [r for r in all_rows if r.dt < ty_start]
    print(f"\n{len(hist)} pre-{ty_start.date()} Coinbase transactions mapped")

    for i, r in enumerate(hist):
        r.idx = i
    _, pools = cgt_ledger.compute_cgt(hist)
    pools = {a: (u, c) for a, (u, c) in pools.items() if u > 0 or c > 0}

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    tab = os.environ.get("CGT_SHEET_TAB", "CGT Ledger")
    ledger_rows, _ = cgt_ledger.read_ledger(sheet_id, tab)
    stubs = {r.asset_in: r for r in ledger_rows
             if r.txid.startswith("MANUAL:NEEDS-INPUT-")}
    if args.asset:
        stubs = {a: r for a, r in stubs.items() if a == args.asset.upper()}

    print(f"{len(stubs)} NEEDS-INPUT stub(s) in '{tab}': "
          f"{', '.join(sorted(stubs)) or '—'}\n")

    writes: list[tuple[Row, Decimal, str]] = []
    for asset in sorted(set(pools) | set(stubs)):
        units, cost = pools.get(asset, (Decimal(0), Decimal(0)))
        stub = stubs.get(asset)
        print(f"── {asset} " + "─" * (60 - len(asset)))
        print(f"  Coinbase pool at {ty_start.date()}: {units.normalize():f} units, "
              f"cost £{cost:.2f}"
              + (f" (£{cost / units:.4f}/unit)" if units else ""))
        print(f"  {_acq_summary(hist, asset)}")
        print(f"  {_disp_summary(hist, asset)}")

        if stub is None:
            print("  ⚠ no NEEDS-INPUT stub for this asset — if these coins are "
                  "still held elsewhere, check the ledger covers them.\n")
            continue
        if not units:
            print("  ⚠ stub exists but no Coinbase pool reconstructed — cost "
                  "basis must come from somewhere else.\n")
            continue

        needed = stub.qty_in or Decimal(0)
        tol = max(cgt_ledger.RECONCILE_TOLERANCE, needed * Decimal("0.005"))
        pro_rata = cost * needed / units if units else Decimal(0)
        print(f"  Stub needs {needed.normalize():f} units "
              f"(sheet row {stub.idx}, Value "
              f"{'BLANK' if stub.value_gbp is None else f'already £{stub.value_gbp}'})")

        if abs(units - needed) <= tol:
            verdict, value = "MATCH", cost
        elif units > needed:
            verdict, value = ("MATCH (pro-rata; excess likely withdrawal fees "
                              "or coins elsewhere)", pro_rata)
        else:
            verdict, value = (f"MISMATCH — Coinbase only explains "
                              f"{units.normalize():f} of {needed.normalize():f}", None)
        print(f"  Verdict: {verdict}")
        if value is not None:
            print(f"  → opening Value (GBP): £{value:.2f}")
            if stub.value_gbp is None:
                writes.append((stub, value, asset))
            elif abs(stub.value_gbp - value) > Decimal("0.01"):
                print(f"  (Value already filled £{stub.value_gbp} — row is "
                      f"user-owned, never auto-edited; update G{stub.idx} "
                      f"manually if you prefer the computed £{value:.2f})")
        elif args.write:
            print("  (skip write: mismatch — enter Value manually)")
        print()

    if not args.write:
        if writes:
            print(f"{len(writes)} stub(s) fillable — rerun with --write, or "
                  f"transcribe the values above (row becomes yours once filled).")
        return

    for stub, value, asset in writes:
        n = len([r for r in hist if r.type in ("BUY", "REWARD", "SWAP")
                 and r.asset_in == asset])
        method = f"Coinbase history backfill ({n} acquisitions before {ty_start.date()})"
        notes = (stub.notes + " | " if stub.notes else "") + \
            f"[coinbase backfill] {_acq_summary(hist, asset, verbose=True)}"
        if args.dry_run:
            print(f"DRY-RUN would write {asset}: G{stub.idx}=£{value:.2f}, "
                  f"H{stub.idx}='{method}', N{stub.idx}=notes({len(notes)} chars)")
            continue
        sheets.write_range(sheet_id, tab, f"G{stub.idx}:H{stub.idx}",
                           [[float(round(value, 2)), method]], raw=True)
        sheets.update_cell(sheet_id, tab, f"N{stub.idx}", notes)
        print(f"WROTE {asset}: G{stub.idx}=£{value:.2f} (+method, notes)")
    if writes and not args.dry_run:
        print("\nNow run: python3 src/cgt_ledger.py --recompute-only")


if __name__ == "__main__":
    main()
