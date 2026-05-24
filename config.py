from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


_PACKAGE_DIR = Path(__file__).resolve().parent

SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs/edit"
)
SHEET_ID = "1YwVEq6lNXbocbt4NwnBR1WGg0tD_RLVaR1HJaurkaCs"
WORKSHEET_MISSING_VALUE = "Missing Value"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDENTIALS_PRIMARY = "/usr/local/share/keys.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
PLAYWRIGHT_PROXY = os.environ.get("PLAYWRIGHT_PROXY", "http://127.0.0.1:10809")

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
    "--",
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
    source: SourceKey
    index_col: str
    ticker_col: str
    value_col: str
    label: str


MISSING_VALUE_START_ROW = 3
MISSING_VALUE_BLOCKS: tuple[SheetColumnBlock, ...] = (
    SheetColumnBlock("alphaspread", "B", "C", "D", "Alpha Spread"),
    SheetColumnBlock("valueinvesting", "G", "H", "I", "ValueIo"),
    SheetColumnBlock("gurufocus", "M", "N", "O", "GuruFocus"),
    SheetColumnBlock("price_target_low", "R", "S", "T", "Price Target Low"),
    SheetColumnBlock("price_target_avg", "R", "S", "U", "Price Target Average"),
    SheetColumnBlock("price_target_high", "R", "S", "V", "Price Target High"),
)

ALPHA_SPREAD_URL = "https://www.alphaspread.com/security/{security_type}/{ticker}/dcf-valuation/base-case"
VALUE_INVESTING_URL = "https://valueinvesting.io/{ticker}/valuation/dcf-growth-exit-5y"
VALUE_INVESTING_VALUATION_URL = "https://valueinvesting.io/{ticker}/valuation/{path}"
VALUE_INVESTING_PATHS: tuple[str, ...] = (
    "dcf-growth-exit-5y",
    "dcf-ebitda-exit-5y",
    "dcf",
)
GURUFOCUS_EARNINGS_TERM_URL = "https://www.gurufocus.com/term/intrinsic-value-dcf-earnings-based/{ticker}"

JSON_URL_HINTS_ALPHASPREAD = ("alphaspread", "dcf", "valuation")
JSON_URL_HINTS_VALUEINVESTING = ("valueinvesting", "valuation", "dcf", "fair")

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

DEFAULT_SECURITY_TYPE = "nasdaq"
