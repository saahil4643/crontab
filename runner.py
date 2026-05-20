"""Orchestrate missing-cell discovery, Playwright scraping, and sheet updates."""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

from config import (
    BATCH_SIZE,
    DELAY_BETWEEN_BATCHES_SEC,
    DELAY_BETWEEN_TICKERS_SEC,
    GURUFOCUS_STORAGE_STATE,
    GURUFOCUS_USE_HEADED,
    HEADLESS,
    SourceKey,
)
from models import ScrapeJob
from scrapers import SCRAPERS_BY_SOURCE
from scrapers.base import PlaywrightSession
from scrapers.extract import format_for_sheet
from sheet_client import DcfSheetClient

logger = logging.getLogger(__name__)


def _delay_between_tickers() -> None:
    lo, hi = DELAY_BETWEEN_TICKERS_SEC
    time.sleep(random.uniform(lo, hi))


def _batch_headless(batch: list[ScrapeJob], *, force_headed: bool) -> bool:
    """GuruFocus DCF pages often do not populate under headless Chrome on Linux."""
    if force_headed:
        return False
    if GURUFOCUS_USE_HEADED and any(j.source == "gurufocus" for j in batch):
        return False
    return HEADLESS


def scrape_job(session: PlaywrightSession, job: ScrapeJob) -> Optional[str]:
    scraper = SCRAPERS_BY_SOURCE.get(job.source)
    if scraper is None:
        logger.error("No scraper registered for source %r", job.source)
        return None
    page = session.new_page()
    try:
        if job.source == "alphaspread":
            return scraper.scrape(page, job.ticker, security_type=job.security_type)
        return scraper.scrape(page, job.ticker)
    finally:
        page.close()


def run(
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
    sources: Optional[set[SourceKey]] = None,
    force_headed: bool = False,
) -> None:
    client = DcfSheetClient()
    logger.info("Worksheets: %s", client.list_worksheets())

    jobs = client.collect_missing_jobs()
    if sources:
        jobs = [j for j in jobs if j.source in sources]
    if limit is not None:
        jobs = jobs[:limit]

    if not jobs:
        logger.info("Nothing to do.")
        return

    logger.info("Processing %d jobs (dry_run=%s)", len(jobs), dry_run)

    for batch_start in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[batch_start : batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        logger.info("=== Batch %d (%d jobs) ===", batch_num, len(batch))

        if dry_run:
            for job in batch:
                logger.info(
                    "DRY RUN row=%d %s %s col=%s index=%s",
                    job.row,
                    job.source,
                    job.ticker,
                    job.value_col,
                    job.security_type,
                )
            continue

        headless = _batch_headless(batch, force_headed=force_headed)
        logger.info("Playwright headless=%s for this batch", headless)

        with PlaywrightSession(
            headless=headless,
            storage_state=GURUFOCUS_STORAGE_STATE,
        ) as session:
            for job in batch:
                logger.info(
                    "Processing row %d %s (%s) col %s",
                    job.row,
                    job.ticker,
                    job.label,
                    job.value_col,
                )
                try:
                    raw = scrape_job(session, job)
                    if raw is not None:
                        value = format_for_sheet(raw)
                        client.write_value(job, value)
                    else:
                        logger.warning("No value for %s %s", job.ticker, job.label)
                except Exception as exc:
                    logger.exception("Failed job %s: %s", job.ticker, exc)
                _delay_between_tickers()

        if batch_start + BATCH_SIZE < len(jobs):
            logger.info("Batch cooldown %ss", DELAY_BETWEEN_BATCHES_SEC)
            time.sleep(DELAY_BETWEEN_BATCHES_SEC)

    logger.info("Cron 62 run finished")
