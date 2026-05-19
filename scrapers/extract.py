"""
Parse captured JSON / page text / CSS fallbacks into normalized sheet values.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from playwright.sync_api import Page

from config import (
    DCF_JSON_KEY_HINTS,
    DCF_TEXT_REGEX_PATTERNS,
    PRICE_TARGET_JSON_KEY_HINTS,
    PRICE_TARGET_TEXT_REGEX,
    SAVE_DEBUG_ON_FAILURE,
)
from scrapers.capture import NetworkCapture, PageCapture, save_debug_capture

logger = logging.getLogger(__name__)

# Max depth when walking arbitrary JSON trees
_JSON_WALK_MAX_DEPTH = 14


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of the extraction pipeline."""

    value: str
    method: str  # json | regex_text | css | none


def normalize_numeric_string(raw: str) -> Optional[str]:
    """
    Turn '$1,234.56', '(12.3)', etc. into a sheet-friendly number string.
    Returns None if not a valid number.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text or text in (".", "-", "-."):
        return None

    try:
        number = float(text)
    except ValueError:
        return None

    if negative:
        number = -abs(number)

    # Sheet: keep 2 decimals for fractional prices, else plain
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.2f}"


def format_for_sheet(value: str) -> str:
    """Final formatting before update_cell."""
    normalized = normalize_numeric_string(value)
    return normalized if normalized is not None else value.strip()


def _key_matches(key: str, hints: Sequence[str]) -> bool:
    key_lower = key.lower().replace("_", "").replace("-", "")
    return any(h in key_lower for h in hints)


def _coerce_number(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return normalize_numeric_string(str(value))
    if isinstance(value, str):
        return normalize_numeric_string(value)
    return None


def walk_json_for_value(
    obj: Any,
    key_hints: Sequence[str],
    *,
    depth: int = 0,
) -> Optional[str]:
    """Depth-first search for numeric values under keys matching hints."""
    if depth > _JSON_WALK_MAX_DEPTH:
        return None

    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(key, str) and _key_matches(key, key_hints):
                found = _coerce_number(val)
                if found is not None:
                    return found
            nested = walk_json_for_value(val, key_hints, depth=depth + 1)
            if nested is not None:
                return nested
        for val in obj.values():
            nested = walk_json_for_value(val, key_hints, depth=depth + 1)
            if nested is not None:
                return nested

    elif isinstance(obj, list):
        for item in obj:
            nested = walk_json_for_value(item, key_hints, depth=depth + 1)
            if nested is not None:
                return nested

    return None


def extract_from_json_payloads(
    payloads: Sequence[Any],
    key_hints: Sequence[str],
) -> Optional[str]:
    for payload in payloads:
        body = payload.body if hasattr(payload, "body") else payload
        found = walk_json_for_value(body, key_hints)
        if found is not None:
            return found
    return None


def extract_from_text(text: str, patterns: Sequence[str]) -> Optional[str]:
    if not text:
        return None
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            group = match.group(1) if match.lastindex else match.group(0)
            normalized = normalize_numeric_string(group)
            if normalized is not None:
                return normalized
    return None


def run_extraction_pipeline(
    page: Page,
    url: str,
    *,
    source_key: str,
    ticker: str,
    source_label: str,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 90_000,
    json_url_substrings: Sequence[str] = (),
    json_key_hints: Sequence[str] = DCF_JSON_KEY_HINTS,
    text_patterns: Sequence[str] = DCF_TEXT_REGEX_PATTERNS,
    css_fallback: Optional[Callable[[Page], Optional[str]]] = None,
    after_load: Optional[Callable[[Page], None]] = None,
    debug_dir: Optional[Any] = None,
) -> Optional[ExtractionResult]:
    """
    1. Navigate with NetworkCapture (JSON).
    2. Parse JSON by key hints.
    3. Regex on visible body text.
    4. Optional CSS callback.
    """
    # Capture all JSON responses on the page (see scrapers/capture.py)
    capture_handler = NetworkCapture(page)
    if json_url_substrings:
        logger.debug(
            "[%s] URL hints (for reference): %s",
            source_label,
            ", ".join(json_url_substrings),
        )

    def navigate() -> None:
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if after_load:
            after_load(page)

    capture: PageCapture = capture_handler.collect_during(navigate)

    # --- 1. JSON ---
    json_value = extract_from_json_payloads(capture.json_payloads, json_key_hints)
    if json_value is not None:
        logger.info("[%s] %s extracted via JSON (%d payloads)", source_label, ticker, len(capture.json_payloads))
        return ExtractionResult(value=json_value, method="json")

    # --- 2. Regex on visible text ---
    try:
        body_text = page.locator("body").inner_text(timeout=20_000)
    except Exception:
        body_text = capture.html or ""

    text_value = extract_from_text(body_text, text_patterns)
    if text_value is not None:
        logger.info("[%s] %s extracted via regex on page text", source_label, ticker)
        return ExtractionResult(value=text_value, method="regex_text")

    # --- 3. CSS fallback ---
    if css_fallback:
        css_raw = css_fallback(page)
        if css_raw is not None:
            normalized = normalize_numeric_string(css_raw)
            if normalized is not None:
                logger.info("[%s] %s extracted via CSS fallback", source_label, ticker)
                return ExtractionResult(value=normalized, method="css")

    if SAVE_DEBUG_ON_FAILURE and debug_dir is not None:
        save_debug_capture(capture, debug_dir, source=source_key, ticker=ticker)

    logger.warning("[%s] %s: no value (json=%d)", source_label, ticker, len(capture.json_payloads))
    return None


def run_price_target_pipeline(
    page: Page,
    url: str,
    *,
    source_key: str,
    ticker: str,
    source_label: str,
    kind: str,
    **kwargs: Any,
) -> Optional[ExtractionResult]:
    """Price targets use alternate JSON keys and regex patterns per kind."""
    hints = PRICE_TARGET_JSON_KEY_HINTS.get(kind, PRICE_TARGET_JSON_KEY_HINTS["avg"])
    patterns = PRICE_TARGET_TEXT_REGEX.get(kind, PRICE_TARGET_TEXT_REGEX["avg"])
    return run_extraction_pipeline(
        page,
        url,
        source_key=source_key,
        ticker=ticker,
        source_label=source_label,
        json_key_hints=hints,
        text_patterns=patterns,
        **kwargs,
    )
