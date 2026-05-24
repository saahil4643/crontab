from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
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
    source_metric_label,
)
from resilient_collector.env_loader import load_env_file
from resilient_collector.groq_fallback import (
    extract_groq_direct_fallback_metric,
    extract_groq_fallback_metric,
    groq_direct_fallback_enabled,
)
from resilient_collector.proxy_pool import ProxyConfig, build_validated_pool
from scrapers.extract import format_for_sheet
from sheet_client import DcfSheetClient

logger = logging.getLogger(__name__)

BASELINE_SLEEP_SECONDS = int(os.environ.get("RESILIENT_SLEEP_BASELINE_SECONDS", "6"))
GURUFOCUS_SLEEP_SECONDS = int(os.environ.get("RESILIENT_GURUFOCUS_SLEEP_SECONDS", "90"))
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


def groq_requires_snippets() -> bool:
    raw = os.environ.get("RESILIENT_GROQ_REQUIRE_SNIPPETS")
    if raw is None:
        raw = os.environ.get("GROQ_REQUIRE_SNIPPETS")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _run_groq_fallback_chain(
    ticker: str,
    snippets: list[str],
    metric_label: str,
) -> TickerResult | None:
    """Second/third backup: Groq snippets, then optional direct Groq question."""
    if snippets:
        groq_result = await extract_groq_fallback_metric(
            ticker,
            snippets=snippets,
            metric_label=metric_label,
        )
        if groq_result.metric:
            return TickerResult(
                ticker=ticker,
                success=True,
                source="groq_fallback",
                metric=groq_result.metric,
                confidence=groq_result.confidence,
                error=groq_result.note,
            )

    if groq_direct_fallback_enabled():
        direct_result = await extract_groq_direct_fallback_metric(ticker, metric_label=metric_label)
        if direct_result.metric:
            return TickerResult(
                ticker=ticker,
                success=True,
                source="groq_direct_fallback",
                metric=direct_result.metric,
                confidence=direct_result.confidence,
                error=direct_result.note,
            )
        if not snippets:
            return TickerResult(
                ticker=ticker,
                success=False,
                source="fallbacks",
                error=direct_result.note or "Groq direct fallback returned no metric",
            )

    if groq_requires_snippets() and not snippets:
        return TickerResult(
            ticker=ticker,
            success=False,
            source="fallbacks",
            error="Google fallback found no snippets; Groq snippet parse skipped",
        )

    return None


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
                snippets: list[str] = []
                try:
                    google_result = await extract_google_fallback_metric(page, ticker)
                    snippets = google_result.snippets
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

                groq_result = await _run_groq_fallback_chain(
                    ticker,
                    snippets,
                    source_metric_label("gurufocus"),
                )
                if groq_result:
                    return TickerResult(
                        ticker=ticker,
                        success=groq_result.success,
                        source=groq_result.source,
                        metric=groq_result.metric,
                        proxy=proxy.label,
                        confidence=groq_result.confidence,
                        error=groq_result.error,
                    )

                return TickerResult(
                    ticker=ticker,
                    success=False,
                    source="fallbacks",
                    proxy=proxy.label,
                    error="Google and Groq fallbacks completed but no metric was found",
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
                snippets: list[str] = []
                try:
                    google_result = await extract_google_fallback_metric_for_job(page, job)
                    snippets = google_result.snippets
                except Exception as google_exc:
                    logger.warning("[%s] Google fallback failed: %s", job.ticker, google_exc)
                    google_result = None

                if google_result and google_result.metric:
                    return TickerResult(
                        ticker=job.ticker,
                        success=True,
                        source="google_fallback",
                        metric=google_result.metric,
                        proxy=proxy.label,
                    )

                groq_result = await _run_groq_fallback_chain(
                    job.ticker,
                    snippets,
                    source_metric_label(job.source),
                )
                if groq_result:
                    return TickerResult(
                        ticker=job.ticker,
                        success=groq_result.success,
                        source=groq_result.source,
                        metric=groq_result.metric,
                        proxy=proxy.label,
                        confidence=groq_result.confidence,
                        error=groq_result.error,
                    )

                return TickerResult(
                    ticker=job.ticker,
                    success=False,
                    source="fallbacks",
                    proxy=proxy.label,
                    error="Google and Groq fallbacks completed but no metric was found",
                )

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

    logger.info("[sheet] Processing %d resilient sheet jobs dry_run=%s", len(jobs), dry_run)
    for job in jobs:
        logger.info(
            "[sheet] queued row=%d ticker=%s source=%s col=%s label=%s current=%r",
            job.row,
            job.ticker,
            job.source,
            job.value_col,
            job.label,
            job.current_value,
        )

    if dry_run:
        return [SheetJobResult(job=job, result=None, written=False) for job in jobs]

    proxy_pool = await build_validated_pool()
    results: list[SheetJobResult] = []
    job_cache: dict[tuple[str, SourceKey], TickerResult] = {}

    for index, job in enumerate(jobs, start=1):
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=%s col=%s (%d/%d)",
            job.row,
            job.ticker,
            job.source,
            job.value_col,
            index,
            len(jobs),
        )

        cache_key = (job.ticker, job.source)
        if cache_key in job_cache:
            result = job_cache[cache_key]
            logger.info("[sheet] Reusing cached result for %s %s", job.ticker, job.source)
        else:
            result = await process_job(job, proxy_pool)
            job_cache[cache_key] = result

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
            else:
                logger.warning(
                    "[sheet] Left unchanged row=%d col=%s ticker=%s old=%r because result normalized empty",
                    job.row,
                    job.value_col,
                    job.ticker,
                    job.current_value,
                )
        else:
            logger.warning(
                "[sheet] Left unchanged row=%d col=%s ticker=%s old=%r after no valid value error=%s",
                job.row,
                job.value_col,
                job.ticker,
                job.current_value,
                result.error,
            )

        results.append(SheetJobResult(job=job, result=result, written=written))

        if index < len(jobs):
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
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Resilient async collector with direct scrape, Google snippets, and Groq parsing",
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
