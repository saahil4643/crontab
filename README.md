# Raffay cron 62 — Daily Position Bullish Trader (DCF gap fill)

Bi-weekly Python cron that fills **missing** DCF and price-target values on the Google Sheet **Missing Value** tab (tickers **AC**, values **AD–AI**) using **Playwright** and a **JSON-first extraction pipeline**.

**Full documentation:** [docs/FLOW.md](docs/FLOW.md)

**Spreadsheet:** https://docs.google.com/spreadsheets/d/1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs/edit

## Server setup

```bash
cd /var/www/PersonalProjects/ElyScriptsCodeLive/modified/raffay_cron-62_Daily-Position-Bullish-Trader-step
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# Debian/Ubuntu: sudo apt install xvfb
```

Place credentials at **`/usr/local/share/keys.json`** and share the spreadsheet with the service account email (`client_email` in that file) as **Editor**.

## Production run

```bash
chmod +x scripts/run_production.sh
./scripts/run_production.sh --dry-run
./scripts/run_production.sh --source gurufocus --limit 5
./scripts/run_production.sh
```

`run_production.sh` uses **xvfb-run** so GuruFocus DCF calculator can run (headless Chrome alone often returns no Fair Value).

Environment overrides:

| Variable | Default | Meaning |
|----------|---------|---------|
| `GURUFOCUS_USE_HEADED` | `1` | Use non-headless browser when batch includes GuruFocus |
| `HEADLESS` | `1` | Headless for non-GuruFocus batches |

## Local / dev

```bash
python scripts/capture_json.py --source gurufocus --ticker AAPL --headed --try-extract
python run.py --dry-run          # needs keys.json or /usr/local/share/keys.json
python run.py --source gurufocus --limit 1 --headed
```

## Sheet layout (Missing Value)

| Column | Role |
|--------|------|
| AB | Index (e.g. Nasdaq) → Alpha Spread URL |
| AC | Ticker |
| AD–AI | Alpha Spread, ValueIo, GuruFocus, PT Low/Avg/High |

Data rows start at **row 3**.

## Extraction order (each site)

1. Network JSON during page load  
2. Regex on visible text  
3. CSS fallback  

Failed runs may save debug files under `logs/captures/`.
