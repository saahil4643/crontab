from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from pathlib import Path

from config import SourceKey, USER_AGENT
from logging_setup import setup_logging
from models import ScrapeJob
from resilient_collector.extractors import (
    extract_gurufocus_metric,
    extract_primary_metric,
    extract_primary_metric_for_job,
    extract_valueinvesting_metric,
    extract_yahoo_price_targets,
)
from resilient_collector.valueinvesting_errors import ValueInvestingLimitError
from resilient_collector.gurufocus_session import GuruFocusSession
from resilient_collector.valueinvesting_session import ValueInvestingSession
from resilient_collector.env_loader import load_env_file
from resilient_collector.progress_tracker import mark_completed
from resilient_collector.alphaspread_guard import AlphaSpreadGuard, apply_jitter
from resilient_collector.nav_retry import (
    interruptible_sleep_seconds,
    is_retryable_primary_failure,
    scrape_nav_retry_base_seconds,
)
from resilient_collector.proxy_pool import ProxyConfig, build_validated_pool
from resilient_collector.scheduler import (
    ROUND_ROBIN_ORDER,
    YahooTickerGroup,
    any_queue_nonempty,
    partition_scrape_queues,
    queue_remaining_summary,
    take_chunk,
)
from scrapers.extract import format_for_sheet
from sheet_client import DcfSheetClient
from sheet_utils import has_stored_cell_value
from sheet_write_buffer import SheetWriteBuffer
from worker_shutdown import ShutdownController, interruptible_sleep
from resilient_collector.ai_fallback import ai_is_configured, ai_primary_sources, fetch_ai_valuation_metric

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_PENDING_WRITES_PATH = _ROOT / "logs" / "pending_sheet_writes.jsonl"


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _scheduler_mode() -> str:
    raw = (os.environ.get("RESILIENT_SCHEDULER") or "round_robin").strip().lower()
    if raw in {"legacy", "legacy_batch", "batch"}:
        return "legacy_batch"
    return "round_robin"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _sleep_default(name: str, *, legacy: int, round_robin: int) -> int:
    """Explicit env wins; otherwise use shorter defaults in round_robin mode."""
    raw = os.environ.get(name)
    if raw is not None and str(raw).strip() != "":
        return int(raw)
    return round_robin if _scheduler_mode() == "round_robin" else legacy


SCHEDULER_MODE = _scheduler_mode()
CHUNK_SIZE = max(1, _env_int("RESILIENT_CHUNK_SIZE", 10))
# Alpha Spread locks after 1–2 views in one session; keep chunks at 1 by default.
ALPHASPREAD_CHUNK_SIZE = max(1, _env_int("RESILIENT_ALPHASPREAD_CHUNK_SIZE", 1))

BASELINE_SLEEP_SECONDS = _sleep_default(
    "RESILIENT_SLEEP_BASELINE_SECONDS", legacy=8, round_robin=4
)
# Proven Aug 13 pacing: 8s between isolated Alpha Spread browsers (not the 4s round-robin baseline).
ALPHASPREAD_SLEEP_SECONDS = _env_int("RESILIENT_ALPHASPREAD_SLEEP_SECONDS", 8)
ALPHASPREAD_JOB_TIMEOUT_SECONDS = _env_int("RESILIENT_ALPHASPREAD_JOB_TIMEOUT_SECONDS", 120)
GURUFOCUS_SLEEP_SECONDS = _sleep_default(
    "RESILIENT_GURUFOCUS_SLEEP_SECONDS", legacy=90, round_robin=20
)
VALUEINVESTING_SLEEP_SECONDS = _sleep_default(
    "RESILIENT_VALUEINVESTING_SLEEP_SECONDS", legacy=25, round_robin=10
)
VALUEINVESTING_SLEEP_JITTER_SECONDS = _sleep_default(
    "RESILIENT_VALUEINVESTING_SLEEP_JITTER_SECONDS", legacy=10, round_robin=5
)
YAHOO_SLEEP_SECONDS = _sleep_default(
    "RESILIENT_YAHOO_SLEEP_SECONDS", legacy=8, round_robin=3
)
JITTER_SECONDS = int(os.environ.get("RESILIENT_SLEEP_JITTER_SECONDS", "0"))

SHEET_RETRY_ATTEMPTS = int(os.environ.get("SHEET_RETRY_ATTEMPTS", "6"))
SHEET_RETRY_BASE_SECONDS = float(os.environ.get("SHEET_RETRY_BASE_SECONDS", "2"))
SCRAPE_PRIMARY_RETRY_ATTEMPTS = max(
    1, int(os.environ.get("RESILIENT_SCRAPE_PRIMARY_RETRY_ATTEMPTS", "2"))
)
VALUEINVESTING_FAILED_REPLAY = env_bool("RESILIENT_VALUEINVESTING_FAILED_REPLAY", True)
VALUEINVESTING_REPLAY_PAUSE_SECONDS = int(
    os.environ.get("RESILIENT_VALUEINVESTING_REPLAY_PAUSE_SECONDS", "60")
)
DEFAULT_TICKERS = ("AAPL", "MSFT")
SHEET_SOURCES: set[SourceKey] = {
    "alphaspread",
    "valueinvesting",
    "gurufocus",
    "price_target_low",
    "price_target_avg",
    "price_target_high",
}
_YAHOO_KIND_BY_SOURCE: dict[SourceKey, str] = {
    "price_target_low": "low",
    "price_target_avg": "avg",
    "price_target_high": "high",
}


def _jittered_seconds(base: int, fraction: float = 0.25) -> int:
    """Integer delay with ±fraction jitter; mean stays near *base*."""
    if base <= 0:
        return 0
    return max(1, int(round(apply_jitter(float(base), fraction))))


@dataclass
class BrowserHolder:
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None


def browser_headless() -> bool:
    return env_bool("RESILIENT_HEADLESS", False)


@dataclass
class TickerResult:
    ticker: str
    success: bool
    source: str
    metric: str | None = None
    proxy: str | None = None
    error: str | None = None
    confidence: str | None = None
    transient: bool = False


@dataclass
class SheetJobResult:
    job: ScrapeJob
    result: TickerResult | None
    written: bool = False


