# Resilient Sheet Collector

Fills missing valuation cells in a Google Sheet using Playwright scraping with Google/Groq fallbacks.

## Server setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Create `.env` in the project root (not committed to git):

```bash
SHEET_ID=your_spreadsheet_id
GOOGLE_CREDENTIALS_PATH=/usr/local/share/keys.json
WORKSHEET_NAME=Missing Value
SHEET_START_ROW=3
GROQ_KEY_1=your_key
GROQ_KEY_2=your_key
RESILIENT_HEADLESS=1
RESILIENT_USE_XVFB=0
RESILIENT_PROXY_MODE=direct
```

Optional column override (`SHEET_COLUMNS`):

```bash
SHEET_COLUMNS=alphaspread:B:C:D:Alpha Spread|valueinvesting:G:H:I:ValueIo|gurufocus:M:N:O:GuruFocus|price_target_low:R:S:T:Price Target Low|price_target_avg:R:S:U:Price Target Average|price_target_high:R:S:V:Price Target High
```

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

Processes **missing cells only** for Alpha Spread, ValueInvesting, GuruFocus, and Yahoo price targets (low/avg/high). Failed jobs leave cells unchanged.

## Optional tuning (.env)

`RESILIENT_SLEEP_BASELINE_SECONDS=6` · `RESILIENT_GURUFOCUS_SLEEP_SECONDS=90` · `RESILIENT_VALUEINVESTING_SLEEP_SECONDS=15` · `RESILIENT_VALUEINVESTING_VIEWS_PER_IDENTITY=5`
