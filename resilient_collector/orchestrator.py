from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import SourceKey, USER_AGENT
from logging_setup import setup_logging
from models import ScrapeJob
from resilient_collector.extractors import (
    extract_google_fallback_metric,
    extract_google_fallback_metric_for_job,
    extract_primary_metric,
    extract_primary_metric_for_job,
    extract_valueinvesting_metric,
)
from resilient_collector.valueinvesting_errors import ValueInvestingLimitError
from resilient_collector.valueinvesting_session import ValueInvestingSession
from resilient_collector.env_loader import load_env_file
from resilient_collector.proxy_pool import ProxyConfig, build_validated_pool
from scrapers.extract import format_for_sheet
from sheet_client import DcfSheetClient
from sheet_utils import has_stored_cell_value

logger = logging.getLogger(__name__)

BASELINE_SLEEP_SECONDS = int(os.environ.get("RESILIENT_SLEEP_BASELINE_SECONDS", "8"))
GURUFOCUS_SLEEP_SECONDS = int(os.environ.get("RESILIENT_GURUFOCUS_SLEEP_SECONDS", "90"))
# Pause between ValueInvesting tickers to reduce rate limits when reusing one browser.
VALUEINVESTING_SLEEP_SECONDS = int(os.environ.get("RESILIENT_VALUEINVESTING_SLEEP_SECONDS", "25"))
VALUEINVESTING_SLEEP_JITTER_SECONDS = int(
    os.environ.get("RESILIENT_VALUEINVESTING_SLEEP_JITTER_SECONDS", "10")
)
JITTER_SECONDS = int(os.environ.get("RESILIENT_SLEEP_JITTER_SECONDS", "0"))
SHEET_RETRY_ATTEMPTS = int(os.environ.get("SHEET_RETRY_ATTEMPTS", "6"))
SHEET_RETRY_BASE_SECONDS = float(os.environ.get("SHEET_RETRY_BASE_SECONDS", "2"))
DEFAULT_TICKERS = ("AAPL", "MSFT")
SHEET_SOURCES: set[SourceKey] = {
    "alphaspread",
    "valueinvesting",
    "gurufocus",
    "price_target_low",
    "price_target_avg",
    "price_target_high",
}


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def browser_headless() -> bool:
    return env_bool("RESILIENT_HEADLESS", False)


async def _run_page_fallback_chain(
    page: Page,
    job: ScrapeJob,
    proxy_label: str,
) -> TickerResult:
    """Google search fallback after primary scrape failure."""
    try:
        google_result = await extract_google_fallback_metric_for_job(page, job)
    except Exception as google_exc:
        logger.warning("[%s] Google fallback failed: %s", job.ticker, google_exc)
        return TickerResult(
            ticker=job.ticker,
            success=False,
            source="fallbacks",
            proxy=proxy_label,
            error=str(google_exc),
        )

    if google_result.metric:
        return TickerResult(
            ticker=job.ticker,
            success=True,
            source="google_fallback",
            metric=google_result.metric,
            proxy=proxy_label,
        )

    return TickerResult(
        ticker=job.ticker,
        success=False,
        source="fallbacks",
        proxy=proxy_label,
        error="Google fallback completed but no metric was found",
    )


@dataclass
class TickerResult:
    ticker: str
    success: bool
    source: str
    metric: str | None = None
    proxy: str | None = None
    error: str | None = None
    confidence: str | None = None


@dataclass
class SheetJobResult:
    job: ScrapeJob
    result: TickerResult | None
    written: bool = False


def compliance_sleep_seconds(source: SourceKey | None = None) -> int:
    if source == "gurufocus":
        return GURUFOCUS_SLEEP_SECONDS
    if source == "valueinvesting":
        if VALUEINVESTING_SLEEP_JITTER_SECONDS <= 0:
            return VALUEINVESTING_SLEEP_SECONDS
        low = max(0, VALUEINVESTING_SLEEP_SECONDS - VALUEINVESTING_SLEEP_JITTER_SECONDS)
        high = VALUEINVESTING_SLEEP_SECONDS + VALUEINVESTING_SLEEP_JITTER_SECONDS
        return random.randint(low, high)
    if JITTER_SECONDS <= 0:
        return BASELINE_SLEEP_SECONDS
    return random.randint(max(0, BASELINE_SLEEP_SECONDS - JITTER_SECONDS), BASELINE_SLEEP_SECONDS + JITTER_SECONDS)


