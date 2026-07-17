# finance-logger

Daily snapshot job that pulls balances from personal finance accounts and writes them to a Google Sheet.

## What it does

Runs at 8am every day via cron and logs the following to fixed cells in a Google Sheet:

| Source | Data |
|--------|------|
| Monzo | Current account balance |
| Wise | GBP balance |
| Trading 212 ISA | Portfolio value |
| Trading 212 Invest | Portfolio value + profit |
| Barclaycard | Outstanding balance (parsed from forwarded email) |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp config/.env.example config/.env
chmod 600 config/.env
# Fill in all values — see comments in .env.example for where to find each one
```

### 3. Bootstrap Monzo OAuth

Monzo requires a one-time OAuth flow to get a refresh token:

```bash
python bootstrap_monzo.py
```

### 4. Set up Barclaycard email forwarding

Barclaycard has no public API, so the balance is parsed from statement emails.

1. In Outlook, create a rule: forward all messages from `*@barclaycard.co.uk` to your Gmail.
2. In Gmail, generate an app password: Account → Security → 2-Step Verification → App passwords. Create one named `finance-automation` and copy the 16-character string.
3. Set `IMAP_USER` and `IMAP_APP_PASSWORD` in `config/.env`.
4. Manually forward one existing Barclaycard email from Outlook to Gmail now — so the smoke test has something to find before the next statement arrives.
5. If the regex fails on the first real email, the full body is printed to stderr. Paste it into `src/barclaycard.py` to refine `BALANCE_REGEX`. Or set `BARCLAYCARD_DEBUG=1` in `.env` to print the body on every successful fetch too.

### 5. Set up the cron job

```bash
sudo mkdir -p /var/log/finance && sudo chown $USER /var/log/finance
crontab -e  # add the line from config/crontab.example
```

### 6. Smoke test each integration

```bash
python tests/test_monzo.py
python tests/test_wise.py
python tests/test_t212.py
python tests/test_sheets.py
python tests/test_barclaycard.py
```

## Running manually

```bash
python src/daily_snapshot.py
```

## CGT Ledger

`src/cgt_ledger.py` maintains a UK Capital Gains Tax ledger in the `CGT Ledger` tab
of the same Google Sheet, covering Trading 212 Invest (GIA) and Kraken crypto.
It auto-imports new transactions from both APIs and computes gains using UK
share-matching rules (same-day → 30-day bed & breakfast → Section 104 pooling).

Manually entered rows (RSUs, Raisin, anything without an API) are fully
supported and always preserved — the script only ever appends new rows
(columns A–N, deduped by Txn ID) and rewrites the computed columns (O–T) plus
the tax-year summary block.

### One-time setup

```bash
python src/cgt_ledger.py --setup   # creates the tab, headers, formatting
```

Then add an `OPENING` row per asset held before the tracked tax year starts
(`CGT_TAX_YEAR_START` in `.env`) — Type `OPENING`, Asset In + Qty In, Value =
pool cost in GBP, dated the day before the tax year start, Txn ID
`MANUAL:OPENING-<ASSET>`. Without this, gains on later disposals of
pre-existing holdings will be overstated.

For RSU vests: Type `REWARD`, Asset In/Qty In, Value = market value at vest,
and `Income Taxed` (column M) = the amount already taxed through payroll —
this becomes the acquisition cost basis. RSU sales are ordinary `SELL` rows.

Kraken API key needs "Query Funds", "Query Closed Orders & Trades" and
"Query Ledger Entries" permissions (all read-only) for the CGT ledger to pull
trades and staking rewards.

### Running

```bash
python src/cgt_ledger.py                   # sync from APIs + recompute
python src/cgt_ledger.py --dry-run         # preview without writing
python src/cgt_ledger.py --recompute-only  # skip APIs, just recompute
python src/cgt_ledger.py --sort            # also physically re-sort rows by date
```

Runs daily via cron at 8:30am (see `config/crontab.example`), separate from
the main snapshot job since T212's history endpoint is rate-limited and slow.

## Project structure

```
src/
  daily_snapshot.py   # Main orchestration job
  monzo.py            # Monzo API client (OAuth)
  wise.py             # Wise API client
  t212.py             # Trading 212 API client
  kraken.py           # Kraken API client
  barclaycard.py      # Barclaycard balance (Gmail IMAP scrape)
  sheets.py           # Google Sheets client
  cgt_ledger.py       # UK CGT ledger: sync + share-matching engine
config/
  .env.example        # Credential template
  crontab.example     # Cron schedule
tests/                # Smoke tests for each integration
  test_cgt_engine.py   # Unit tests for CGT share-matching rules
  test_cgt_sources.py  # Smoke test for CGT data sources
bootstrap_monzo.py    # One-time Monzo OAuth setup
```
