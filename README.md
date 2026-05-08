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
config/
  .env.example        # Credential template
  crontab.example     # Cron schedule
tests/                # Smoke tests for each integration
bootstrap_monzo.py    # One-time Monzo OAuth setup
```
