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
supported and always preserved — the script only ever adds new rows
(columns A–N, deduped by Txn ID) and rewrites the computed columns (O–T) plus
the summary block. Rows are kept physically sorted by date every run, so the
ledger reads grouped by tax year; input cells are rewritten verbatim (the one
exception: column A is normalised to a real dd/mm/yyyy Date cell).

Everything lives on the one `CGT Ledger` tab: data rows at the top, then a
blank spacer row, then a `CGT SUMMARY` block — labels down column A, one
column per tax year. The block includes the UK loss carry-forward chain:
current-year losses offset current-year gains first, brought-forward losses
then reduce net gains only down to the annual exempt amount, and the unused
remainder carries into the next year's column automatically. Losses from
years before the ledger started can be seeded with `CGT_LOSSES_BF` in `.env`.
New rows are inserted above the summary block and inherit the neighbouring
row's formatting — restyle columns however you like and the script follows.

### One-time setup

```bash
python src/cgt_ledger.py --setup   # creates the tab, headers, formatting
```

For a tab that already exists, `python src/cgt_ledger.py --migrate` refreshes
headers, formats (dd/mm/yyyy dates, £ money columns), highlight rules, header
notes, hides the Txn ID column, sorts the rows and writes the summary block.
It's idempotent — safe to re-run any time (rules are replaced, not stacked).

Opening pools for assets held before the tracked tax year start
(`CGT_TAX_YEAR_START` in `.env`) are handled automatically where possible:
every run compares actual T212/Kraken holdings against what the ledger
accounts for, and for anything under-tracked first tries to reconstruct the
opening Section 104 pool from full pre-tax-year API history (exact GBP costs
from T212 order fills; Kraken trades + rewards). Only if that fails — e.g.
crypto deposited from an external wallet, where no purchase record exists —
does it write a stub `OPENING` row with Value left blank; the Value cell is
highlighted until you fill in the real cost basis, and the Notes column says
what the backfill did or didn't find. Highlights are formula-driven, so they
clear the instant you type; the computed columns (O–T) refresh on the next
run (nightly 8:30am, or `--recompute-only` on demand).

The Txn ID column (L, hidden) is the sync's dedupe key — leave it blank on
manual rows, or use `MANUAL:<anything-unique>`.

For RSU vests: Type `REWARD`, Asset In/Qty In, Value = market value at vest,
and `Income Taxed` (column M) = the amount already taxed through payroll —
this becomes the acquisition cost basis. RSU sales are ordinary `SELL` rows.

Kraken API key needs "Query Funds", "Query Closed Orders & Trades" and
"Query Ledger Entries" permissions (all read-only) for the CGT ledger to pull
trades, staking rewards, and current balances.

Kraken ingestion is ledger-based: order-book trades come from TradesHistory,
but instant conversions (the Convert button — spend/receive ledger pairs),
crypto deposits and withdrawals only exist in the Ledgers endpoint, so both
are read. Crypto-to-crypto conversions are recorded as SWAP disposals valued
at the daily GBP close; deposits/withdrawals become TRANSFER rows (not
disposals — the Section 104 pool spans wallets, so moving coins doesn't
change cost basis, but externally deposited coins need an OPENING row for
the basis they arrived with).

### Running

```bash
python src/cgt_ledger.py                   # sync from APIs + recompute
python src/cgt_ledger.py --dry-run         # preview without writing
python src/cgt_ledger.py --recompute-only  # skip APIs, just recompute
python src/cgt_ledger.py --migrate         # refresh formatting/layout on an existing tab (idempotent)
```

Runs daily via cron at 8:30am (see `config/crontab.example`), separate from
the main snapshot job since T212's history endpoint is rate-limited and slow.

If the ledger tracks *more* of an asset than you actually hold (e.g. an
un-logged disposal, gift, or transfer to self-custody), that's never
auto-written to the sheet — only a console warning, since it's not always a
taxable event and shouldn't be guessed at.

Staking-reward and transfer rows are kept in the data — rewards carry pool
units/cost basis and take part in 30-day matching; transfers anchor dedupe
and reconciliation — but they're hidden from view by the tab's basic filter
(re-applied each run, bounded to the data rows so the summary block below is
never hidden; unhiding lasts until the next sync). Each tax year's total
reward income appears in the summary block for self-assessment.

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
