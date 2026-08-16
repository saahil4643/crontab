"""Retry transient Playwright navigation failures (proxy stalls, ERR_TIMED_OUT)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from playwright.async_api import Page, Response

logger = logging.getLogger(__name__)

_TRANSIENT_NAV_MARKERS: tuple[str, ...] = (
    "ERR_TIMED_OUT",
    "ERR_CONNECTION",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_PROXY",
    "ERR_NETWORK",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_INTERNET_DISCONNECTED",
    "NS_ERROR_NET_TIMEOUT",
    "Timeout",
    "timeout exceeded",
    "net::err_",
    # Local browser bootstrap issues — keep existing sheet values
    "Executable doesn't exist",
    "BrowserType.launch",
    "playwright install",
    "is not a valid selector",
    "restrictive or unstable status",
)


def scrape_nav_retry_attempts() -> int:
    return max(1, int(os.environ.get("SCRAPE_NAV_RETRY_ATTEMPTS", "3")))


def scrape_nav_retry_base_seconds() -> float:
    return max(0.0, float(os.environ.get("SCRAPE_NAV_RETRY_BASE_SECONDS", "5")))


def is_transient_nav_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    text = str(exc).lower()
    return any(marker.lower() in text for marker in _TRANSIENT_NAV_MARKERS)


def is_retryable_primary_failure(exc: BaseException) -> bool:
    from resilient_collector.valueinvesting_errors import ValueInvestingLimitError

    if isinstance(exc, ValueInvestingLimitError):
        return False
    msg = str(exc).lower()
    if "signup wall" in msg or "pricing/login" in msg or "view limit" in msg:
        return False
    if is_transient_nav_error(exc):
        return True
    if "metric not found" in msg and is_transient_nav_error(RuntimeError(msg)):
        return True
    return False


async def goto_with_retry(
    page: Page,
    url: str,
    *,
    ticker: str,
    label: str,
    wait_until: str = "domcontentloaded",
    timeout_ms: int,
    attempts: int | None = None,
    base_seconds: float | None = None,
) -> Response | None:
    """Navigate with retries on transient network / timeout errors."""
    max_attempts = attempts if attempts is not None else scrape_nav_retry_attempts()
    retry_base = base_seconds if base_seconds is not None else scrape_nav_retry_base_seconds()
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_transient_nav_error(exc):
                raise
            delay = retry_base * attempt
            logger.warning(
                "[%s] %s goto failed attempt %d/%d (%s); retrying in %.1fs",
                ticker,
                label,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    return None


async def interruptible_sleep_seconds(
    seconds: float,
    shutdown: Any | None = None,
) -> bool:
    """Sleep up to *seconds*; return False if shutdown was requested."""
    if seconds <= 0:
        return True
    if shutdown is None:
        await asyncio.sleep(seconds)
        return True
    from worker_shutdown import interruptible_sleep

    return await interruptible_sleep(seconds, shutdown, label="scrape retry pause")
