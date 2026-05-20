# Raffay Cron 62 — System Flow & Developer Guide

This document explains **what the cron does**, **how data moves through the code**, and **where to change things** when a website or spreadsheet layout changes. It is written for humans and for AI assistants working in this repository.

---

## 1. Purpose

**Cron 62** fills **missing** financial values on the Google Sheet **Missing Value** tab:

| Source site | Index | Ticker | Value column | Sheet header |
|-------------|-------|--------|--------------|--------------|
| Alpha Spread | B | C | D | Alpha Spread |
| ValueInvesting.io | G | H | I | ValueIo |
| GuruFocus | M | N | O | GuruFocus |
| Price targets | R | S | T / U / V | Low / Avg / High |

- **Schedule (business):** about every 2 weeks  
- **Spreadsheet:** [Daily Position Bullish Trader](https://docs.google.com/spreadsheets/d/1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs/edit)  
- **Behavior:** data from row 3; each source has its own ticker block; fill **D**, **I**, **O**, and **T–V** when empty or placeholder (`N/A`, `0.00%`, etc.)  
- **Production:** `xvfb-run -a python run.py` (virtual display for GuruFocus on Linux)

---

## 2. High-level flow

```text
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   run.py    │────▶│    runner.py     │────▶│  sheet_client.py    │
│  (CLI args) │     │ batches + timing │     │ find missing cells  │
└─────────────┘     └────────┬─────────┘     └──────────┬──────────┘
                             │                            │
                             │         list[ScrapeJob]    │
                             ▼                            │
                    ┌──────────────────┐                  │
                    │ PlaywrightSession│                  │
                    │  (one per batch) │                  │
                    └────────┬─────────┘                  │
                             │                            │
              per job        ▼                            │
                    ┌──────────────────┐                  │
                    │ scrapers/*.py    │                  │
                    │  goto URL        │                  │
                    │  capture JSON    │                  │
                    │  extract value   │                  │
                    └────────┬─────────┘                  │
                             │                            │
                             │  normalized string         │
                             ▼                            ▼
                    ┌──────────────────┐     ┌─────────────────────┐
                    │ extract.py       │     │ sheet_client.py     │
                    │ format_for_sheet │────▶│ update_cell         │
                    └──────────────────┘     └─────────────────────┘
```

---

## 3. Extraction pipeline (per ticker, per site)

Every site scraper uses the **same order** (see `scrapers/extract.py` → `run_extraction_pipeline`):

| Step | Method | What happens |
|------|--------|----------------|
| **1** | `json` | Playwright records **network JSON** responses while the page loads. Code walks JSON trees looking for keys like `dcf`, `fairValue`, `intrinsicValue` (see `DCF_JSON_KEY_HINTS` in `config.py`). |
| **2** | `regex_text` | If JSON fails, read **visible body text** and apply regex patterns (`DCF_TEXT_REGEX_PATTERNS`). |
| **3** | `css` | If still empty, run a **site-specific CSS fallback** (last resort; may break when UI changes). |

If all steps fail and `SAVE_DEBUG_ON_FAILURE` is true, artifacts are saved under `logs/captures/` (JSON files + HTML) for debugging.

### Why this order?

- **JSON** is usually more stable than CSS class names.  
- **Regex on text** works when the number is visible but not in a clean API.  
- **CSS** is kept from older scripts (e.g. step 53 GuruFocus) as a safety net.

---

## 4. File reference

### Entry & orchestration

| File | Role |
|------|------|
| `run.py` | CLI: `--dry-run`, `--limit N`, `--source gurufocus` (repeatable). |
| `runner.py` | Loads jobs, runs batches (`BATCH_SIZE`), opens Playwright, calls scrapers, writes sheet. |
| `logging_setup.py` | Logs to console + `logs/app.log`. |

### Configuration & credentials

| File | Role |
|------|------|
| `config.py` | Sheet URL/ID, column map, URLs, JSON key hints, regex patterns, delays, debug paths. **Start here when something global changes.** |
| `credentials.py` | Loads Google service account from `/usr/local/share/keys.json` or local `keys.json`. |

### Google Sheets

| File | Role |
|------|------|
| `sheet_client.py` | `collect_missing_jobs()`, `write_value()`. |
| `models.py` | `ScrapeJob` dataclass (row, ticker, source, column). |
| `sheet_utils.py` | Column letter ↔ index (`K` → 11). |

### Capture & extract (core scraping logic)

| File | Role |
|------|------|
| `scrapers/capture.py` | `NetworkCapture` — listen to `response` events, store JSON bodies; optional HTML snapshot. |
| `scrapers/extract.py` | `walk_json_for_value`, `extract_from_text`, `normalize_numeric_string`, `format_for_sheet`, `run_extraction_pipeline`. |
| `scrapers/base.py` | `PlaywrightSession`, `BaseScraper` with retries and `_pipeline_dcf` / `_pipeline_price_target`. |

### Per-site scrapers

| File | URL constant | Notes |
|------|--------------|--------|
| `scrapers/alpha_spread.py` | `ALPHA_SPREAD_URL` | Uses the source block's index column for the URL security type. |
| `scrapers/value_investing.py` | `VALUE_INVESTING_URL` | ValueIo column I. |
| `scrapers/gurufocus.py` | `GURUFOCUS_DCF_URL` → `https://www.gurufocus.com/stock/{ticker}/dcf` | **Fair Value** from stock DCF page DOM (not generic JSON). |
| `scrapers/price_target.py` | Yahoo analysis URL (placeholder) | Separate JSON/regex keys per low/avg/high. |

---

## 5. Data model: `ScrapeJob`

Defined in `models.py`:

```python
ScrapeJob(
    row=5,              # 1-based Google Sheet row
    ticker="GOOGL",
    source="gurufocus",
    value_col="K",
    value_col_index=11,
    label="GuruFocus",
)
```

One job = **one empty cell** to fill.

---

## 6. Configuration cheat sheet

Edit `config.py` when:

| Change | What to edit |
|--------|----------------|
| New spreadsheet | `SHEET_URL`, `SHEET_ID` |
| Tab renamed | `WORKSHEET_MISSING_VALUE` |
| Column moved | `MISSING_VALUE_BLOCKS` |
| New JSON field name on a site | `DCF_JSON_KEY_HINTS` or site-specific hints |
| Better text matching | `DCF_TEXT_REGEX_PATTERNS` |
| Slower / faster cron | `DELAY_*`, `BATCH_SIZE` |
| Turn off debug files | `SAVE_DEBUG_ON_FAILURE = False` |

---

## 7. How to run

```bash
cd raffay_cron-62_Daily-Position-Bullish-Trader-step
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# Share sheet with service account email from keys.json
python run.py --dry-run
python run.py --source gurufocus --limit 5
python run.py
```

### Prerequisites

1. **Google Sheet shared** with the service account (`client_email` in `keys.json`) as Editor.  
2. **Chromium** installed for Playwright.  
3. Tab name **Missing Value** must exist (or update `config.py`).

---

## 8. Debugging a failing site

1. Run one ticker: `python run.py --source gurufocus --limit 1`  
2. Check `logs/app.log` for `extracted via json|regex_text|css` or warnings.  
3. If failed, open `logs/captures/{source}_{TICKER}*`:
   - `*_json_0.json` — captured API payloads (inspect real key names)  
   - `*.html` — page at failure time  
   - `*.meta.json` — summary  
4. Add new key hints to `DCF_JSON_KEY_HINTS` or site `JSON_URL_HINTS_*` in `config.py`.  
5. Optionally improve `css_fallback` in that site’s scraper file.

---

## 9. Adding a new data source

1. Add a `SourceKey` in `config.py` and a `SheetColumnBlock` in `MISSING_VALUE_BLOCKS`.  
2. Create `scrapers/new_site.py` subclassing `BaseScraper`.  
3. Implement `scrape()` using `_run_with_retries` + `_pipeline_dcf`.  
4. Register in `scrapers/__init__.py` → `SCRAPERS_BY_SOURCE`.  
5. Document URL and columns in this file.

---

## 10. Cron schedule (server)

See `CRONTAB_EXAMPLE.txt`. Example:

```cron
0 2 */14 * * cd /path/to/raffay_cron-62_Daily-Position-Bullish-Trader-step && ./venv/bin/python run.py >> logs/cron.out 2>&1
```

---

## 11. Related projects in this repo

| Project | Relevance |
|---------|-----------|
| `cron 57 swing wagnew 28 Apr26 (2)` | Same **Sheets + loop** pattern; ValueInvesting via Google (different approach). |
| `scrap_earnings_dcf_step_53` | Original GuruFocus / AlphaSpread / ValueInvesting Playwright logic. |
| `Scraping-to-GoogleSheet-Undervalued_2_step_54` | Modular `web_site/` scrapers. |

Cron 62 combines **cron 57 operations** with **step 53/54 sites** and the **JSON-first extraction pipeline** described above.

---

## 12. Glossary

| Term | Meaning |
|------|---------|
| **DCF** | Discounted cash flow fair value (dollar amount). |
| **Missing cell** | Empty or listed in `MISSING_PLACEHOLDERS`. |
| **Job** | One ticker + one value column to fill. |
| **Batch** | Up to `BATCH_SIZE` jobs sharing one browser instance. |
| **Capture** | JSON (+ HTML) collected during `page.goto`. |

---

## 13. Testing JSON without Google Sheets

Use this **before** the client shares their spreadsheet or when tuning `DCF_JSON_KEY_HINTS`.

### Command

```bash
python scripts/capture_json.py --source gurufocus --ticker AAPL
# Prints live Fair Value from GuruFocus stock DCF DOM + saves JSON/HTML
python scripts/capture_json.py --source alphaspread --ticker MSFT --try-extract
python scripts/capture_json.py --list-sources
```

| Flag | Purpose |
|------|---------|
| `--source` | `alphaspread`, `valueinvesting`, `gurufocus`, `price_target` |
| `--ticker` | Symbol (default `AAPL`) |
| `--try-extract` | Print trial values from JSON keys + regex (no sheet write) |
| `--headed` | Visible browser |
| `--no-html` | Skip `.html` file |
| `--output-dir` | Default: `logs/capture-test/` |

### Output files

```text
logs/capture-test/
  gurufocus_AAPL.meta.json       # URLs + counts
  gurufocus_AAPL_json_0.json     # first API body
  gurufocus_AAPL_json_1.json     # ...
  gurufocus_AAPL.html            # optional full page
```

1. **GuruFocus:** use `python scripts/capture_json.py --source gurufocus --ticker AAPL` — look for **“Fair Value (live DOM)”** (from `/stock/{ticker}/dcf`, not JSON files).  
2. Other sites: open `*_json_*.json` and find DCF-related fields; add keys to `DCF_JSON_KEY_HINTS`.  
3. Re-run with `--try-extract` until extraction looks correct.

### Deploy phase (Google Sheets)

When JSON extraction looks good:

1. Place client service account JSON at `/usr/local/share/keys.json` or local `keys.json`.  
2. Share the workbook with `client_email` from that file.  
3. `python run.py --dry-run` — lists missing cells only.  
4. `python run.py --limit 5` — live scrape + write.

**`scripts/capture_json.py`** never touches Google APIs. **`run.py`** is for production.

---

*Last updated: JSON capture test script + DCF Source sheet `1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs`.*
