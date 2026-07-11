"""Shared Playwright browser for GuruFocus to keep cookies and reduce 403s."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import GURUFOCUS_HOME_URL, USER_AGENT
from resilient_collector.proxy_pool import ProxyConfig

if TYPE_CHECKING:
    from playwright.async_api import Playwright

logger = logging.getLogger(__name__)

GURUFOCUS_VIEWS_PER_IDENTITY = int(
    os.environ.get("RESILIENT_GURUFOCUS_VIEWS_PER_IDENTITY", "15")
)
GURUFOCUS_POST_ROTATION_SLEEP_SECONDS = int(
    os.environ.get("RESILIENT_GURUFOCUS_POST_ROTATION_SLEEP_SECONDS", "30")
)
GURUFOCUS_WARMUP_WAIT_MS = int(os.environ.get("RESILIENT_GURUFOCUS_WARMUP_WAIT_MS", "4000"))
DIRECT_TIMEOUT_MS = int(os.environ.get("DIRECT_SCRAPE_TIMEOUT_MS", "30000"))

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

_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def browser_headless() -> bool:
    """
    GuruFocus Cloudflare often blocks headless Chromium.
    Prefer RESILIENT_GURUFOCUS_HEADLESS when set; otherwise inherit RESILIENT_HEADLESS.
    """
    if os.environ.get("RESILIENT_GURUFOCUS_HEADLESS") is not None:
        return _env_bool("RESILIENT_GURUFOCUS_HEADLESS", False)
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

        result = stealth(page)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("playwright_stealth unavailable or failed: %s", exc)


class GuruFocusSession:
    """
    Reuse one browser for many GuruFocus tickers so Cloudflare cookies persist.

    Rotates identity after N page loads (or on caller request after 403).
    """

    def __init__(self, proxy_pool: list[ProxyConfig]) -> None:
        self._proxy_pool = proxy_pool
        self._max_views = max(1, GURUFOCUS_VIEWS_PER_IDENTITY)
        self._views_used = 0
        self._identity_serial = 0
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._proxy_label: str | None = None
        self._warmed = False

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
        self._warmed = False
        if had_session and GURUFOCUS_POST_ROTATION_SLEEP_SECONDS > 0:
            logger.info(
                "[gurufocus] Post-rotation throttle pause: %d seconds",
                GURUFOCUS_POST_ROTATION_SLEEP_SECONDS,
            )
            await asyncio.sleep(GURUFOCUS_POST_ROTATION_SLEEP_SECONDS)

        proxy = random.choice(self._proxy_pool)
        self._proxy_label = proxy.label

        self._playwright = await async_playwright().start()
        headless = browser_headless()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=headless,
                args=list(_LAUNCH_ARGS),
            )
        except Exception as launch_exc:
            # Headed launch needs Xvfb/DISPLAY on servers; fall back to headless.
            if not headless and "Missing X server" in str(launch_exc):
                logger.warning(
                    "[gurufocus] Headed launch failed (%s); falling back to headless",
                    launch_exc,
                )
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=list(_LAUNCH_ARGS),
                )
                headless = True
            else:
                await self.close()
                raise
        logger.info("[gurufocus] Chromium launched headless=%s", headless)
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
        await self._page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
        })
        logger.info(
            "[gurufocus] New browser identity #%d route=%s views_budget=%d",
            self._identity_serial,
            self._proxy_label,
            self._max_views,
        )
        await self.warm_up()

    async def warm_up(self) -> None:
        """Visit homepage once so Cloudflare cookies can settle before DCF pages."""
        if self._page is None or self._warmed:
            return
        url = GURUFOCUS_HOME_URL
        logger.info("[gurufocus] Warm-up navigation: %s", url)
        try:
            response = await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=DIRECT_TIMEOUT_MS,
            )
            status = response.status if response is not None else None
            logger.info("[gurufocus] Warm-up status=%s", status)
            await self._page.wait_for_timeout(GURUFOCUS_WARMUP_WAIT_MS)
            self._warmed = True
        except Exception as exc:
            logger.warning("[gurufocus] Warm-up failed: %s", exc)

    def record_view(self) -> None:
        self._views_used += 1
        logger.info(
            "[gurufocus] Identity #%d used %d/%d views",
            self._identity_serial,
            self._views_used,
            self._max_views,
        )

    async def close(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception as exc:
                logger.debug("[gurufocus] page.close: %s", exc)
            self._page = None

        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:
                logger.debug("[gurufocus] context.close: %s", exc)
            self._context = None

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.debug("[gurufocus] browser.close: %s", exc)
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug("[gurufocus] playwright.stop: %s", exc)
            self._playwright = None
        self._warmed = False
