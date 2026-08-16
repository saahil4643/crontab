"""Shared Playwright browser for GuruFocus to keep cookies and reduce 403s."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from pathlib import Path
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
GURUFOCUS_LOGIN_TIMEOUT_MS = int(os.environ.get("GURUFOCUS_LOGIN_TIMEOUT_MS", "45000"))
GURUFOCUS_CF_WAIT_MS = int(os.environ.get("RESILIENT_GURUFOCUS_CF_WAIT_MS", "45000"))
DIRECT_TIMEOUT_MS = int(os.environ.get("DIRECT_SCRAPE_TIMEOUT_MS", "30000"))

GURUFOCUS_EMAIL = os.environ.get("GURUFOCUS_EMAIL", "").strip()
GURUFOCUS_PASSWORD = os.environ.get("GURUFOCUS_PASSWORD", "").strip()
GURUFOCUS_LOGIN_URL = os.environ.get(
    "GURUFOCUS_LOGIN_URL",
    "https://www.gurufocus.com/login",
).strip()
GURUFOCUS_STORAGE_STATE = Path(
    os.environ.get("GURUFOCUS_STORAGE_STATE", ".gurufocus-storage-state.json")
).expanduser()

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

_EMAIL_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[id*="email" i]',
    'input[placeholder*="email" i]',
    'input[autocomplete="username"]',
    'input[autocomplete="email"]',
)
_PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
    'input[id*="password" i]',
    'input[autocomplete="current-password"]',
)
_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button:has-text("Sign In")',
    'a:has-text("Log in")',
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def browser_headless(*, force_headed: bool = False) -> bool:
    """
    Resolve GuruFocus headless mode.

    RESILIENT_GURUFOCUS_HEADLESS:
      - auto / session / hybrid: headed until .gurufocus-storage-state.json exists,
        then headless with that session (escalate to headed on 403).
      - 0/false: always headed
      - 1/true: always headless
    """
    if force_headed:
        return False

    raw = (os.environ.get("RESILIENT_GURUFOCUS_HEADLESS") or "").strip().lower()
    if raw in {"auto", "session", "hybrid"}:
        if GURUFOCUS_STORAGE_STATE.exists():
            return True
        logger.info(
            "[gurufocus] auto mode: no saved session yet → headed bootstrap"
        )
        return False
    if raw != "":
        return raw in {"1", "true", "yes", "on"}
    if GURUFOCUS_EMAIL and GURUFOCUS_PASSWORD:
        return False
    return _env_bool("RESILIENT_HEADLESS", False)


def credentials_configured() -> bool:
    return bool(GURUFOCUS_EMAIL and GURUFOCUS_PASSWORD)


def _identity_user_agent(identity_serial: int) -> str:
    # Keep UA stable when using a saved login session — rotating UA can invalidate CF cookies.
    if credentials_configured() or GURUFOCUS_STORAGE_STATE.exists():
        return _USER_AGENTS[0]
    return _USER_AGENTS[identity_serial % len(_USER_AGENTS)]


def _identity_viewport(identity_serial: int) -> dict[str, int]:
    if credentials_configured() or GURUFOCUS_STORAGE_STATE.exists():
        return _VIEWPORTS[0]
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


async def _wait_out_cloudflare(page: Page, timeout_ms: int = GURUFOCUS_CF_WAIT_MS) -> bool:
    try:
        title = (await page.title() or "").lower()
    except Exception:
        title = ""
    content_hint = ""
    try:
        content_hint = (await page.content())[:2500].lower()
    except Exception:
        pass
    challenged = (
        "attention required" in title
        or "just a moment" in title
        or "cf-browser-verification" in content_hint
        or ("cdn-cgi" in content_hint and "cloudflare" in content_hint)
    )
    if not challenged:
        return True

    logger.info("[gurufocus] Cloudflare challenge detected; waiting up to %dms", timeout_ms)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        await page.wait_for_timeout(1500)
        try:
            title = (await page.title() or "").lower()
        except Exception:
            continue
        if "attention required" not in title and "just a moment" not in title:
            logger.info("[gurufocus] Cloudflare challenge cleared; title=%r", await page.title())
            return True
    logger.warning("[gurufocus] Cloudflare challenge did not clear")
    return False


async def _first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 2500):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


async def _looks_logged_in(page: Page) -> bool:
    """Heuristic: require account chrome — do not treat CF cookies as login."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "/login" in url:
        return False

    logout = page.locator(
        'a:has-text("Log Out"), a:has-text("Logout"), a:has-text("Sign Out"), '
        'button:has-text("Log Out"), button:has-text("Logout")'
    )
    try:
        if await logout.count() > 0:
            return True
    except Exception:
        pass

    account = page.locator(
        'a[href*="my-account"], a[href*="member"], a[href*="/user/"], '
        '[class*="user-avatar"], a:has-text("My Account"), a:has-text("Account")'
    )
    try:
        if await account.count() > 0:
            return True
    except Exception:
        pass

    # Explicit app auth cookies only (never Cloudflare clearance alone)
    try:
        cookies = await page.context.cookies("https://www.gurufocus.com")
        names = {c.get("name", "").lower() for c in cookies}
        authish = {
            n
            for n in names
            if n.startswith(("gf_", "guru", "laravel_session", "remember_web"))
            or n in {"sessionid", "auth_token", "access_token"}
        }
        if authish:
            email_field = await _first_visible(page, _EMAIL_SELECTORS, timeout_ms=500)
            if email_field is None:
                return True
    except Exception:
        pass
    return False