async def compliance_sleep(source: SourceKey | None = None) -> None:
    delay = compliance_sleep_seconds(source)
    logger.info("[schedule] Post-request pause: %d seconds", delay)
    await asyncio.sleep(delay)


async def create_isolated_context(
    browser: Browser,
    proxy: ProxyConfig,
) -> BrowserContext:
    logger.info("[context] New isolated context via %s", proxy.label)
    context_kwargs = dict(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
    )
    proxy_config = proxy.playwright_config
    if proxy_config:
        context_kwargs["proxy"] = proxy_config
    return await browser.new_context(**context_kwargs)


async def apply_stealth(page: Page) -> None:
    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
        return
    except Exception:
        pass

    try:
        from playwright_stealth import stealth
        result = stealth(page)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("playwright_stealth unavailable or failed: %s", exc)


async def retry_sync(label: str, func, *args):
    last_error: Exception | None = None
    for attempt in range(1, SHEET_RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            last_error = exc
            if attempt >= SHEET_RETRY_ATTEMPTS:
                break
            delay = SHEET_RETRY_BASE_SECONDS * attempt
            logger.warning("%s failed attempt %d/%d: %s; retrying in %.1fs", label, attempt, SHEET_RETRY_ATTEMPTS, exc, delay)
            await asyncio.sleep(delay)
    raise last_error


async def teardown(page: Page | None, context: BrowserContext | None, browser: Browser | None, ticker: str) -> None:
    if page is not None:
        try:
            await page.close()
        except Exception as exc:
            logger.debug("[%s] page.close: %s", ticker, exc)

    if context is not None:
        try:
            await context.close()
            logger.info("[%s] Browser context closed", ticker)
        except Exception as exc:
            logger.debug("[%s] context.close: %s", ticker, exc)

    if browser is not None:
        try:
            await browser.close()
            logger.info("[%s] Browser instance closed", ticker)
        except Exception as exc:
            logger.debug("[%s] browser.close: %s", ticker, exc)


async def process_ticker(ticker: str, proxy_pool: list[ProxyConfig]) -> TickerResult:
    proxy = random.choice(proxy_pool)
    logger.info("[orchestrator] %s selected route %s", ticker, proxy.label)

    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=browser_headless())
            context = await create_isolated_context(browser, proxy)
            page = await context.new_page()
            await apply_stealth(page)

            try:
                metric = await extract_primary_metric(page, ticker)
                return TickerResult(
                    ticker=ticker,
                    success=True,
                    source="primary",
                    metric=metric,
                    proxy=proxy.label,
                )
            except Exception as primary_exc:
                logger.warning(
                    "[%s] Primary failed (%s); handing off to Google fallback",
                    ticker,
                    primary_exc,
                )
                try:
                    google_result = await extract_google_fallback_metric(page, ticker)
                except Exception as google_exc:
                    logger.warning("[%s] Google fallback failed: %s", ticker, google_exc)
                    google_result = None

                if google_result and google_result.metric:
                    return TickerResult(
                        ticker=ticker,
                        success=True,
                        source="google_fallback",
                        metric=google_result.metric,
                        proxy=proxy.label,
                    )

                return TickerResult(
                    ticker=ticker,
                    success=False,
                    source="fallbacks",
                    proxy=proxy.label,
                    error="Google fallback completed but no metric was found",
                )

    except Exception as exc:
        logger.error("[%s] Unrecoverable error: %s", ticker, exc)
        return TickerResult(
            ticker=ticker,
            success=False,
            source="runtime",
            proxy=proxy.label,
            error=str(exc),
        )

    finally:
        await teardown(page, context, browser, ticker)


