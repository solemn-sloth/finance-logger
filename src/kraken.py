"""
Kraken API client.

Fetches total account balance equivalent in GBP using the private
TradeBalance endpoint with HMAC-SHA512 request signing.

Auth: API Key + Private Key (base64-encoded secret).
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

_BASE = "https://api.kraken.com"


def _sign(path: str, data: dict, secret: str) -> str:
    """Return the HMAC-SHA512 signature for a private Kraken API request."""
    encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _private_post(endpoint: str, params: dict | None = None) -> dict:
    """Send a signed POST to a Kraken private endpoint and return the result dict."""
    api_key = os.environ["KRAKEN_API_KEY"]
    api_secret = os.environ["KRAKEN_PRIVATE_KEY"]
    path = f"/0/private/{endpoint}"
    data = {"nonce": str(int(time.time() * 1000))}
    if params:
        data.update(params)
    headers = {
        "API-Key": api_key,
        "API-Sign": _sign(path, data, api_secret),
    }
    resp = requests.post(_BASE + path, data=data, headers=headers, timeout=10)
    if not resp.ok:
        raise RuntimeError(f"Kraken {endpoint} failed: {resp.status_code} {resp.text}")
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken {endpoint} error: {body['error']}")
    return body["result"]


def get_total_balance() -> float:
    """Return total account value in GBP, valuing every holding at its current
    price — including staked/Earn assets.

    NB: TradeBalance's 'eb' (equivalent balance) covers only the *spot* wallet,
    so anything allocated to Kraken Earn/staking (e.g. ETH held as ETH2.S) is
    excluded and the total collapses to near-zero. We therefore sum the Balance
    endpoint (which does report staked variants) valued at live Ticker prices.
    Best-effort: an asset that can't be priced is warned about and skipped, not
    fatal, so one odd coin never zeroes the whole figure."""
    from decimal import Decimal

    by_asset: dict[str, Decimal] = {}
    for raw_asset, amount in _private_post("Balance").items():
        qty = Decimal(str(amount))
        if qty <= Decimal("0.00000001"):
            continue
        asset = normalize_asset(raw_asset)  # ETH2.S / ETH.F -> ETH, spot + staked merge
        by_asset[asset] = by_asset.get(asset, Decimal(0)) + qty

    total = Decimal(0)
    for asset, qty in by_asset.items():
        try:
            total += current_gbp_value(asset, qty)
        except Exception as exc:
            print(f"WARN: Kraken could not value {qty.normalize():f} {asset} in GBP, "
                  f"excluded from total: {exc}", file=sys.stderr)
    return round(float(total), 2)


# ---------------------------------------------------------------------------
# CGT ledger support: trade/reward history + GBP valuation via public OHLC.
# API key needs "Query Ledger Entries" permission for get_staking_rewards.
# ---------------------------------------------------------------------------

_ASSET_ALIASES = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XETC": "ETC",
    "XXDG": "DOGE", "XDG": "DOGE", "XXRP": "XRP", "XXLM": "XLM",
    "XLTC": "LTC", "XZEC": "ZEC", "XXMR": "XMR", "XMLN": "MLN", "XREP": "REP",
    "ZGBP": "GBP", "ZUSD": "USD", "ZEUR": "EUR", "ZCAD": "CAD",
    "ZJPY": "JPY", "ZAUD": "AUD", "ZCHF": "CHF",
}
FIAT = {"GBP", "USD", "EUR", "CAD", "JPY", "AUD", "CHF"}

_pairs_cache: tuple[dict, dict] | None = None
_ohlc_cache: dict = {}


def normalize_asset(asset: str) -> str:
    """Kraken asset code -> plain symbol (XXBT -> BTC, ETH2.S -> ETH, ZGBP -> GBP)."""
    base = asset.split(".")[0]
    if base == "ETH2":
        base = "ETH"
    return _ASSET_ALIASES.get(base, base)


_last_public_call = 0.0


def _public_get(endpoint: str, params: dict | None = None, max_retries: int = 5) -> dict:
    """GET a Kraken public endpoint (throttled ~1 req/s, retries on rate limit)."""
    global _last_public_call
    for attempt in range(max_retries):
        wait = 1.1 - (time.time() - _last_public_call)
        if wait > 0:
            time.sleep(wait)
        _last_public_call = time.time()
        resp = requests.get(f"{_BASE}/0/public/{endpoint}", params=params or {}, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Kraken public {endpoint} failed: {resp.status_code} {resp.text}")
        body = resp.json()
        errors = body.get("error") or []
        if any("Too many requests" in e for e in errors):
            time.sleep(5 * (attempt + 1))
            continue
        if errors:
            raise RuntimeError(f"Kraken public {endpoint} error: {errors}")
        return body["result"]
    raise RuntimeError(f"Kraken public {endpoint}: rate-limited after {max_retries} retries")


def _pairs() -> tuple[dict, dict]:
    """Return (pair-name -> (base, quote), (base, quote) -> OHLC altname). Cached."""
    global _pairs_cache
    if _pairs_cache is None:
        result = _public_get("AssetPairs")
        by_name: dict = {}
        by_assets: dict = {}
        for key, info in result.items():
            assets = (normalize_asset(info["base"]), normalize_asset(info["quote"]))
            alt = info.get("altname", key)
            by_name[key] = assets
            by_name[alt] = assets
            by_assets.setdefault(assets, alt)
        _pairs_cache = (by_name, by_assets)
    return _pairs_cache


def pair_assets(pair: str) -> tuple[str, str]:
    """Resolve a Kraken pair name to normalised (base, quote)."""
    assets = _pairs()[0].get(pair)
    if not assets:
        raise RuntimeError(f"Unknown Kraken pair: {pair}")
    return assets


def _daily_close(base: str, quote: str, day_ts: int):
    """Daily OHLC close for base/quote on the UTC day starting at day_ts (Decimal)."""
    from decimal import Decimal

    alt = _pairs()[1].get((base, quote))
    if not alt:
        raise RuntimeError(f"No Kraken pair for {base}/{quote}")
    key = (alt, day_ts)
    if key not in _ohlc_cache:
        result = _public_get("OHLC", {"pair": alt, "interval": 1440, "since": day_ts - 1})
        rows = next(v for k, v in result.items() if k != "last")
        if not rows or int(rows[0][0]) - day_ts >= 86400:
            raise RuntimeError(f"No OHLC candle for {alt} at {day_ts}")
        _ohlc_cache[key] = Decimal(str(rows[0][4]))
    return _ohlc_cache[key]


def _ticker_last(base: str, quote: str):
    """Current last-trade price for base/quote via the public Ticker (Decimal)."""
    from decimal import Decimal

    alt = _pairs()[1].get((base, quote))
    if not alt:
        raise RuntimeError(f"No Kraken pair for {base}/{quote}")
    result = _public_get("Ticker", {"pair": alt})
    row = next(iter(result.values()))
    return Decimal(str(row["c"][0]))  # 'c' = [last-trade price, lot volume]


def current_gbp_value(asset: str, amount):
    """Value `amount` of `asset` in GBP at the current market price (Decimal).
    Same GBP routing as get_gbp_value — direct pair, inverse, or via USD — but
    using live Ticker prices instead of a historical daily close."""
    from decimal import Decimal

    amount = Decimal(str(amount))
    if asset == "GBP":
        return amount
    by_assets = _pairs()[1]
    if (asset, "GBP") in by_assets:
        return amount * _ticker_last(asset, "GBP")
    if ("GBP", asset) in by_assets:
        return amount / _ticker_last("GBP", asset)
    if (asset, "USD") in by_assets and ("GBP", "USD") in by_assets:
        return amount * _ticker_last(asset, "USD") / _ticker_last("GBP", "USD")
    raise RuntimeError(f"No GBP valuation route for {asset}")


def get_gbp_value(asset: str, amount, on_date) -> tuple:
    """
    Value `amount` of `asset` in GBP on `on_date` (datetime.date) using Kraken
    daily OHLC closes. Returns (Decimal GBP value, method string for audit).
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    amount = Decimal(str(amount))
    if asset == "GBP":
        return amount, "GBP"
    day_ts = int(datetime(on_date.year, on_date.month, on_date.day, tzinfo=timezone.utc).timestamp())
    by_assets = _pairs()[1]
    if (asset, "GBP") in by_assets:
        px = _daily_close(asset, "GBP", day_ts)
        return amount * px, f"Kraken {asset}GBP close {px} ({on_date})"
    if ("GBP", asset) in by_assets:
        px = _daily_close("GBP", asset, day_ts)
        return amount / px, f"Kraken GBP{asset} close {px} ({on_date})"
    if (asset, "USD") in by_assets and ("GBP", "USD") in by_assets:
        usd = _daily_close(asset, "USD", day_ts)
        gbpusd = _daily_close("GBP", "USD", day_ts)
        return amount * usd / gbpusd, (
            f"Kraken {asset}USD close {usd} / GBPUSD {gbpusd} ({on_date})"
        )
    raise RuntimeError(f"No GBP valuation route for {asset}")


