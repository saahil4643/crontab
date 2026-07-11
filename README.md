# Resilient Sheet Collector

Fills missing valuation cells in a Google Sheet using Playwright scraping with Google search fallback.

## Server setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Create `.env` in the project root:

```bash
SHEET_ID=your_spreadsheet_id
GOOGLE_CREDENTIALS_PATH=/usr/local/share/keys.json
WORKSHEET_NAME=Missing Value
SHEET_START_ROW=3
DIRECT_SCRAPE_TIMEOUT_MS=30000
SERP_TIMEOUT_MS=15000
VALUEINVESTING_TABLE_WAIT_MS=35000

# Pacing
RESILIENT_VALUEINVESTING_SLEEP_SECONDS=25
RESILIENT_VALUEINVESTING_SLEEP_JITTER_SECONDS=10
RESILIENT_VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS=20
RESILIENT_SLEEP_BASELINE_SECONDS=8
RESILIENT_GURUFOCUS_SLEEP_SECONDS=90
RESILIENT_GURUFOCUS_VIEWS_PER_IDENTITY=15
RESILIENT_GURUFOCUS_POST_ROTATION_SLEEP_SECONDS=30
RESILIENT_GURUFOCUS_WARMUP_WAIT_MS=4000
RESILIENT_GURUFOCUS_HEADLESS=0

RESILIENT_HEADLESS=1
RESILIENT_USE_XVFB=1
RESILIENT_PROXY_MODE=direct
```

Optional `SHEET_COLUMNS` if your sheet layout differs — see existing `.env` on Mac for format.

Share the spreadsheet with the service account email from `keys.json` (Editor access).

## Run

Test:

```bash
python run_resilient_sheet.py --dry-run --limit 3
```

Background (production):

```bash
python run_resilient_sheet.py --background
```

Watch logs:

```bash
tail -f logs/*worker*.log
```

Stop:

```bash
kill $(cat logs/resilient_sheet_run.pid)
```

Mac local dev: add `--no-xvfb` and point `GOOGLE_CREDENTIALS_PATH` at your local JSON file.

## Sources

Processes **missing cells only** for Alpha Spread, ValueInvesting, GuruFocus, and Yahoo price targets (low/avg/high).

All sources use Google search fallback when primary scrape fails.

Default timeouts and pacing are tuned for server reliability; override via `.env` if needed.