async def process_job(job: ScrapeJob, proxy_pool: list[ProxyConfig]) -> TickerResult:
    proxy = random.choice(proxy_pool)
    logger.info(
        "[orchestrator] row=%d %s %s selected route %s",
        job.row,
        job.ticker,
        job.source,
        proxy.label,
    )

    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=browser_headless())
            context = await create_isolated_context(browser, proxy)
            page = await context.new_page()
            await apply_stealth(page)

            try:
                primary = await extract_primary_metric_for_job(page, job)
                return TickerResult(
                    ticker=job.ticker,
                    success=True,
                    source=f"primary_{primary.method}",
                    metric=primary.metric,
                    proxy=proxy.label,
                )
            except Exception as primary_exc:
                logger.warning(
                    "[%s] %s primary failed (%s); handing off to Google fallback",
                    job.ticker,
                    job.source,
                    primary_exc,
                )
                return await _run_page_fallback_chain(page, job, proxy.label)

    except Exception as exc:
        logger.error("[%s] %s unrecoverable error: %s", job.ticker, job.source, exc)
        return TickerResult(
            ticker=job.ticker,
            success=False,
            source="runtime",
            proxy=proxy.label,
            error=str(exc),
        )

    finally:
        await teardown(page, context, browser, job.ticker)


async def run_collector(tickers: list[str]) -> list[TickerResult]:
    load_env_file()
    proxy_pool = await build_validated_pool()
    results: list[TickerResult] = []

    for index, ticker in enumerate(tickers, start=1):
        logger.info(
            "[orchestrator] Processing %s (%d/%d)",
            ticker,
            index,
            len(tickers),
        )
        result = await process_ticker(ticker, proxy_pool)
        results.append(result)
        logger.info(
            "[result] ticker=%s success=%s source=%s metric=%s proxy=%s confidence=%s error=%s",
            result.ticker,
            result.success,
            result.source,
            result.metric,
            result.proxy,
            result.confidence,
            result.error,
        )
        if index < len(tickers):
            await compliance_sleep()

    return results


def _filter_sheet_jobs(
    jobs: list[ScrapeJob],
    sources: set[SourceKey] | None,
    limit: int | None,
) -> list[ScrapeJob]:
    selected_sources = sources or SHEET_SOURCES
    filtered = [job for job in jobs if job.source in selected_sources]
    if limit is not None:
        filtered = filtered[:limit]
    return filtered


def _partition_valueinvesting_jobs(jobs: list[ScrapeJob]) -> tuple[list[ScrapeJob], list[ScrapeJob]]:
    vi_jobs = [job for job in jobs if job.source == "valueinvesting"]
    other_jobs = [job for job in jobs if job.source != "valueinvesting"]
    return vi_jobs, other_jobs


async def _process_valueinvesting_with_session(
    job: ScrapeJob,
    session: ValueInvestingSession,
    *,
    allow_retry_on_limit: bool = True,
) -> TickerResult:
    proxy_label = session.proxy_label

    async def _scrape_once() -> TickerResult:
        page = await session.ensure_page()
        primary = await extract_valueinvesting_metric(page, job)
        session.record_view()
        return TickerResult(
            ticker=job.ticker,
            success=True,
            source=f"primary_{primary.method}",
            metric=primary.metric,
            proxy=proxy_label,
        )

    try:
        return await _scrape_once()
    except ValueInvestingLimitError as limit_exc:
        session.record_view()
        logger.warning(
            "[%s] ValueInvesting view limit (%s); rotating browser identity",
            job.ticker,
            limit_exc,
        )
        if not allow_retry_on_limit:
            session.record_view()
            page = await session.ensure_page()
            return await _run_page_fallback_chain(page, job, proxy_label)
        await session.rotate_identity()
        try:
            return await _scrape_once()
        except Exception as retry_exc:
            logger.warning(
                "[%s] valueinvesting retry failed (%s); trying fallbacks",
                job.ticker,
                retry_exc,
            )
            session.record_view()
            page = await session.ensure_page()
            return await _run_page_fallback_chain(page, job, proxy_label)
    except Exception as exc:
        logger.warning(
            "[%s] valueinvesting primary failed (%s); trying fallbacks",
            job.ticker,
            exc,
        )
        session.record_view()
        page = await session.ensure_page()
        return await _run_page_fallback_chain(page, job, proxy_label)