def _paginated_private(endpoint: str, result_key: str, since_ts: int) -> list[dict]:
    """Fetch all entries of a paginated private history endpoint since since_ts."""
    entries = []
    ofs = 0
    while True:
        result = _private_post(endpoint, {"start": since_ts, "ofs": ofs})
        page = result.get(result_key, {})
        if not page:
            break
        for entry_id, entry in page.items():
            entry = dict(entry)
            entry["id"] = entry_id
            entries.append(entry)
        ofs += len(page)
        if ofs >= int(result.get("count", 0)):
            break
        time.sleep(3)  # stay under the private API rate counter
    entries.sort(key=lambda e: float(e["time"]))
    return entries


def get_balances() -> dict:
    """
    Current non-zero balances, normalized asset code -> Decimal quantity.
    Staked variants (ETH2, .S/.M/.F suffixes) collapse into their base asset.
    Fiat currencies and dust (< 1e-8) are excluded.
    """
    from decimal import Decimal

    balances: dict = {}
    for raw_asset, amount in _private_post("Balance").items():
        asset = normalize_asset(raw_asset)
        if asset in FIAT:
            continue
        qty = Decimal(str(amount))
        if qty <= Decimal("0.00000001"):
            continue
        balances[asset] = balances.get(asset, Decimal(0)) + qty
    return balances