class GuruFocusSession:
    """
    Reuse one browser for many GuruFocus tickers so Cloudflare cookies persist.

    Optionally logs in with GURUFOCUS_EMAIL / GURUFOCUS_PASSWORD and persists
    Playwright storage_state for reuse across runs.
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
        self._logged_in = False
        self._force_headed = False
        self._last_headless: bool | None = None

    @property
    def views_used(self) -> int:
        return self._views_used

    @property
    def identity_serial(self) -> int:
        return self._identity_serial

    @property
    def proxy_label(self) -> str:
        return self._proxy_label or "unknown"

    @property
    def is_headless(self) -> bool | None:
        return self._last_headless

    def needs_rotation(self) -> bool:
        return self._page is None or self._views_used >= self._max_views

    async def ensure_page(self) -> Page:
        if self.needs_rotation():
            await self.rotate_identity()
        assert self._page is not None
        return self._page

    async def escalate_to_headed(self) -> None:
        """After a headless 403, reopen headed (login/CF) and refresh storage_state."""
        logger.warning(
            "[gurufocus] Headless session blocked; escalating to headed to refresh session"
        )
        await self.rotate_identity(force_headed=True)

    async def rotate_identity(self, *, force_headed: bool | None = None) -> None:
        had_session = self._page is not None
        await self._persist_storage_state()
        await self.close()
        self._identity_serial += 1
        self._views_used = 0
        self._warmed = False
        self._logged_in = False
        if force_headed is not None:
            self._force_headed = force_headed
        if had_session and GURUFOCUS_POST_ROTATION_SLEEP_SECONDS > 0:
            logger.info(
                "[gurufocus] Post-rotation throttle pause: %d seconds",
                GURUFOCUS_POST_ROTATION_SLEEP_SECONDS,
            )
            await asyncio.sleep(GURUFOCUS_POST_ROTATION_SLEEP_SECONDS)

        proxy = random.choice(self._proxy_pool)
        self._proxy_label = proxy.label

        self._playwright = await async_playwright().start()
        headless = browser_headless(force_headed=self._force_headed)
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
        self._last_headless = headless
        if headless and GURUFOCUS_STORAGE_STATE.exists():
            logger.info(
                "[gurufocus] Chromium launched headless=%s (reusing saved session)",
                headless,
            )
        else:
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

        if GURUFOCUS_STORAGE_STATE.exists():
            context_kwargs["storage_state"] = str(GURUFOCUS_STORAGE_STATE)
            logger.info(
                "[gurufocus] Restoring saved session from %s",
                GURUFOCUS_STORAGE_STATE,
            )

        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        await _apply_stealth(self._page)
        await self._page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
        })
        if credentials_configured():
            logger.info(
                "[gurufocus] Login credentials configured for %s",
                GURUFOCUS_EMAIL,
            )
        logger.info(
            "[gurufocus] New browser identity #%d route=%s views_budget=%d",
            self._identity_serial,
            self._proxy_label,
            self._max_views,
        )
        await self.warm_up()
        await self.ensure_logged_in()

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
            await _wait_out_cloudflare(self._page)
            await self._page.wait_for_timeout(GURUFOCUS_WARMUP_WAIT_MS)
            self._warmed = True
        except Exception as exc:
            logger.warning("[gurufocus] Warm-up failed: %s", exc)

    async def ensure_logged_in(self) -> bool:
        """Log in when credentials are set and session is not already authenticated."""
        if self._page is None:
            return False
        if self._logged_in:
            return True

        if await _looks_logged_in(self._page):
            self._logged_in = True
            logger.info("[gurufocus] Already logged in (saved session or cookies)")
            await self._persist_storage_state()
            return True

        if not credentials_configured():
            logger.info(
                "[gurufocus] No GURUFOCUS_EMAIL/PASSWORD set; continuing anonymous"
            )
            return False

        ok = await self._perform_login()
        self._logged_in = ok
        if ok:
            await self._persist_storage_state()
        return ok

    async def _perform_login(self) -> bool:
        assert self._page is not None
        page = self._page
        logger.info("[gurufocus] Navigating to login page")
        try:
            await page.goto(
                GURUFOCUS_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=DIRECT_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.warning("[gurufocus] Login navigation failed: %s", exc)
            return False

        if not await _wait_out_cloudflare(page):
            logger.warning("[gurufocus] Login blocked by Cloudflare")
            return False

        email_input = await _first_visible(page, _EMAIL_SELECTORS, timeout_ms=8000)
        password_input = await _first_visible(page, _PASSWORD_SELECTORS, timeout_ms=8000)
        if email_input is None or password_input is None:
            # Sometimes login is a modal on home
            logger.info("[gurufocus] Login fields not on /login; trying homepage modal")
            try:
                await page.goto(
                    GURUFOCUS_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=DIRECT_TIMEOUT_MS,
                )
                await _wait_out_cloudflare(page)
                login_link = page.locator(
                    'a:has-text("Log in"), a:has-text("Login"), a:has-text("Sign in")'
                ).first
                if await login_link.count() > 0:
                    await login_link.click(timeout=5000)
                    await page.wait_for_timeout(1500)
            except Exception as exc:
                logger.debug("[gurufocus] homepage login open failed: %s", exc)
            email_input = await _first_visible(page, _EMAIL_SELECTORS, timeout_ms=8000)
            password_input = await _first_visible(page, _PASSWORD_SELECTORS, timeout_ms=8000)

        if email_input is None or password_input is None:
            logger.warning("[gurufocus] Could not find login form fields")
            return False

        try:
            await email_input.fill(GURUFOCUS_EMAIL)
            await password_input.fill(GURUFOCUS_PASSWORD)
            submit = await _first_visible(page, _SUBMIT_SELECTORS, timeout_ms=5000)
            if submit is not None:
                await submit.click()
            else:
                await password_input.press("Enter")
            await page.wait_for_load_state("domcontentloaded", timeout=GURUFOCUS_LOGIN_TIMEOUT_MS)
            await page.wait_for_timeout(2000)
            await _wait_out_cloudflare(page)
        except Exception as exc:
            logger.warning("[gurufocus] Login submit failed: %s", exc)
            return False

        if await _looks_logged_in(page):
            logger.info("[gurufocus] Login succeeded")
            return True

        # Soft success: left /login without an obvious error banner
        url = (page.url or "").lower()
        if "/login" not in url:
            logger.info("[gurufocus] Login likely succeeded (left login URL)")
            return True

        logger.warning("[gurufocus] Login did not appear to succeed")
        return False

    async def _persist_storage_state(self) -> None:
        if self._context is None:
            return
        if not (credentials_configured() or self._logged_in):
            return
        try:
            GURUFOCUS_STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=str(GURUFOCUS_STORAGE_STATE))
            logger.info("[gurufocus] Saved session state to %s", GURUFOCUS_STORAGE_STATE)
        except Exception as exc:
            logger.warning("[gurufocus] Failed to save session state: %s", exc)

    def record_view(self) -> None:
        self._views_used += 1
        logger.info(
            "[gurufocus] Identity #%d used %d/%d views",
            self._identity_serial,
            self._views_used,
            self._max_views,
        )

    async def close(self) -> None:
        await self._persist_storage_state()

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
        self._logged_in = False
