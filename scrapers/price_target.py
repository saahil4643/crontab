"""Price target low / average / high — JSON → regex pipeline (URL TBD)."""

from __future__ import annotations

import logging
from typing import Literal, Optional

from playwright.sync_api import Page

from config import PRICE_TARGET_URL_TEMPLATE
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

PriceTargetKind = Literal["low", "avg", "high"]


class PriceTargetScraper(BaseScraper):
    """
    Fills O / P / Q on DCF Source (ticker column N).
    Uses same extraction pipeline with price-target-specific JSON keys and regex.
    """

    def __init__(self, kind: PriceTargetKind) -> None:
        self.kind = kind
        self.source_key = f"price_target_{kind}"
        self.source_label = f"Price Target {kind.upper()}"

    def scrape(self, page: Page, ticker: str) -> Optional[str]:
        url = PRICE_TARGET_URL_TEMPLATE.format(ticker=ticker)
        logger.info("[%s] %s (provider URL may need update)", self.source_label, url)

        def pipeline(page: Page, url: str, ticker: str, attempt: int) -> Optional[object]:
            self.dismiss_overlays(page)
            return self._pipeline_price_target(
                page,
                url,
                ticker,
                kind=self.kind,
                json_url_substrings=("yahoo", "quote", "analysis", "target"),
                wait_until="domcontentloaded",
                timeout_ms=90_000,
            )

        return self._run_with_retries(page, ticker, pipeline, url)
