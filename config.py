"""
Configuration for Raffay cron 62 — DCF / price-target gap fill.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

_PACKAGE_DIR = Path(__file__).resolve().parent

# --- Google Sheet ---
SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs/edit"
)
SHEET_ID = "1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs"

# Production writes the Missing Value tab (tickers AC, values AD–AI).
WORKSHEET_MISSING_VALUE = "Missing Value"
WORKSHEET_MISSING_VALUE_GID = 714988304
# Legacy tab — not used by cron 62 production run
WORKSHEET_DCF_SOURCE = "DCF Source"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDENTIALS_PRIMARY = "/usr/local/share/keys.json"

# --- Scraping ---
# Server: use xvfb-run (see scripts/run_production.sh). GuruFocus needs a real display path.
HEADLESS = _env_bool("HEADLESS", True)
# When True, batches that include GuruFocus run Chromium with headless=False (required on Linux).
GURUFOCUS_USE_HEADED = _env_bool("GURUFOCUS_USE_HEADED", True)
BATCH_SIZE = 50
DELAY_BETWEEN_TICKERS_SEC = (3, 7)
DELAY_BETWEEN_BATCHES_SEC = 15
SCRAPE_RETRIES = 3
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)

# Treat these as empty / needs refill
MISSING_PLACEHOLDERS = frozenset({
    "",
    "N/A",
    "n/a",
    "NA",
    "No matching rec",
    "No matching records found",
    "0",
    "0.0",
    "0.00",
    "0%",
    "0.00%",
    "-",
    "—",
})

SourceKey = Literal[
    "alphaspread",
    "valueinvesting",
    "gurufocus",
    "price_target_low",
    "price_target_avg",
    "price_target_high",
]


@dataclass(frozen=True)
class SheetColumnBlock:
    """One ticker + value pair on the DCF Source tab."""

    source: SourceKey
    index_col: str
    ticker_col: str
    value_col: str
    label: str


# DCF Source layout (from workbook screenshots)
DCF_SOURCE_BLOCKS: tuple[SheetColumnBlock, ...] = (
    SheetColumnBlock("alphaspread", "A", "B", "C", "Alpha Spread"),
    SheetColumnBlock("valueinvesting", "E", "F", "G", "ValueIo"),
    SheetColumnBlock("gurufocus", "I", "J", "K", "GuruFocus"),
    SheetColumnBlock("price_target_low", "M", "N", "O", "Price Target Low"),
    SheetColumnBlock("price_target_avg", "M", "N", "P", "Price Target Average"),
    SheetColumnBlock("price_target_high", "M", "N", "Q", "Price Target High"),
)

# Missing Value tab layout (row 1 = summary, row 2 = headers, data from row 3)
MISSING_VALUE_START_ROW = 3
MISSING_VALUE_INDEX_COL = "AB"  # e.g. Nasdaq → Alpha Spread URL segment
MISSING_VALUE_TICKER_COL = "AC"
MISSING_VALUE_COLUMNS: dict[SourceKey, str] = {
    "alphaspread": "AD",
    "valueinvesting": "AE",
    "gurufocus": "AF",
    "price_target_low": "AG",
    "price_target_avg": "AH",
    "price_target_high": "AI",
}
MISSING_VALUE_SOURCE_LABELS: dict[SourceKey, str] = {
    "alphaspread": "Alpha Spread",
    "valueinvesting": "ValueIo",
    "gurufocus": "GuruFocus",
    "price_target_low": "Price Target Low",
    "price_target_avg": "Price Target Average",
    "price_target_high": "Price Target High",
}
# Order used when scanning rows (one ticker row → up to six cells)
MISSING_VALUE_SOURCE_ORDER: tuple[SourceKey, ...] = (
    "alphaspread",
    "valueinvesting",
    "gurufocus",
    "price_target_low",
    "price_target_avg",
    "price_target_high",
)

# --- Source URLs ---
ALPHA_SPREAD_URL = (
    "https://www.alphaspread.com/security/{security_type}/{ticker}/dcf-valuation/base-case"
)
VALUE_INVESTING_URL = "https://valueinvesting.io/{ticker}/valuation/dcf-growth-exit-5y"
# Required GuruFocus entry point for DCF / Fair Value (see scrapers/gurufocus.py)
GURUFOCUS_CALCULATOR_URL = "https://www.gurufocus.com/dcf-calculator"
GURUFOCUS_DCF_URL = f"{GURUFOCUS_CALCULATOR_URL}?ticker={{ticker}}"
# Legacy alternate page — not used by cron 62
GURUFOCUS_STOCK_DCF_URL = "https://www.gurufocus.com/stock/{ticker}/dcf"
# DCF calculator is client-rendered; Fair Value can take 30–90s without login
GURUFOCUS_FAIR_VALUE_POLL_ATTEMPTS = 45
GURUFOCUS_FAIR_VALUE_POLL_INTERVAL_MS = 2000
GURUFOCUS_PAGE_SETTLE_MS = 5000
GURUFOCUS_WAIT_FAIR_VALUE_TIMEOUT_MS = 90_000
GURUFOCUS_CAPTURE_POST_LOAD_MS = 12_000
# Optional: path to Playwright storage_state JSON after manual GuruFocus login
GURUFOCUS_STORAGE_STATE = _PACKAGE_DIR / "gurufocus_auth.json"

# --- Extraction (JSON keys + regex); see docs/FLOW.md ---
# Substrings to prefer when filtering network JSON (empty = capture all JSON responses)
JSON_URL_HINTS_ALPHASPREAD = ("alphaspread", "dcf", "valuation")
JSON_URL_HINTS_VALUEINVESTING = ("valueinvesting", "valuation", "dcf", "fair")
JSON_URL_HINTS_GURUFOCUS = ("gurufocus", "dcf", "stock", "api")

# Keys searched in captured JSON trees (case-insensitive, underscores ignored)
DCF_JSON_KEY_HINTS: tuple[str, ...] = (
    "dcf",
    "dcfvalue",
    "fairvalue",
    "fairvaluepershare",
    "intrinsicvalue",
    "intrinsic",
    "ivdcearning",
    "valuation",
    "estimatedvalue",
    "valuepershare",
)

PRICE_TARGET_JSON_KEY_HINTS: dict[str, tuple[str, ...]] = {
    "low": ("targetlow", "lowtarget", "pricetargetlow", "lowesttarget"),
    "avg": ("targetmean", "targetaverage", "meantarget", "pricetarget", "consensus"),
    "high": ("targethigh", "hightarget", "pricetargethigh", "highesttarget"),
}

DCF_TEXT_REGEX_PATTERNS: tuple[str, ...] = (
    r"Discounted Cash Flow[^$\d]{0,80}\$?\s*([\d,]+\.?\d*)",
    r"DCF\s*(?:Value)?[\s:]*\$?\s*([\d,]+\.?\d*)",
    r"Fair\s*Value[\s:]*\$?\s*([\d,]+\.?\d*)",
    r"Intrinsic\s*Value[\s:]*\$?\s*([\d,]+\.?\d*)",
    r"\$\s*(-?[\d,]+\.?\d*)\s*(?:USD|per share)?",
)

PRICE_TARGET_TEXT_REGEX: dict[str, tuple[str, ...]] = {
    "low": (
        r"Price\s*Target\s*Low[\s:]*\$?\s*([\d,]+\.?\d*)",
        r"Low[\s:]*\$?\s*([\d,]+\.?\d*)",
    ),
    "avg": (
        r"Price\s*Target\s*(?:Average|Mean)?[\s:]*\$?\s*([\d,]+\.?\d*)",
        r"Average\s*Target[\s:]*\$?\s*([\d,]+\.?\d*)",
        r"Consensus[\s:]*\$?\s*([\d,]+\.?\d*)",
    ),
    "high": (
        r"Price\s*Target\s*High[\s:]*\$?\s*([\d,]+\.?\d*)",
        r"High[\s:]*\$?\s*([\d,]+\.?\d*)",
    ),
}

# Save HTML + JSON under logs/captures/ when extraction fails
SAVE_DEBUG_ON_FAILURE = True
DEBUG_CAPTURES_DIR = _PACKAGE_DIR / "logs" / "captures"

# JSON-only testing (scripts/capture_json.py) — no Google Sheets required
CAPTURE_TEST_OUTPUT_DIR = _PACKAGE_DIR / "logs" / "capture-test"
DEFAULT_SECURITY_TYPE = "nasdaq"
PRICE_TARGET_URL_TEMPLATE = "https://finance.yahoo.com/quote/{ticker}/analysis/"

# Playwright settings for capture-json test script
CAPTURE_TEST_WAIT_UNTIL = "domcontentloaded"
CAPTURE_TEST_TIMEOUT_MS = 120_000
CAPTURE_TEST_POST_LOAD_WAIT_MS = 3000
