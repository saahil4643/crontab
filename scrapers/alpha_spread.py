"""Alpha Spread DCF scraper — JSON capture → regex → CSS fallback."""

from __future__ import annotations

import logging
from typing import Optional

from playwright.sync_api import Page

from config import ALPHA_SPREAD_URL, JSON_URL_HINTS_ALPHASPREAD
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def _alpha_spread_css(page: Page) -> Optional[str]:
    loc = page.locator(
        ".dcf-value-color.no-margin.valuation-scenario-value"
    ).first
    if loc.count() == 0:
        important = page.locator("tr.important td.right.aligned.restriction-sensitive-data").first
        if important.count() == 0:
            return None
        return important.inner_text(timeout=10_000)
    return loc.inner_text(timeout=10_000)


class AlphaSpreadScraper(BaseScraper):
    source_key = "alphaspread"
    source_label = "Alpha Spread"

    def scrape(
        self,
        page: Page,
        ticker: str,
        *,
        security_type: str | None = None,
    ) -> Optional[str]:
        security_type = (security_type or "nasdaq").strip().lower()
        url = ALPHA_SPREAD_URL.format(security_type=security_type, ticker=ticker)
        logger.info("[%s] %s", self.source_label, url)

        def pipeline(page: Page, url: str, ticker: str, attempt: int) -> Optional[object]:
            self.dismiss_overlays(page)
            return self._pipeline_dcf(
                page,
                url,
                ticker,
                json_url_substrings=JSON_URL_HINTS_ALPHASPREAD,
                css_fallback=_alpha_spread_css,
            )

        return self._run_with_retries(page, ticker, pipeline, url)
