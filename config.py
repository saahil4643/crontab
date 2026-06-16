from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


_PACKAGE_DIR = Path(__file__).resolve().parent


def _bootstrap_env() -> None:
    """Load .env before reading configuration defaults (inline to avoid import cycles)."""
    env_path = _PACKAGE_DIR / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            cleaned = cleaned[1:-1]
        os.environ[key] = cleaned


_bootstrap_env()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    return int(raw)


def _require_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. Set it in .env before running."
        )
    return value


# --- Google Sheets ---

SHEET_ID = _env("SHEET_ID")
_sheet_url = _env("SHEET_URL")
SHEET_URL = _sheet_url or (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit" if SHEET_ID else ""
)
WORKSHEET_MISSING_VALUE = _env("WORKSHEET_NAME", "Missing Value")
MISSING_VALUE_START_ROW = _env_int("SHEET_START_ROW", 3)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDENTIALS_PRIMARY = _env("GOOGLE_CREDENTIALS_PATH", "/usr/local/share/keys.json")

# --- Scrape source URLs (override per deployment if needed) ---

ALPHA_SPREAD_URL = _env(
    "ALPHA_SPREAD_URL_TEMPLATE",
    "https://www.alphaspread.com/security/{security_type}/{ticker}/dcf-valuation/base-case",
)
VALUE_INVESTING_URL = _env(
    "VALUE_INVESTING_URL_TEMPLATE",
    "https://valueinvesting.io/{ticker}/valuation/dcf-growth-exit-5y",
)
VALUE_INVESTING_VALUATION_URL = _env(
    "VALUE_INVESTING_VALUATION_URL_TEMPLATE",
    "https://valueinvesting.io/{ticker}/valuation/{path}",
)
GURUFOCUS_EARNINGS_TERM_URL = _env(
    "GURUFOCUS_URL_TEMPLATE",
    "https://www.gurufocus.com/term/intrinsic-value-dcf-earnings-based/{ticker}",
)
YAHOO_QUOTE_URL = _env(
    "YAHOO_QUOTE_URL_TEMPLATE",
    "https://finance.yahoo.com/quote/{ticker}/",
)
YAHOO_CRUMB_URL = _env(
    "YAHOO_CRUMB_URL",
    "https://query1.finance.yahoo.com/v1/test/getcrumb",
)
YAHOO_QUOTE_SUMMARY_URL = _env(
    "YAHOO_QUOTE_SUMMARY_URL_TEMPLATE",
    "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
)

_paths_raw = _env("VALUE_INVESTING_PATHS", "dcf-growth-exit-5y,dcf-ebitda-exit-5y,dcf")
VALUE_INVESTING_PATHS: tuple[str, ...] = tuple(
    p.strip() for p in _paths_raw.split(",") if p.strip()
)

USER_AGENT = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)
PLAYWRIGHT_PROXY = _env("PLAYWRIGHT_PROXY", "http://127.0.0.1:10809")

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


_DEFAULT_SHEET_COLUMNS = (
    "alphaspread:B:C:D:Alpha Spread|"
    "valueinvesting:G:H:I:ValueIo|"
    "gurufocus:M:N:O:GuruFocus|"
    "price_target_low:R:S:T:Price Target Low|"
    "price_target_avg:R:S:U:Price Target Average|"
    "price_target_high:R:S:V:Price Target High"
)


def _parse_sheet_columns(raw: str) -> tuple[SheetColumnBlock, ...]:
    """
    Parse SHEET_COLUMNS env var.

    Format: source:index:ticker:value:label blocks separated by |.
    Example: alphaspread:B:C:D:Alpha Spread|valueinvesting:G:H:I:ValueIo
    """
    blocks: list[SheetColumnBlock] = []
    for chunk in raw.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) != 5:
            raise ValueError(
                f"Invalid SHEET_COLUMNS block {chunk!r}; "
                "expected source:index:ticker:value:label"
            )
        source, index_col, ticker_col, value_col, label = parts
        blocks.append(
            SheetColumnBlock(
                source=source,  # type: ignore[arg-type]
                index_col=index_col,
                ticker_col=ticker_col,
                value_col=value_col,
                label=label,
            )
        )
    if not blocks:
        raise ValueError("SHEET_COLUMNS parsed to zero blocks")
    return tuple(blocks)


MISSING_VALUE_BLOCKS: tuple[SheetColumnBlock, ...] = _parse_sheet_columns(
    _env("SHEET_COLUMNS", _DEFAULT_SHEET_COLUMNS)
)

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

DEFAULT_SECURITY_TYPE = _env("DEFAULT_SECURITY_TYPE", "nasdaq")


def validate_config() -> None:
    """Fail fast when required sheet settings are missing."""
    _require_env("SHEET_ID")
    if not SHEET_URL:
        raise RuntimeError("SHEET_URL could not be derived; set SHEET_ID or SHEET_URL in .env")
