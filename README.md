# Resilient Sheet Collector

Fills missing valuation cells in a Google Sheet using Playwright scraping.

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
VALUEINVESTING_TABLE_WAIT_MS=35000

# Scheduling
RESILIENT_SCHEDULER=round_robin   # or legacy_batch
RESILIENT_CHUNK_SIZE=15
RESILIENT_ALPHASPREAD_CHUNK_SIZE=1
SHEET_WRITE_BATCH_SIZE=15

# Pacing (shorter with round-robin; Alpha Spread stays at 8s isolated browsers)
RESILIENT_VALUEINVESTING_SLEEP_SECONDS=10
RESILIENT_VALUEINVESTING_SLEEP_JITTER_SECONDS=5
RESILIENT_VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS=15
RESILIENT_SLEEP_BASELINE_SECONDS=4
RESILIENT_ALPHASPREAD_SLEEP_SECONDS=8
RESILIENT_ALPHASPREAD_JOB_TIMEOUT_SECONDS=120
RESILIENT_ALPHASPREAD_RETRY_BASE_SECONDS=480
RESILIENT_ALPHASPREAD_RETRY_CAP_SECONDS=4500
RESILIENT_ALPHASPREAD_RETRY_MAX_ATTEMPTS=5
RESILIENT_ALPHASPREAD_BREAKER_THRESHOLD=3
RESILIENT_ALPHASPREAD_BREAKER_WINDOW_SECONDS=600
RESILIENT_ALPHASPREAD_BREAKER_COOLDOWN_SECONDS=2700
RESILIENT_GURUFOCUS_SLEEP_SECONDS=20
RESILIENT_YAHOO_SLEEP_SECONDS=3
RESILIENT_GURUFOCUS_VIEWS_PER_IDENTITY=15
RESILIENT_GURUFOCUS_POST_ROTATION_SLEEP_SECONDS=15
RESILIENT_GURUFOCUS_WARMUP_WAIT_MS=4000
RESILIENT_GURUFOCUS_HEADLESS=0

# GuruFocus login (recommended; keeps session in .gurufocus-storage-state.json)
GURUFOCUS_EMAIL=your_email@example.com
GURUFOCUS_PASSWORD=your_password
GURUFOCUS_LOGIN_URL=https://www.gurufocus.com/login
GURUFOCUS_STORAGE_STATE=.gurufocus-storage-state.json

RESILIENT_HEADLESS=1
RESILIENT_USE_XVFB=1
RESILIENT_PROXY_MODE=direct
```

GuruFocus is Cloudflare-protected. Prefer `RESILIENT_GURUFOCUS_HEADLESS=0` (headed) plus login credentials. Optional `auto` tries headless after a saved `.gurufocus-storage-state.json`, then escalates to headed on 403 — in practice Cloudflare often still blocks headless. Never commit `.env` or `.gurufocus-storage-state.json`.

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

Default scheduler is **round-robin chunks**: up to `RESILIENT_CHUNK_SIZE` jobs per source (AS → VI → GF → Yahoo), then switch. **Alpha Spread is different:** `RESILIENT_ALPHASPREAD_CHUNK_SIZE` defaults to **1**, and each ticker gets a **new browser** plus an **~8s** pause with light jitter (`RESILIENT_ALPHASPREAD_SLEEP_SECONDS`). Reusing one page for 15 Alpha Spread tickers caused HTTP 403 after the first success. A 403/timeout is retried later in the same run (exponential backoff, max 5 attempts). Three 403s in 10 minutes trip a circuit breaker that pauses **only** Alpha Spread (~45 min) while other sources continue. Each Alpha Spread job has a 120s hang watchdog. Yahoo low/avg/high for the same ticker share one page load.

Successful cell updates are buffered and sent to Google Sheets in batches of `SHEET_WRITE_BATCH_SIZE` (default 15). Remaining cells flush when the run ends.

Set `RESILIENT_SCHEDULER=legacy_batch` to restore the old “drain one site fully” order.

If a primary site scrape fails, the cell is left empty (no Google search fallback).