async def process_valueinvesting_jobs(
    jobs: list[ScrapeJob],
    proxy_pool: list[ProxyConfig],
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    """Scrape each VI job in a shared browser session; yield after each ticker for immediate sheet writes."""
    if not jobs:
        return

    session = ValueInvestingSession(proxy_pool)
    total = len(jobs)

    try:
        for index, job in enumerate(jobs, start=1):
            logger.info(
                "[sheet] Processing row=%d ticker=%s source=valueinvesting col=%s (VI %d/%d)",
                job.row,
                job.ticker,
                job.value_col,
                index,
                total,
            )
            result = await _process_valueinvesting_with_session(job, session)
            logger.info(
                "[result] ticker=%s success=%s source=%s metric=%s error=%s",
                result.ticker,
                result.success,
                result.source,
                result.metric,
                result.error,
            )
            yield job, result
            if index < total:
                await compliance_sleep("valueinvesting")  # anti-throttle between tickers
    finally:
        await session.close()


async def _clear_stale_cell(client: DcfSheetClient, job: ScrapeJob, *, reason: str) -> bool:
    if not has_stored_cell_value(job.current_value):
        logger.info(
            "[sheet] row=%d col=%s ticker=%s already empty (%s)",
            job.row,
            job.value_col,
            job.ticker,
            reason,
        )
        return False
    await retry_sync("clear_value", client.clear_value, job)
    logger.info(
        "[sheet] Cleared row=%d col=%s ticker=%s old=%r reason=%s",
        job.row,
        job.value_col,
        job.ticker,
        job.current_value,
        reason,
    )
    return True


async def _apply_clear_only_job(client: DcfSheetClient, job: ScrapeJob) -> SheetJobResult:
    index_hint = job.index_label or job.security_type
    cleared = await _clear_stale_cell(
        client,
        job,
        reason=f"non-US index {index_hint!r}",
    )
    return SheetJobResult(job=job, result=None, written=cleared)


async def _apply_job_result(
    client: DcfSheetClient,
    job: ScrapeJob,
    result: TickerResult,
) -> SheetJobResult:
    if job.clear_only:
        return await _apply_clear_only_job(client, job)

    written = False
    if result.success and result.metric:
        value = format_for_sheet(result.metric)
        if value.strip():
            await retry_sync("write_value", client.write_value, job, value)
            written = True
            logger.info(
                "[sheet] Wrote row=%d col=%s ticker=%s old=%r value=%s source=%s",
                job.row,
                job.value_col,
                job.ticker,
                job.current_value,
                value,
                result.source,
            )
        elif await _clear_stale_cell(client, job, reason="normalized scrape result empty"):
            written = True
        else:
            logger.warning(
                "[sheet] Left empty row=%d col=%s ticker=%s after normalized empty result",
                job.row,
                job.value_col,
                job.ticker,
            )
    elif await _clear_stale_cell(client, job, reason=result.error or "scrape failed"):
        written = True
    else:
        logger.info(
            "[sheet] Left empty row=%d col=%s ticker=%s after no valid value error=%s",
            job.row,
            job.value_col,
            job.ticker,
            result.error,
        )
    return SheetJobResult(job=job, result=result, written=written)


async def run_sheet_collector(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    sources: set[SourceKey] | None = None,
) -> list[SheetJobResult]:
    load_env_file()
    client = DcfSheetClient()
    jobs = _filter_sheet_jobs(await retry_sync("collect_jobs", client.collect_jobs), sources, limit)
    if not jobs:
        logger.info("[sheet] Nothing to process")
        return []

    clear_jobs = [job for job in jobs if job.clear_only]
    scrape_jobs = [job for job in jobs if not job.clear_only]
    logger.info(
        "[sheet] Processing %d jobs (%d scrape, %d non-US clear) dry_run=%s",
        len(jobs),
        len(scrape_jobs),
        len(clear_jobs),
        dry_run,
    )
    for job in jobs:
        logger.info(
            "[sheet] queued row=%d ticker=%s source=%s col=%s label=%s index=%r "
            "current=%r clear_only=%s",
            job.row,
            job.ticker,
            job.source,
            job.value_col,
            job.label,
            job.index_label,
            job.current_value,
            job.clear_only,
        )

    if dry_run:
        return [SheetJobResult(job=job, result=None, written=False) for job in jobs]

    proxy_pool = await build_validated_pool()
    results: list[SheetJobResult] = []
    job_cache: dict[tuple[str, SourceKey], TickerResult] = {}

    for job in clear_jobs:
        results.append(await _apply_clear_only_job(client, job))

    vi_jobs, other_jobs = _partition_valueinvesting_jobs(scrape_jobs)
    if vi_jobs:
        logger.info(
            "[sheet] ValueInvesting batch: %d jobs (shared browser, rotate identity every %s views)",
            len(vi_jobs),
            os.environ.get("RESILIENT_VALUEINVESTING_VIEWS_PER_IDENTITY", "5"),
        )
        async for job, result in process_valueinvesting_jobs(vi_jobs, proxy_pool):
            job_cache[(job.ticker, job.source)] = result
            results.append(await _apply_job_result(client, job, result))

    total_other = len(other_jobs)
    for index, job in enumerate(other_jobs, start=1):
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=%s col=%s (%d/%d)",
            job.row,
            job.ticker,
            job.source,
            job.value_col,
            index,
            total_other,
        )

        cache_key = (job.ticker, job.source)
        if cache_key in job_cache:
            result = job_cache[cache_key]
            logger.info("[sheet] Reusing cached result for %s %s", job.ticker, job.source)
        else:
            result = await process_job(job, proxy_pool)
            job_cache[cache_key] = result

        results.append(await _apply_job_result(client, job, result))

        if index < total_other:
            await compliance_sleep(job.source)

    return results


