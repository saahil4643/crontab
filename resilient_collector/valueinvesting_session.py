"""Shared Playwright browser for ValueInvesting free-tier view budgeting."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import USER_AGENT
from resilient_collector.proxy_pool import ProxyConfig

if TYPE_CHECKING:
    from playwright.async_api import Playwright

logger = logging.getLogger(__name__)

VALUEINVESTING_VIEWS_PER_IDENTITY = int(
    os.environ.get("RESILIENT_VALUEINVESTING_VIEWS_PER_IDENTITY", "5")
)
VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS = int(
    os.environ.get("RESILIENT_VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS", "20")
)

_USER_AGENTS: tuple[str, ...] = (
    USER_AGENT,
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
)

_VIEWPORTS: tuple[dict[str, int], ...] = (
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def browser_headless() -> bool:
    return _env_bool("RESILIENT_HEADLESS", False)


def _identity_user_agent(identity_serial: int) -> str:
    return _USER_AGENTS[identity_serial % len(_USER_AGENTS)]


def _identity_viewport(identity_serial: int) -> dict[str, int]:
    return _VIEWPORTS[identity_serial % len(_VIEWPORTS)]


async def _apply_stealth(page: Page) -> None:
    try:
        from playwright_stealth import stealth_async

        await stealth_async(page)
        return
    except Exception:
        pass

    try:
        from playwright_stealth import stealth

        import asyncio

        result = stealth(page)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("playwright_stealth unavailable or failed: %s", exc)


class ValueInvestingSession:
    """
  Reuse one browser for up to N ticker page loads, then rotate identity.

  N defaults to 5 (ValueInvesting free-tier views per visitor).
  """

    def __init__(self, proxy_pool: list[ProxyConfig]) -> None:
        self._proxy_pool = proxy_pool
        self._max_views = max(1, VALUEINVESTING_VIEWS_PER_IDENTITY)
        self._views_used = 0
        self._identity_serial = 0
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._proxy_label: str | None = None

    @property
    def views_used(self) -> int:
        return self._views_used

    @property
    def identity_serial(self) -> int:
        return self._identity_serial

    @property
    def proxy_label(self) -> str:
        return self._proxy_label or "unknown"

    def needs_rotation(self) -> bool:
        return self._page is None or self._views_used >= self._max_views

    async def ensure_page(self) -> Page:
        if self.needs_rotation():
            await self.rotate_identity()
        assert self._page is not None
        return self._page

    async def rotate_identity(self) -> None:
        had_session = self._page is not None
        await self.close()
        self._identity_serial += 1
        self._views_used = 0
        if had_session and VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS > 0:
            logger.info(
                "[valueinvesting] Post-rotation throttle pause: %d seconds",
                VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS,
            )
            await asyncio.sleep(VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS)
        proxy = random.choice(self._proxy_pool)
        self._proxy_label = proxy.label

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=browser_headless())
        context_kwargs: dict = {
            "user_agent": _identity_user_agent(self._identity_serial),
            "viewport": _identity_viewport(self._identity_serial),
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        proxy_config = proxy.playwright_config
        if proxy_config:
            context_kwargs["proxy"] = proxy_config
        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        await _apply_stealth(self._page)
        logger.info(
            "[valueinvesting] New browser identity #%d route=%s views_budget=%d",
            self._identity_serial,
            self._proxy_label,
            self._max_views,
        )

    def record_view(self) -> None:
        self._views_used += 1
        logger.info(
            "[valueinvesting] Identity #%d used %d/%d free views",
            self._identity_serial,
            self._views_used,
            self._max_views,
        )

    async def close(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception as exc:
                logger.debug("[valueinvesting] page.close: %s", exc)
            self._page = None

        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:
                logger.debug("[valueinvesting] context.close: %s", exc)
            self._context = None

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.debug("[valueinvesting] browser.close: %s", exc)
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug("[valueinvesting] playwright.stop: %s", exc)
            self._playwright = None
