"""ValueInvesting.io (ValueIo) DCF scraper — JSON capture → regex → CSS fallback."""

from __future__ import annotations

import logging
from typing import Optional

from playwright.sync_api import Page

from config import JSON_URL_HINTS_VALUEINVESTING, VALUE_INVESTING_URL
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def _value_investing_css(page: Page) -> Optional[str]:
    for selector in (
        "[data-testid='fair-value']",
        ".fair-value",
        "h2:has-text('Fair Value')",
    ):
        loc = page.locator(selector).first
        if loc.count() > 0:
            try:
                return loc.inner_text(timeout=5000)
            except Exception:
                continue
    return None


class ValueInvestingScraper(BaseScraper):
    source_key = "valueinvesting"
    source_label = "ValueIo"

    def scrape(self, page: Page, ticker: str) -> Optional[str]:
        url = VALUE_INVESTING_URL.format(ticker=ticker)
        logger.info("[%s] %s", self.source_label, url)

        def pipeline(page: Page, url: str, ticker: str, attempt: int) -> Optional[object]:
            self.dismiss_overlays(page)
            return self._pipeline_dcf(
                page,
                url,
                ticker,
                json_url_substrings=JSON_URL_HINTS_VALUEINVESTING,
                css_fallback=_value_investing_css,
            )

        return self._run_with_retries(page, ticker, pipeline, url)