def compliance_sleep_seconds(source: SourceKey | None = None) -> int:
    if source == "alphaspread":
        return _jittered_seconds(ALPHASPREAD_SLEEP_SECONDS)
    if source == "gurufocus":
        return GURUFOCUS_SLEEP_SECONDS
    if source == "valueinvesting":
        if VALUEINVESTING_SLEEP_JITTER_SECONDS <= 0:
            return VALUEINVESTING_SLEEP_SECONDS
        low = max(0, VALUEINVESTING_SLEEP_SECONDS - VALUEINVESTING_SLEEP_JITTER_SECONDS)
        high = VALUEINVESTING_SLEEP_SECONDS + VALUEINVESTING_SLEEP_JITTER_SECONDS
        return random.randint(low, high)
    if source in _YAHOO_KIND_BY_SOURCE:
        return YAHOO_SLEEP_SECONDS
    if JITTER_SECONDS <= 0:
        return BASELINE_SLEEP_SECONDS
    return random.randint(max(0, BASELINE_SLEEP_SECONDS - JITTER_SECONDS), BASELINE_SLEEP_SECONDS + JITTER_SECONDS)


async def compliance_sleep(
    source: SourceKey | None = None,
    shutdown: ShutdownController | None = None,
) -> bool:
    """Pause between jobs. Returns False if shutdown was requested during the wait."""
    delay = compliance_sleep_seconds(source)
    logger.info("[schedule] Post-request pause: %d seconds", delay)
    return await interruptible_sleep(delay, shutdown, label="Post-request pause")


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
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{label} failed with no attempts executed")


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
                logger.warning("[%s] Primary failed (%s)", ticker, primary_exc)
                return TickerResult(
                    ticker=ticker,
                    success=False,
                    source="primary",
                    proxy=proxy.label,
                    error=str(primary_exc),
                    transient=is_retryable_primary_failure(primary_exc),
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


async def process_job(
    job: ScrapeJob,
    proxy_pool: list[ProxyConfig],
    *,
    holder: BrowserHolder | None = None,
) -> TickerResult:
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

    if ai_is_configured() and job.source in ai_primary_sources():
        logger.info(
            "[%s] Fetching %s metric directly via AI (configured in AI_PRIMARY_FOR)...",
            job.ticker,
            job.source,
        )
        ai_val = await fetch_ai_valuation_metric(job.ticker, job.source)
        if ai_val:
            return TickerResult(
                ticker=job.ticker,
                success=True,
                source=f"ai_{job.source}",
                metric=ai_val,
                proxy="ai_search",
            )

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=browser_headless())
            if holder is not None:
                holder.browser = browser
            context = await create_isolated_context(browser, proxy)
            if holder is not None:
                holder.context = context
            page = await context.new_page()
            if holder is not None:
                holder.page = page
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
                    "[%s] %s primary failed (%s)",
                    job.ticker,
                    job.source,
                    primary_exc,
                )
                if ai_is_configured():
                    logger.info(
                        "[%s] Attempting AI live web search fallback for %s...",
                        job.ticker,
                        job.source,
                    )
                    ai_val = await fetch_ai_valuation_metric(job.ticker, job.source)
                    if ai_val:
                        return TickerResult(
                            ticker=job.ticker,
                            success=True,
                            source=f"ai_{job.source}",
                            metric=ai_val,
                            proxy="ai_search",
                        )
                return TickerResult(
                    ticker=job.ticker,
                    success=False,
                    source="primary",
                    proxy=proxy.label,
                    error=str(primary_exc),
                    transient=is_retryable_primary_failure(primary_exc),
                )

    except asyncio.CancelledError:
        logger.warning("[%s] %s scrape cancelled (watchdog or shutdown)", job.ticker, job.source)
        raise
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
        if holder is not None:
            holder.page = None
            holder.context = None
            holder.browser = None


async def _force_close_holder(holder: BrowserHolder, ticker: str) -> None:
    await teardown(holder.page, holder.context, holder.browser, ticker)
    holder.page = None
    holder.context = None
    holder.browser = None


async def run_alphaspread_job_with_watchdog(
    job: ScrapeJob,
    proxy_pool: list[ProxyConfig],
) -> TickerResult:
    """Run one isolated Alpha Spread job with a hard wall-clock timeout."""
    holder = BrowserHolder()
    task = asyncio.create_task(process_job(job, proxy_pool, holder=holder))
    done, _pending = await asyncio.wait(
        {task}, timeout=ALPHASPREAD_JOB_TIMEOUT_SECONDS
    )
    if task in done:
        return task.result()

    logger.warning("WATCHDOG TIMEOUT: %s — killed and requeued", job.ticker)
    task.cancel()
    try:
        await asyncio.wait_for(_force_close_holder(holder, job.ticker), timeout=10)
    except Exception as exc:
        logger.warning("[%s] watchdog browser kill: %s", job.ticker, exc)
    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        logger.warning(
            "[%s] watchdog: cancelled scrape still pending; dropping task",
            job.ticker,
        )
    except Exception as exc:
        logger.debug("[%s] watchdog cancelled task: %s", job.ticker, exc)
    return TickerResult(
        ticker=job.ticker,
        success=False,
        source="watchdog",
        error="watchdog timeout",
        transient=True,
    )


async def _await_with_watchdog(coro, *, ticker: str, source: str):
    """Cancel a per-ticker scrape if it exceeds the job timeout (shared sessions)."""
    task = asyncio.create_task(coro)
    done, _pending = await asyncio.wait(
        {task}, timeout=ALPHASPREAD_JOB_TIMEOUT_SECONDS
    )
    if task in done:
        return task.result()
    logger.warning("WATCHDOG TIMEOUT: %s source=%s — cancelled", ticker, source)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass
    return None


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


def _partition_gurufocus_jobs(jobs: list[ScrapeJob]) -> tuple[list[ScrapeJob], list[ScrapeJob]]:
    gf_jobs = [job for job in jobs if job.source == "gurufocus"]
    other_jobs = [job for job in jobs if job.source != "gurufocus"]
    return gf_jobs, other_jobs


def _is_gurufocus_block_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "restrictive or unstable status: 403" in msg or "restrictive or unstable status: 429" in msg


