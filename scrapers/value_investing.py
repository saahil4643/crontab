"""ValueInvesting.io (ValueIo) DCF scraper."""

from __future__ import annotations

import logging
import re
from typing import Optional

from playwright.sync_api import Page

from config import USER_AGENT, VALUE_INVESTING_URL
from scrapers.base import BaseScraper
from scrapers.capture import NetworkCapture, PageCapture, save_debug_capture
from scrapers.extract import extract_from_json_payloads, normalize_numeric_string

logger = logging.getLogger(__name__)

_VALUEINVESTING_JSON_HINTS = (
    "dcfvaluation",
    "dcfvalue",
    "fairvalue",
    "fairprice",
    "intrinsicvalue",
    "estimatedvalue",
    "valuepershare",
)

_BLOCKED_PAGE_MARKERS = (
    "regional plan",
    "global plan",
    "start free trial",
    "create your account",
    "full access to",
    "per month, billed annually",
)

_VALUATION_TEXT_PATTERNS = (
    # Example: "The Discounted Cash Flow (DCF) valuation of Apple Inc (AAPL) is 199.39 USD"
    r"Discounted\s+Cash\s+Flow\s*\(DCF\)\s+valuation\b.*?\bis\s+(-?[\d,.]+)\s+USD\b",
    r"\bDCF\s+valuation\b.*?\bis\s+(-?[\d,.]+)\s+USD\b",
    r"\bFair\s+Value\b[\s:]*(-?[\d,.]+)\s+USD\b",
    r"(-?[\d,.]+)\s+USD\s+Fair\s+Value\b",
)


def _page_text(page: Page, capture: PageCapture) -> str:
    try:
        return page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return capture.html or ""


def _is_pricing_or_blocked_page(text: str) -> bool:
    lowered = text.lower()
    if "discounted cash flow" in lowered or "dcf valuation" in lowered:
        return False
    return any(marker in lowered for marker in _BLOCKED_PAGE_MARKERS)


def _extract_from_valueinvesting_text(text: str) -> Optional[str]:
    for pattern in _VALUATION_TEXT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        normalized = normalize_numeric_string(match.group(1))
        if normalized is not None:
            return normalized
    return None


def _extract_from_norm_elements(page: Page) -> Optional[str]:
    """Cron 53/54 pattern: ValueInvesting valuation widgets use .norm."""
    try:
        page.wait_for_selector(".norm", state="attached", timeout=15_000)
        norm_texts = [
            el.text_content(timeout=3000).strip()
            for el in page.locator(".norm").all()
            if el.text_content(timeout=3000)
        ]
    except Exception:
        return None

    # Cron 53/54 use index 2 as fair price; validate it is not a plan price.
    for index in (2, 1, 0):
        if len(norm_texts) <= index:
            continue
        candidate = norm_texts[index]
        if "%" in candidate:
            continue
        normalized = normalize_numeric_string(candidate.split()[0])
        if normalized is not None and normalized != "14.99":
            return normalized
    return None


class ValueInvestingScraper(BaseScraper):
    source_key = "valueinvesting"
    source_label = "ValueIo"

    def scrape(self, page: Page, ticker: str) -> Optional[str]:
        url = VALUE_INVESTING_URL.format(ticker=ticker)
        logger.info("[%s] %s", self.source_label, url)

        def pipeline(page: Page, url: str, ticker: str, attempt: int) -> Optional[object]:
            page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": USER_AGENT,
            })

            capture_handler = NetworkCapture(page)

            def navigate() -> None:
                page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                self.dismiss_overlays(page)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass

            capture = capture_handler.collect_during(navigate, post_load_wait_ms=3000)
            text = _page_text(page, capture)

            if _is_pricing_or_blocked_page(text):
                logger.warning("[%s] %s reached pricing/login page; skipping", self.source_label, ticker)
                save_debug_capture(capture, self._debug_dir(), source=self.source_key, ticker=ticker)
                return None

            json_value = extract_from_json_payloads(
                capture.json_payloads,
                _VALUEINVESTING_JSON_HINTS,
            )
            if json_value is not None and json_value != "14.99":
                logger.info(
                    "[%s] %s extracted via ValueInvesting JSON (%d payloads)",
                    self.source_label,
                    ticker,
                    len(capture.json_payloads),
                )
                return json_value

            norm_value = _extract_from_norm_elements(page)
            if norm_value is not None:
                logger.info("[%s] %s extracted via ValueInvesting .norm", self.source_label, ticker)
                return norm_value

            text_value = _extract_from_valueinvesting_text(text)
            if text_value is not None:
                logger.info("[%s] %s extracted via ValueInvesting text", self.source_label, ticker)
                return text_value

            save_debug_capture(capture, self._debug_dir(), source=self.source_key, ticker=ticker)
            logger.warning(
                "[%s] %s no ValueInvesting DCF value (json=%d)",
                self.source_label,
                ticker,
                len(capture.json_payloads),
            )
            return None

        return self._run_with_retries(page, ticker, pipeline, url)
