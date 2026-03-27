# Finance Logging Automation

Daily snapshot job that pulls balances from personal finance accounts and writes them to a Google Sheet.

## What it does

Runs at 8am every day via cron and logs the following to fixed cells in a Google Sheet:

| Source | Data |
|--------|------|
| Monzo | Current account balance |
| Wise | GBP balance |
| Trading 212 ISA | Portfolio value |
| Trading 212 Invest | Portfolio value + profit |

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

### 4. Set up the cron job

```bash
sudo mkdir -p /var/log/finance && sudo chown $USER /var/log/finance
crontab -e  # add the line from config/crontab.example
```

### 5. Smoke test each integration

```bash
python tests/test_monzo.py
python tests/test_wise.py
python tests/test_t212.py
python tests/test_sheets.py
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
  sheets.py           # Google Sheets client
config/
  .env.example        # Credential template
  crontab.example     # Cron schedule
tests/                # Smoke tests for each integration
bootstrap_monzo.py    # One-time Monzo OAuth setup
```