async def _process_gurufocus_with_session(
    job: ScrapeJob,
    session: GuruFocusSession,
) -> TickerResult:
    proxy_label = session.proxy_label

    if ai_is_configured() and ("gurufocus" in ai_primary_sources() or job.source in ai_primary_sources()):
        logger.info(
            "[%s] Fetching gurufocus metric directly via AI (configured in AI_PRIMARY_FOR)...",
            job.ticker,
        )
        ai_val = await fetch_ai_valuation_metric(job.ticker, "gurufocus")
        if ai_val:
            return TickerResult(
                ticker=job.ticker,
                success=True,
                source="ai_gurufocus",
                metric=ai_val,
                proxy="ai_search",
            )

    async def _scrape_once() -> TickerResult:
        page = await session.ensure_page()
        primary = await extract_gurufocus_metric(page, job)
        session.record_view()
        return TickerResult(
            ticker=job.ticker,
            success=True,
            source=f"primary_{primary.method}",
            metric=primary.metric,
            proxy=session.proxy_label,
        )

    last_primary_exc: Exception | None = None
    rotated_for_block = False
    for primary_attempt in range(1, SCRAPE_PRIMARY_RETRY_ATTEMPTS + 1):
        try:
            return await _scrape_once()
        except Exception as exc:
            last_primary_exc = exc
            if _is_gurufocus_block_error(exc) and not rotated_for_block:
                rotated_for_block = True
                logger.warning(
                    "[%s] GuruFocus blocked (%s); rotating browser identity and retrying",
                    job.ticker,
                    exc,
                )
                session.record_view()
                if session.is_headless:
                    await session.escalate_to_headed()
                else:
                    await session.rotate_identity()
                try:
                    return await _scrape_once()
                except Exception as retry_exc:
                    last_primary_exc = retry_exc
                    break
            if (
                primary_attempt < SCRAPE_PRIMARY_RETRY_ATTEMPTS
                and is_retryable_primary_failure(exc)
            ):
                delay = scrape_nav_retry_base_seconds() * primary_attempt
                logger.warning(
                    "[%s] gurufocus primary attempt %d/%d failed (%s); "
                    "retrying primary in %.1fs",
                    job.ticker,
                    primary_attempt,
                    SCRAPE_PRIMARY_RETRY_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    assert last_primary_exc is not None
    logger.warning("[%s] gurufocus primary failed (%s)", job.ticker, last_primary_exc)
    if ai_is_configured():
        logger.info(
            "[%s] gurufocus direct scrape blocked; attempting AI live web search fallback...",
            job.ticker,
        )
        ai_val = await fetch_ai_valuation_metric(job.ticker, "gurufocus")
        if ai_val:
            return TickerResult(
                ticker=job.ticker,
                success=True,
                source="ai_gurufocus",
                metric=ai_val,
                proxy="ai_search",
            )
    session.record_view()
    return TickerResult(
        ticker=job.ticker,
        success=False,
        source="primary",
        proxy=session.proxy_label or proxy_label,
        error=str(last_primary_exc),
        transient=_is_gurufocus_block_error(last_primary_exc)
        or is_retryable_primary_failure(last_primary_exc),
    )


async def process_gurufocus_jobs(
    jobs: list[ScrapeJob],
    proxy_pool: list[ProxyConfig],
    *,
    shutdown: ShutdownController | None = None,
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    """Scrape GuruFocus jobs in a shared browser session; yield after each ticker."""
    if not jobs:
        return

    session = GuruFocusSession(proxy_pool)
    total = len(jobs)

    try:
        for index, job in enumerate(jobs, start=1):
            if shutdown is not None and shutdown.is_requested():
                logger.warning(
                    "[sheet] Shutdown during GuruFocus batch at GF %d/%d | row=%d ticker=%s col=%s",
                    index - 1,
                    total,
                    shutdown.progress.last_row or job.row,
                    shutdown.progress.last_ticker or job.ticker,
                    job.value_col,
                )
                break

            if shutdown is not None:
                shutdown.note_job(
                    row=job.row,
                    ticker=job.ticker,
                    source=job.source,
                    batch="GuruFocus",
                    batch_index=index,
                    batch_total=total,
                )

            logger.info(
                "[sheet] Processing row=%d ticker=%s source=gurufocus col=%s (GF %d/%d)",
                job.row,
                job.ticker,
                job.value_col,
                index,
                total,
            )
            result = await _await_with_watchdog(
                _process_gurufocus_with_session(job, session),
                ticker=job.ticker,
                source="gurufocus",
            )
            if result is None:
                logger.warning(
                    "[%s] GuruFocus watchdog timeout; rotating identity",
                    job.ticker,
                )
                try:
                    await session.rotate_identity()
                except Exception as rotate_exc:
                    logger.warning("[%s] GuruFocus rotate after watchdog: %s", job.ticker, rotate_exc)
                result = TickerResult(
                    ticker=job.ticker,
                    success=False,
                    source="watchdog",
                    proxy=session.proxy_label,
                    error="watchdog timeout",
                    transient=True,
                )
            logger.info(
                "[result] ticker=%s success=%s source=%s metric=%s error=%s transient=%s",
                result.ticker,
                result.success,
                result.source,
                result.metric,
                result.error,
                result.transient,
            )
            yield job, result
            if shutdown is not None:
                shutdown.note_job_completed()

            if index < total:
                if not await compliance_sleep("gurufocus", shutdown):
                    logger.warning(
                        "[sheet] Shutdown during GuruFocus post-request pause at GF %d/%d | row=%d ticker=%s",
                        index,
                        total,
                        job.row,
                        job.ticker,
                    )
                    break
    finally:
        await session.close()


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

    last_primary_exc: Exception | None = None
    for primary_attempt in range(1, SCRAPE_PRIMARY_RETRY_ATTEMPTS + 1):
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
                return TickerResult(
                    ticker=job.ticker,
                    success=False,
                    source="primary",
                    proxy=proxy_label,
                    error=str(limit_exc),
                    transient=True,
                )
            await session.rotate_identity()
            try:
                return await _scrape_once()
            except Exception as retry_exc:
                logger.warning(
                    "[%s] valueinvesting retry failed (%s)",
                    job.ticker,
                    retry_exc,
                )
                session.record_view()
                return TickerResult(
                    ticker=job.ticker,
                    success=False,
                    source="primary",
                    proxy=proxy_label,
                    error=str(retry_exc),
                    transient=is_retryable_primary_failure(retry_exc),
                )
        except Exception as exc:
            last_primary_exc = exc
            if (
                primary_attempt < SCRAPE_PRIMARY_RETRY_ATTEMPTS
                and is_retryable_primary_failure(exc)
            ):
                delay = scrape_nav_retry_base_seconds() * primary_attempt
                logger.warning(
                    "[%s] valueinvesting primary attempt %d/%d failed (%s); "
                    "retrying primary in %.1fs",
                    job.ticker,
                    primary_attempt,
                    SCRAPE_PRIMARY_RETRY_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    assert last_primary_exc is not None
    logger.warning(
        "[%s] valueinvesting primary failed (%s)",
        job.ticker,
        last_primary_exc,
    )
    session.record_view()
    return TickerResult(
        ticker=job.ticker,
        success=False,
        source="primary",
        proxy=proxy_label,
        error=str(last_primary_exc),
        transient=is_retryable_primary_failure(last_primary_exc),
    )


async def process_valueinvesting_jobs(
    jobs: list[ScrapeJob],
    proxy_pool: list[ProxyConfig],
    *,
    shutdown: ShutdownController | None = None,
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    """Scrape each VI job in a shared browser session; yield after each ticker for immediate sheet writes."""
    if not jobs:
        return

    session = ValueInvestingSession(proxy_pool)
    total = len(jobs)
    failed_for_replay: list[ScrapeJob] = []

    async def _run_one(index: int, job: ScrapeJob, *, replay: bool) -> TickerResult | None:
        if shutdown is not None and shutdown.is_requested():
            return None

        if shutdown is not None:
            batch_label = "ValueInvesting-replay" if replay else "ValueInvesting"
            shutdown.note_job(
                row=job.row,
                ticker=job.ticker,
                source=job.source,
                batch=batch_label,
                batch_index=index,
                batch_total=total,
            )

        phase = "replay" if replay else "VI"
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=valueinvesting col=%s (%s %d/%d)",
            job.row,
            job.ticker,
            job.value_col,
            phase,
            index,
            total,
        )
        result = await _await_with_watchdog(
            _process_valueinvesting_with_session(job, session),
            ticker=job.ticker,
            source="valueinvesting",
        )
        if result is None:
            result = TickerResult(
                ticker=job.ticker,
                success=False,
                source="watchdog",
                proxy=session.proxy_label,
                error="watchdog timeout",
                transient=True,
            )
        logger.info(
            "[result] ticker=%s success=%s source=%s metric=%s error=%s transient=%s",
            result.ticker,
            result.success,
            result.source,
            result.metric,
            result.error,
            result.transient,
        )
        return result

    try:
        for index, job in enumerate(jobs, start=1):
            if shutdown is not None and shutdown.is_requested():
                logger.warning(
                    "[sheet] Shutdown during ValueInvesting batch at VI %d/%d | row=%d ticker=%s col=%s",
                    index - 1,
                    total,
                    shutdown.progress.last_row or job.row,
                    shutdown.progress.last_ticker or job.ticker,
                    job.value_col,
                )
                break

            result = await _run_one(index, job, replay=False)
            if result is None:
                break

            yield job, result
            if shutdown is not None:
                shutdown.note_job_completed()

            if not result.success and result.transient:
                failed_for_replay.append(job)

            if index < total:
                if not await compliance_sleep("valueinvesting", shutdown):
                    logger.warning(
                        "[sheet] Shutdown during ValueInvesting post-request pause at VI %d/%d | row=%d ticker=%s",
                        index,
                        total,
                        job.row,
                        job.ticker,
                    )
                    break

        if (
            failed_for_replay
            and VALUEINVESTING_FAILED_REPLAY
            and not (shutdown is not None and shutdown.is_requested())
        ):
            logger.info(
                "[sheet] ValueInvesting replay pass: %d transient failure(s) after initial batch",
                len(failed_for_replay),
            )
            if VALUEINVESTING_REPLAY_PAUSE_SECONDS > 0:
                logger.info(
                    "[sheet] ValueInvesting replay throttle pause: %d seconds",
                    VALUEINVESTING_REPLAY_PAUSE_SECONDS,
                )
                if not await interruptible_sleep_seconds(
                    VALUEINVESTING_REPLAY_PAUSE_SECONDS,
                    shutdown,
                ):
                    logger.warning("[sheet] Shutdown during ValueInvesting replay throttle pause")
                    return

            for replay_index, job in enumerate(failed_for_replay, start=1):
                if shutdown is not None and shutdown.is_requested():
                    break
                result = await _run_one(replay_index, job, replay=True)
                if result is None:
                    break
                yield job, result
                if shutdown is not None:
                    shutdown.note_job_completed()
                if replay_index < len(failed_for_replay):
                    if not await compliance_sleep("valueinvesting", shutdown):
                        break
    finally:
        await session.close()


async def _clear_stale_cell(
    writes: SheetWriteBuffer,
    job: ScrapeJob,
    *,
    reason: str,
) -> bool:
    if not has_stored_cell_value(job.current_value):
        logger.info(
            "[sheet] row=%d col=%s ticker=%s already empty (%s)",
            job.row,
            job.value_col,
            job.ticker,
            reason,
        )
        return False
    await writes.enqueue_clear(job)
    logger.info(
        "[sheet] Cleared row=%d col=%s ticker=%s old=%r reason=%s",
        job.row,
        job.value_col,
        job.ticker,
        job.current_value,
        reason,
    )
    return True


async def _apply_clear_only_job(writes: SheetWriteBuffer, job: ScrapeJob) -> SheetJobResult:
    index_hint = job.index_label or job.security_type
    cleared = await _clear_stale_cell(
        writes,
        job,
        reason=f"non-US index {index_hint!r}",
    )
    return SheetJobResult(job=job, result=None, written=cleared)


async def _apply_job_result(
    writes: SheetWriteBuffer,
    job: ScrapeJob,
    result: TickerResult,
) -> SheetJobResult:
    if job.clear_only:
        res = await _apply_clear_only_job(writes, job)
        mark_completed(job.source, job.row)
        return res

    written = False
    if result.success and result.metric:
        value = format_for_sheet(result.metric)
        if value.strip():
            await writes.enqueue_write(job, value)
            written = True
            logger.info(
                "[sheet] Buffered write row=%d col=%s ticker=%s old=%r value=%s source=%s "
                "(flush when buffer reaches %d)",
                job.row,
                job.value_col,
                job.ticker,
                job.current_value,
                value,
                result.source,
                writes.batch_size,
            )
        elif await _clear_stale_cell(writes, job, reason="normalized scrape result empty"):
            written = True
        else:
            logger.warning(
                "[sheet] Left empty row=%d col=%s ticker=%s after normalized empty result",
                job.row,
                job.value_col,
                job.ticker,
            )
    elif result.transient and has_stored_cell_value(job.current_value):
        logger.info(
            "[sheet] Keeping stale row=%d col=%s ticker=%s value=%r after transient error=%s",
            job.row,
            job.value_col,
            job.ticker,
            job.current_value,
            result.error,
        )
    elif await _clear_stale_cell(writes, job, reason=result.error or "scrape failed"):
        written = True
    else:
        logger.info(
            "[sheet] Left empty row=%d col=%s ticker=%s after no valid value error=%s",
            job.row,
            job.value_col,
            job.ticker,
            result.error,
        )
        
    if not result.transient:
        mark_completed(job.source, job.row)
        
    return SheetJobResult(job=job, result=result, written=written)


async def _process_job_on_page(
    page: Page,
    job: ScrapeJob,
    proxy_label: str,
) -> TickerResult:
    grok_primary_sources = {
        s.strip().lower()
        for s in os.environ.get("GROK_PRIMARY_FOR", "").split(",")
        if s.strip()
    }
    if grok_is_configured() and job.source in grok_primary_sources:
        logger.info(
            "[%s] Fetching %s metric directly via Grok AI (configured in GROK_PRIMARY_FOR)...",
            job.ticker,
            job.source,
        )
        grok_val = await fetch_metric_from_grok(job.ticker, job.source)
        if grok_val:
            return TickerResult(
                ticker=job.ticker,
                success=True,
                source=f"grok_ai_{job.source}",
                metric=grok_val,
                proxy="xai_grok",
            )

    try:
        primary = await extract_primary_metric_for_job(page, job)
        return TickerResult(
            ticker=job.ticker,
            success=True,
            source=f"primary_{primary.method}",
            metric=primary.metric,
            proxy=proxy_label,
        )
    except Exception as primary_exc:
        logger.warning(
            "[%s] %s primary failed (%s)",
            job.ticker,
            job.source,
            primary_exc,
        )
        if grok_is_configured():
            logger.info(
                "[%s] Attempting Grok AI web search fallback for %s...",
                job.ticker,
                job.source,
            )
            grok_val = await fetch_metric_from_grok(job.ticker, job.source)
            if grok_val:
                return TickerResult(
                    ticker=job.ticker,
                    success=True,
                    source=f"grok_ai_{job.source}",
                    metric=grok_val,
                    proxy="xai_grok",
                )
        return TickerResult(
            ticker=job.ticker,
            success=False,
            source="primary",
            proxy=proxy_label,
            error=str(primary_exc),
            transient=is_retryable_primary_failure(primary_exc),
        )


def _is_alphaspread_restricted(result: TickerResult) -> bool:
    """True when Alpha Spread refused the page (403/429), not a selector miss."""
    if result.success:
        return False
    err = (result.error or "").lower()
    return "403" in err or "429" in err or "restrictive or unstable status" in err


def _is_alphaspread_watchdog(result: TickerResult) -> bool:
    return (not result.success) and "watchdog timeout" in (result.error or "").lower()


def _sync_guard_progress(
    shutdown: ShutdownController | None, guard: AlphaSpreadGuard
) -> None:
    if shutdown is not None:
        shutdown.progress.extra = guard.status_line()


def _note_alphaspread_outcome(
    job: ScrapeJob,
    result: TickerResult,
    guard: AlphaSpreadGuard,
    job_cache: dict[tuple[str, SourceKey], TickerResult],
) -> None:
    key = (job.ticker, job.source)
    if result.success:
        job_cache[key] = result
        guard.clear_attempts(job)
        return
    if _is_alphaspread_restricted(result) or _is_alphaspread_watchdog(result):
        job_cache.pop(key, None)
        err = (result.error or "").lower()
        if _is_alphaspread_watchdog(result):
            reason = "watchdog"
        elif "429" in err:
            reason = "429"
        else:
            reason = "403"
        guard.enqueue_retry(job, reason)
        return
    job_cache[key] = result


def _has_non_as_work(queues: dict[str, deque]) -> bool:
    return any(name != "alphaspread" and len(q) > 0 for name, q in queues.items())


def _inject_due_retries(queues: dict[str, deque], guard: AlphaSpreadGuard) -> None:
    for job in reversed(guard.due_jobs()):
        queues["alphaspread"].appendleft(job)
        logger.info(
            "[alphaspread] re-injected retry ticker=%s row=%d",
            job.ticker,
            job.row,
        )


def _return_jobs_to_queue(queue: deque | None, jobs: list[ScrapeJob]) -> None:
    if queue is None or not jobs:
        return
    for job in reversed(jobs):
        queue.appendleft(job)


async def _process_alphaspread_chunk(
    jobs: list[ScrapeJob],
    proxy_pool: list[ProxyConfig],
    *,
    shutdown: ShutdownController | None = None,
    round_num: int = 0,
    leftover_queue: deque | None = None,
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    """One isolated browser per ticker (Aug 13 working pattern).

    403/429 stops the rest of this chunk (jobs go back on the normal queue).
    Same-run retry of the failed ticker is handled by AlphaSpreadGuard.
    """
    if not jobs:
        return

    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        if shutdown is not None and shutdown.is_requested():
            _return_jobs_to_queue(leftover_queue, jobs[index - 1 :])
            break
        if shutdown is not None:
            shutdown.note_job(
                row=job.row,
                ticker=job.ticker,
                source=job.source,
                batch=f"AS-round{round_num}",
                batch_index=index,
                batch_total=total,
            )
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=alphaspread col=%s "
            "(round %d AS %d/%d isolated-browser)",
            job.row,
            job.ticker,
            job.value_col,
            round_num,
            index,
            total,
        )
        result = await run_alphaspread_job_with_watchdog(job, proxy_pool)
        logger.info(
            "[result] ticker=%s success=%s source=%s metric=%s error=%s",
            result.ticker,
            result.success,
            result.source,
            result.metric,
            result.error,
        )
        yield job, result
        if shutdown is not None:
            shutdown.note_job_completed()

        if _is_alphaspread_restricted(result):
            leftover = jobs[index:]
            logger.warning(
                "[alphaspread] Restricted response on %s; deferring %d remaining "
                "job(s) this chunk",
                job.ticker,
                len(leftover),
            )
            _return_jobs_to_queue(leftover_queue, leftover)
            break

        if not await compliance_sleep("alphaspread", shutdown):
            _return_jobs_to_queue(leftover_queue, jobs[index:])
            break


async def _process_yahoo_group_on_page(
    page: Page,
    group: YahooTickerGroup,
    proxy_label: str,
) -> list[tuple[ScrapeJob, TickerResult]]:
    kinds = {
        _YAHOO_KIND_BY_SOURCE[job.source]
        for job in group.jobs
        if job.source in _YAHOO_KIND_BY_SOURCE
    }
    try:
        found = await extract_yahoo_price_targets(page, group.ticker, kinds=kinds)
    except Exception as exc:
        logger.warning("[%s] Yahoo coalesced scrape failed (%s)", group.ticker, exc)
        transient = is_retryable_primary_failure(exc)
        return [
            (
                job,
                TickerResult(
                    ticker=job.ticker,
                    success=False,
                    source="primary",
                    proxy=proxy_label,
                    error=str(exc),
                    transient=transient,
                ),
            )
            for job in group.jobs
        ]

    pairs: list[tuple[ScrapeJob, TickerResult]] = []
    for job in group.jobs:
        kind = _YAHOO_KIND_BY_SOURCE.get(job.source)
        primary = found.get(kind) if kind else None
        if primary:
            pairs.append(
                (
                    job,
                    TickerResult(
                        ticker=job.ticker,
                        success=True,
                        source=f"primary_{primary.method}",
                        metric=primary.metric,
                        proxy=proxy_label,
                    ),
                )
            )
        else:
            pairs.append(
                (
                    job,
                    TickerResult(
                        ticker=job.ticker,
                        success=False,
                        source="primary",
                        proxy=proxy_label,
                        error=f"Yahoo {kind} price target not found",
                    ),
                )
            )
    return pairs


async def _process_yahoo_chunk(
    groups: list[YahooTickerGroup],
    proxy_pool: list[ProxyConfig],
    *,
    shutdown: ShutdownController | None = None,
    round_num: int = 0,
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    if not groups:
        return

    proxy = random.choice(proxy_pool)
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    total = len(groups)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=browser_headless())
            context = await create_isolated_context(browser, proxy)
            page = await context.new_page()
            await apply_stealth(page)

            for index, group in enumerate(groups, start=1):
                if shutdown is not None and shutdown.is_requested():
                    break
                first = group.jobs[0]
                if shutdown is not None:
                    shutdown.note_job(
                        row=first.row,
                        ticker=group.ticker,
                        source=first.source,
                        batch=f"YH-round{round_num}",
                        batch_index=index,
                        batch_total=total,
                    )
                logger.info(
                    "[sheet] Processing Yahoo ticker=%s jobs=%d (round %d YH %d/%d)",
                    group.ticker,
                    len(group.jobs),
                    round_num,
                    index,
                    total,
                )
                pairs = await _await_with_watchdog(
                    _process_yahoo_group_on_page(page, group, proxy.label),
                    ticker=group.ticker,
                    source="yahoo",
                )
                if pairs is None:
                    pairs = [
                        (
                            job,
                            TickerResult(
                                ticker=group.ticker,
                                success=False,
                                source="watchdog",
                                proxy=proxy.label,
                                error="watchdog timeout",
                                transient=True,
                            ),
                        )
                        for job in group.jobs
                    ]
                for job, result in pairs:
                    logger.info(
                        "[result] ticker=%s source=%s success=%s metric=%s error=%s",
                        result.ticker,
                        job.source,
                        result.success,
                        result.metric,
                        result.error,
                    )
                    yield job, result
                    if shutdown is not None:
                        shutdown.note_job_completed()
                if index < total:
                    if not await compliance_sleep("price_target_avg", shutdown):
                        break
    finally:
        await teardown(page, context, browser, "yahoo-chunk")


async def _process_vi_chunk_with_session(
    jobs: list[ScrapeJob],
    session: ValueInvestingSession,
    *,
    shutdown: ShutdownController | None = None,
    round_num: int = 0,
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        if shutdown is not None and shutdown.is_requested():
            break
        if shutdown is not None:
            shutdown.note_job(
                row=job.row,
                ticker=job.ticker,
                source=job.source,
                batch=f"VI-round{round_num}",
                batch_index=index,
                batch_total=total,
            )
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=valueinvesting col=%s "
            "(round %d VI %d/%d)",
            job.row,
            job.ticker,
            job.value_col,
            round_num,
            index,
            total,
        )
        result = await _await_with_watchdog(
            _process_valueinvesting_with_session(job, session),
            ticker=job.ticker,
            source="valueinvesting",
        )
        if result is None:
            result = TickerResult(
                ticker=job.ticker,
                success=False,
                source="watchdog",
                proxy=session.proxy_label,
                error="watchdog timeout",
                transient=True,
            )
        logger.info(
            "[result] ticker=%s success=%s source=%s metric=%s error=%s",
            result.ticker,
            result.success,
            result.source,
            result.metric,
            result.error,
        )
        yield job, result
        if shutdown is not None:
            shutdown.note_job_completed()
        if index < total:
            if not await compliance_sleep("valueinvesting", shutdown):
                break


async def _process_gf_chunk_with_session(
    jobs: list[ScrapeJob],
    session: GuruFocusSession,
    *,
    shutdown: ShutdownController | None = None,
    round_num: int = 0,
) -> AsyncIterator[tuple[ScrapeJob, TickerResult]]:
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        if shutdown is not None and shutdown.is_requested():
            break
        if shutdown is not None:
            shutdown.note_job(
                row=job.row,
                ticker=job.ticker,
                source=job.source,
                batch=f"GF-round{round_num}",
                batch_index=index,
                batch_total=total,
            )
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=gurufocus col=%s "
            "(round %d GF %d/%d)",
            job.row,
            job.ticker,
            job.value_col,
            round_num,
            index,
            total,
        )
        result = await _await_with_watchdog(
            _process_gurufocus_with_session(job, session),
            ticker=job.ticker,
            source="gurufocus",
        )
        if result is None:
            logger.warning(
                "[%s] GuruFocus watchdog timeout; rotating identity",
                job.ticker,
            )
            try:
                await session.rotate_identity()
            except Exception as rotate_exc:
                logger.warning("[%s] GuruFocus rotate after watchdog: %s", job.ticker, rotate_exc)
            result = TickerResult(
                ticker=job.ticker,
                success=False,
                source="watchdog",
                proxy=session.proxy_label,
                error="watchdog timeout",
                transient=True,
            )
        logger.info(
            "[result] ticker=%s success=%s source=%s metric=%s error=%s",
            result.ticker,
            result.success,
            result.source,
            result.metric,
            result.error,
        )
        yield job, result
        if shutdown is not None:
            shutdown.note_job_completed()
        if index < total:
            if not await compliance_sleep("gurufocus", shutdown):
                break


async def _run_legacy_batch_scrape(
    scrape_jobs: list[ScrapeJob],
    proxy_pool: list[ProxyConfig],
    writes: SheetWriteBuffer,
    results: list[SheetJobResult],
    job_cache: dict[tuple[str, SourceKey], TickerResult],
    *,
    shutdown: ShutdownController | None = None,
    guard: AlphaSpreadGuard | None = None,
) -> None:
    guard = guard or AlphaSpreadGuard()
    vi_jobs, other_jobs = _partition_valueinvesting_jobs(scrape_jobs)
    if vi_jobs and not (shutdown is not None and shutdown.is_requested()):
        logger.info(
            "[sheet] ValueInvesting batch: %d jobs (shared browser, rotate identity every %s views)",
            len(vi_jobs),
            os.environ.get("RESILIENT_VALUEINVESTING_VIEWS_PER_IDENTITY", "5"),
        )
        async for job, result in process_valueinvesting_jobs(
            vi_jobs, proxy_pool, shutdown=shutdown
        ):
            if shutdown is not None and shutdown.is_requested():
                break
            job_cache[(job.ticker, job.source)] = result
            results.append(await _apply_job_result(writes, job, result))

    gf_jobs, other_jobs = _partition_gurufocus_jobs(other_jobs)
    if gf_jobs and not (shutdown is not None and shutdown.is_requested()):
        logger.info(
            "[sheet] GuruFocus batch: %d jobs (shared browser, rotate identity every %s views)",
            len(gf_jobs),
            os.environ.get("RESILIENT_GURUFOCUS_VIEWS_PER_IDENTITY", "15"),
        )
        async for job, result in process_gurufocus_jobs(
            gf_jobs, proxy_pool, shutdown=shutdown
        ):
            if shutdown is not None and shutdown.is_requested():
                break
            job_cache[(job.ticker, job.source)] = result
            results.append(await _apply_job_result(writes, job, result))

    total_other = len(other_jobs)
    skipped_as: list[ScrapeJob] = []
    for index, job in enumerate(other_jobs, start=1):
        if shutdown is not None and shutdown.is_requested():
            logger.warning(
                "[sheet] Shutdown during scrape jobs at %d/%d | last row=%d ticker=%s source=%s",
                index - 1,
                total_other,
                shutdown.progress.last_row or job.row,
                shutdown.progress.last_ticker or job.ticker,
                shutdown.progress.last_source or job.source,
            )
            break

        if job.source == "alphaspread" and guard.is_open():
            skipped_as.append(job)
            continue

        if shutdown is not None:
            shutdown.note_job(
                row=job.row,
                ticker=job.ticker,
                source=job.source,
                batch="scrape",
                batch_index=index,
                batch_total=total_other,
            )
            _sync_guard_progress(shutdown, guard)

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
        if job.source != "alphaspread" and cache_key in job_cache:
            result = job_cache[cache_key]
            logger.info("[sheet] Reusing cached result for %s %s", job.ticker, job.source)
        elif job.source == "alphaspread":
            result = await run_alphaspread_job_with_watchdog(job, proxy_pool)
            _note_alphaspread_outcome(job, result, guard, job_cache)
            _sync_guard_progress(shutdown, guard)
        else:
            result = await process_job(job, proxy_pool)
            job_cache[cache_key] = result

        results.append(await _apply_job_result(writes, job, result))
        if shutdown is not None:
            shutdown.note_job_completed()

        if index < total_other or skipped_as or guard.pending_retry_count():
            if not await compliance_sleep(job.source, shutdown):
                logger.warning(
                    "[sheet] Shutdown during post-request pause at %d/%d | row=%d ticker=%s",
                    index,
                    total_other,
                    job.row,
                    job.ticker,
                )
                break

    as_queue: deque = deque(skipped_as)
    while as_queue or guard.pending_retry_count():
        if shutdown is not None and shutdown.is_requested():
            break
        _sync_guard_progress(shutdown, guard)
        for job in reversed(guard.due_jobs()):
            as_queue.appendleft(job)
            logger.info(
                "[alphaspread] re-injected retry ticker=%s row=%d",
                job.ticker,
                job.row,
            )
        if guard.is_open() or not as_queue:
            wait = max(1, int(round(guard.seconds_until_ready())) or 1)
            logger.info("[alphaspread] waiting %ds | %s", wait, guard.status_line())
            if not await interruptible_sleep(
                wait, shutdown, label="Alpha Spread retry/breaker wait"
            ):
                break
            continue
        job = as_queue.popleft()
        if shutdown is not None:
            shutdown.note_job(
                row=job.row,
                ticker=job.ticker,
                source=job.source,
                batch="alphaspread-retry",
            )
        logger.info(
            "[sheet] Processing row=%d ticker=%s source=alphaspread col=%s (retry drain)",
            job.row,
            job.ticker,
            job.value_col,
        )
        result = await run_alphaspread_job_with_watchdog(job, proxy_pool)
        _note_alphaspread_outcome(job, result, guard, job_cache)
        _sync_guard_progress(shutdown, guard)
        results.append(await _apply_job_result(writes, job, result))
        if shutdown is not None:
            shutdown.note_job_completed()
        if as_queue or guard.pending_retry_count():
            if not await compliance_sleep("alphaspread", shutdown):
                break


async def _run_round_robin_scrape(
    scrape_jobs: list[ScrapeJob],
    proxy_pool: list[ProxyConfig],
    writes: SheetWriteBuffer,
    results: list[SheetJobResult],
    job_cache: dict[tuple[str, SourceKey], TickerResult],
    *,
    shutdown: ShutdownController | None = None,
    guard: AlphaSpreadGuard | None = None,
) -> None:
    guard = guard or AlphaSpreadGuard()
    queues = partition_scrape_queues(scrape_jobs)
    logger.info(
        "[sheet] Round-robin scheduler chunk_size=%d alphaspread_chunk=%d remaining %s",
        CHUNK_SIZE,
        ALPHASPREAD_CHUNK_SIZE,
        queue_remaining_summary(queues),
    )
    _sync_guard_progress(shutdown, guard)

    vi_session = ValueInvestingSession(proxy_pool)
    gf_session = GuruFocusSession(proxy_pool)
    round_num = 0

    try:
        while any_queue_nonempty(queues) or guard.pending_retry_count():
            if shutdown is not None and shutdown.is_requested():
                break
            _inject_due_retries(queues, guard)
            _sync_guard_progress(shutdown, guard)

            can_run_as = bool(queues["alphaspread"]) and not guard.is_open()
            can_run_other = _has_non_as_work(queues)
            if not can_run_as and not can_run_other:
                if guard.pending_retry_count() or guard.is_open():
                    wait = max(1, int(round(guard.seconds_until_ready())) or 1)
                    logger.info("[alphaspread] waiting %ds | %s", wait, guard.status_line())
                    if not await interruptible_sleep(
                        wait, shutdown, label="Alpha Spread retry/breaker wait"
                    ):
                        break
                    continue
                break

            round_num += 1
            logger.info(
                "[round %d] start | remaining %s | %s",
                round_num,
                queue_remaining_summary(queues),
                guard.status_line(),
            )

            for source_name in ROUND_ROBIN_ORDER:
                if shutdown is not None and shutdown.is_requested():
                    break
                if source_name == "alphaspread" and guard.is_open():
                    logger.info(
                        "[round %d] alphaspread skipped | %s",
                        round_num,
                        guard.status_line(),
                    )
                    continue
                queue = queues[source_name]
                if not queue:
                    continue

                chunk_size = (
                    ALPHASPREAD_CHUNK_SIZE if source_name == "alphaspread" else CHUNK_SIZE
                )
                chunk = take_chunk(queue, chunk_size)
                logger.info(
                    "[round %d] %s chunk=%d | remaining after take %s",
                    round_num,
                    source_name,
                    len(chunk),
                    queue_remaining_summary(queues),
                )

                if source_name == "alphaspread":
                    async for job, result in _process_alphaspread_chunk(
                        chunk,
                        proxy_pool,
                        shutdown=shutdown,
                        round_num=round_num,
                        leftover_queue=queue,
                    ):
                        _note_alphaspread_outcome(job, result, guard, job_cache)
                        _sync_guard_progress(shutdown, guard)
                        results.append(await _apply_job_result(writes, job, result))
                elif source_name == "valueinvesting":
                    async for job, result in _process_vi_chunk_with_session(
                        chunk, vi_session, shutdown=shutdown, round_num=round_num
                    ):
                        job_cache[(job.ticker, job.source)] = result
                        results.append(await _apply_job_result(writes, job, result))
                elif source_name == "gurufocus":
                    async for job, result in _process_gf_chunk_with_session(
                        chunk, gf_session, shutdown=shutdown, round_num=round_num
                    ):
                        job_cache[(job.ticker, job.source)] = result
                        results.append(await _apply_job_result(writes, job, result))
                elif source_name == "yahoo":
                    async for job, result in _process_yahoo_chunk(
                        chunk, proxy_pool, shutdown=shutdown, round_num=round_num
                    ):
                        job_cache[(job.ticker, job.source)] = result
                        results.append(await _apply_job_result(writes, job, result))

            logger.info(
                "[round %d] done | remaining %s | %s",
                round_num,
                queue_remaining_summary(queues),
                guard.status_line(),
            )
    finally:
        await vi_session.close()
        await gf_session.close()


async def run_sheet_collector(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    sources: set[SourceKey] | None = None,
    shutdown: ShutdownController | None = None,
) -> list[SheetJobResult]:
    load_env_file()
    # Re-read scheduler knobs after .env load (module defaults may predate env).
    global SCHEDULER_MODE, CHUNK_SIZE, ALPHASPREAD_CHUNK_SIZE
    global BASELINE_SLEEP_SECONDS, ALPHASPREAD_SLEEP_SECONDS
    global ALPHASPREAD_JOB_TIMEOUT_SECONDS, GURUFOCUS_SLEEP_SECONDS
    global VALUEINVESTING_SLEEP_SECONDS, VALUEINVESTING_SLEEP_JITTER_SECONDS
    global YAHOO_SLEEP_SECONDS
    SCHEDULER_MODE = _scheduler_mode()
    CHUNK_SIZE = max(1, _env_int("RESILIENT_CHUNK_SIZE", 10))
    ALPHASPREAD_CHUNK_SIZE = max(1, _env_int("RESILIENT_ALPHASPREAD_CHUNK_SIZE", 1))
    BASELINE_SLEEP_SECONDS = _sleep_default(
        "RESILIENT_SLEEP_BASELINE_SECONDS", legacy=8, round_robin=4
    )
    ALPHASPREAD_SLEEP_SECONDS = _env_int("RESILIENT_ALPHASPREAD_SLEEP_SECONDS", 8)
    ALPHASPREAD_JOB_TIMEOUT_SECONDS = _env_int(
        "RESILIENT_ALPHASPREAD_JOB_TIMEOUT_SECONDS", 120
    )
    GURUFOCUS_SLEEP_SECONDS = _sleep_default(
        "RESILIENT_GURUFOCUS_SLEEP_SECONDS", legacy=90, round_robin=20
    )
    VALUEINVESTING_SLEEP_SECONDS = _sleep_default(
        "RESILIENT_VALUEINVESTING_SLEEP_SECONDS", legacy=25, round_robin=10
    )
    VALUEINVESTING_SLEEP_JITTER_SECONDS = _sleep_default(
        "RESILIENT_VALUEINVESTING_SLEEP_JITTER_SECONDS", legacy=10, round_robin=5
    )
    YAHOO_SLEEP_SECONDS = _sleep_default(
        "RESILIENT_YAHOO_SLEEP_SECONDS", legacy=8, round_robin=3
    )

    client = DcfSheetClient()
    writes = SheetWriteBuffer(client, pending_path=_PENDING_WRITES_PATH)
    await writes.replay_pending_file()
    jobs = _filter_sheet_jobs(await retry_sync("collect_jobs", client.collect_jobs), sources, limit)
    if not jobs:
        logger.info("[sheet] Nothing to process")
        return []

    clear_jobs = [job for job in jobs if job.clear_only]
    scrape_jobs = [job for job in jobs if not job.clear_only]
    logger.info(
        "[sheet] Processing %d jobs (%d scrape, %d non-US clear) dry_run=%s "
        "scheduler=%s chunk_size=%d alphaspread_chunk=%d "
        "sleeps=as:%ds gf:%ds vi:%ds baseline:%ds yahoo:%ds as_timeout:%ds",
        len(jobs),
        len(scrape_jobs),
        len(clear_jobs),
        dry_run,
        SCHEDULER_MODE,
        CHUNK_SIZE,
        ALPHASPREAD_CHUNK_SIZE,
        ALPHASPREAD_SLEEP_SECONDS,
        GURUFOCUS_SLEEP_SECONDS,
        VALUEINVESTING_SLEEP_SECONDS,
        BASELINE_SLEEP_SECONDS,
        YAHOO_SLEEP_SECONDS,
        ALPHASPREAD_JOB_TIMEOUT_SECONDS,
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

    if shutdown is not None:
        shutdown.progress.total_jobs = len(jobs)

    if dry_run:
        return [SheetJobResult(job=job, result=None, written=False) for job in jobs]

    proxy_pool = await build_validated_pool()
    results: list[SheetJobResult] = []
    job_cache: dict[tuple[str, SourceKey], TickerResult] = {}
    guard = AlphaSpreadGuard()
    _sync_guard_progress(shutdown, guard)

    try:
        for job in clear_jobs:
            if shutdown is not None and shutdown.is_requested():
                break
            if shutdown is not None:
                shutdown.note_job(row=job.row, ticker=job.ticker, source=job.source, batch="clear")
            results.append(await _apply_clear_only_job(writes, job))
            if shutdown is not None:
                shutdown.note_job_completed()

        if SCHEDULER_MODE == "legacy_batch":
            await _run_legacy_batch_scrape(
                scrape_jobs,
                proxy_pool,
                writes,
                results,
                job_cache,
                shutdown=shutdown,
                guard=guard,
            )
        else:
            await _run_round_robin_scrape(
                scrape_jobs,
                proxy_pool,
                writes,
                results,
                job_cache,
                shutdown=shutdown,
                guard=guard,
            )
    finally:
        await writes.close()

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
        description="Resilient async collector with direct Playwright scraping",
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
