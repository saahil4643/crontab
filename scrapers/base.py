"""Shared Playwright browser session and scraper base class."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from config import DEBUG_CAPTURES_DIR, HEADLESS, SCRAPE_RETRIES, USER_AGENT
from scrapers.extract import ExtractionResult, run_extraction_pipeline, run_price_target_pipeline

logger = logging.getLogger(__name__)


class PlaywrightSession:
    """
    Reusable sync Playwright browser for all scrapers in one cron run.
    Restart between batches in runner.py if needed.
    """

    def __init__(
        self,
        headless: bool = HEADLESS,
        storage_state: Optional[Path] = None,
    ) -> None:
        self._headless = headless
        self._storage_state = storage_state
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context_kwargs: dict = {
            "user_agent": USER_AGENT,
            "locale": "en-US",
            "viewport": {"width": 1920, "height": 1080},
        }
        if self._storage_state and self._storage_state.is_file():
            context_kwargs["storage_state"] = str(self._storage_state)
            logger.info("Using Playwright storage state: %s", self._storage_state)
        self._context = self._browser.new_context(**context_kwargs)
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        logger.info("Playwright browser started (headless=%s)", self._headless)

    def new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("Call start() before new_page()")
        return self._context.new_page()

    def stop(self) -> None:
        if self._context:
            self._context.close()
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        logger.info("Playwright browser stopped")

    def __enter__(self) -> PlaywrightSession:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()


class BaseScraper:
    """
    Each site scraper implements URL + pipeline options.
    Extraction order: network JSON → regex on text → CSS fallback.
    """

    source_key: str = "base"
    source_label: str = "base"

    def scrape(self, page: Page, ticker: str) -> Optional[str]:
        raise NotImplementedError

    def dismiss_overlays(self, page: Page) -> None:
        for label in ("Accept", "Accept All", "I agree", "Got it"):
            try:
                page.get_by_role("button", name=label).click(timeout=2000)
                return
            except Exception:
                continue

    def _debug_dir(self) -> Path:
        return DEBUG_CAPTURES_DIR

    def _run_with_retries(
        self,
        page: Page,
        ticker: str,
        pipeline_fn,
        url: str,
    ) -> Optional[str]:
        last_error: Optional[Exception] = None
        for attempt in range(1, SCRAPE_RETRIES + 1):
            try:
                result = pipeline_fn(page, url, ticker, attempt)
                if result is not None and hasattr(result, "value"):
                    return result.value
                if isinstance(result, str):
                    return result
                if attempt < SCRAPE_RETRIES:
                    logger.info(
                        "[%s] %s retry %d/%d",
                        self.source_label,
                        ticker,
                        attempt,
                        SCRAPE_RETRIES,
                    )
                    page.reload(wait_until="domcontentloaded")
            except Exception as exc:
                last_error = exc
                logger.error(
                    "[%s] %s attempt %d error: %s",
                    self.source_label,
                    ticker,
                    attempt,
                    exc,
                )
        if last_error:
            logger.debug("Last error for %s: %s", ticker, last_error)
        return None

    def _pipeline_dcf(
        self,
        page: Page,
        url: str,
        ticker: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 90_000,
        json_url_substrings: tuple[str, ...] = (),
        after_load=None,
        css_fallback=None,
    ) -> Optional[ExtractionResult]:
        return run_extraction_pipeline(
            page,
            url,
            source_key=self.source_key,
            ticker=ticker,
            source_label=self.source_label,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            json_url_substrings=json_url_substrings,
            css_fallback=css_fallback,
            after_load=after_load,
            debug_dir=self._debug_dir(),
        )

    def _pipeline_price_target(
        self,
        page: Page,
        url: str,
        ticker: str,
        kind: str,
        **kwargs,
    ) -> Optional[ExtractionResult]:
        return run_price_target_pipeline(
            page,
            url,
            source_key=self.source_key,
            ticker=ticker,
            source_label=self.source_label,
            kind=kind,
            debug_dir=self._debug_dir(),
            **kwargs,
        )