def _parse_tickers() -> list[str]:
    raw = os.environ.get("RESILIENT_TICKERS", "")
    if raw.strip():
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    return list(DEFAULT_TICKERS)


def _parse_sources(raw: list[str] | None) -> set[SourceKey] | None:
    if not raw:
        return None
    allowed: set[SourceKey] = SHEET_SOURCES
    selected: set[SourceKey] = set()
    for source in raw:
        if source not in allowed:
            raise argparse.ArgumentTypeError(
                f"Unknown source {source!r}. Choose from: {', '.join(sorted(allowed))}"
            )
        selected.add(source)
    return selected


def main() -> None:
    load_env_file()
    setup_logging(also_stdout=True)
    parser = argparse.ArgumentParser(
        description="Resilient async collector with direct scrape and Google search fallback",
    )
    parser.add_argument(
        "tickers",
        nargs="*",
        help="Stock tickers to process (default: AAPL MSFT or RESILIENT_TICKERS env)",
    )
    parser.add_argument(
        "--sheet",
        action="store_true",
        help="Refresh ticker jobs from the Missing Value sheet and write successful results back",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List sheet jobs without scraping or writing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max sheet jobs to process",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="SOURCE",
        help="Sheet source to process; repeatable",
    )
    args = parser.parse_args()

    if args.sheet:
        try:
            sources = _parse_sources(args.sources)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        logger.info("Starting resilient sheet collector")
        results = asyncio.run(
            run_sheet_collector(
                dry_run=args.dry_run,
                limit=args.limit,
                sources=sources,
            )
        )
        logger.info("=== Sheet Summary ===")
        for result in results:
            logger.info("%s", result)
        return

    tickers = [t.upper() for t in args.tickers] if args.tickers else _parse_tickers()
    logger.info("Starting resilient collector for tickers: %s", tickers)
    ticker_results = asyncio.run(run_collector(tickers))

    logger.info("=== Summary ===")
    for result in ticker_results:
        logger.info("%s", result)


if __name__ == "__main__":
    main()