def get_trades_history(since_ts: int) -> list[dict]:
    """All spot trades since unix ts. Each: id, pair, time, type, price, cost, fee, vol."""
    return _paginated_private("TradesHistory", "trades", since_ts)


def get_ledger_entries(since_ts: int) -> list[dict]:
    """
    Full ledger history since unix ts, all entry types. This is the only place
    instant conversions ("spend"/"receive" pairs), deposits and withdrawals
    appear — TradesHistory covers order-book trades only.
    """
    return _paginated_private("Ledgers", "ledger", since_ts)


def filter_reward_entries(entries: list[dict]) -> list[dict]:
    """
    Staking/Earn reward entries from a ledger-entry list (positive amounts only).
    Excludes allocation/deallocation transfers between spot and Earn wallets.
    """
    rewards = []
    for entry in entries:
        etype = entry.get("type", "")
        subtype = entry.get("subtype", "") or ""
        amount = float(entry.get("amount", 0))
        if amount <= 0:
            continue
        if etype == "staking" and subtype in ("", "reward"):
            rewards.append(entry)
        elif etype == "earn" and subtype == "reward":
            rewards.append(entry)
    return rewards


def get_staking_rewards(since_ts: int) -> list[dict]:
    """Staking/Earn reward ledger entries since unix ts."""
    return filter_reward_entries(get_ledger_entries(since_ts))
