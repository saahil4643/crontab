#!/usr/bin/env python3
"""
Standalone test script to verify GuruFocus login, session saving, and DCF metric extraction.

Usage:
    python demo_gurufocus.py --ticker AAPL
    python demo_gurufocus.py --email user@example.com --password secret --ticker MSFT
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning, module=r"google.*")
warnings.filterwarnings("ignore", message=r".*Python version 3\.9.*", category=FutureWarning)

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

from resilient_collector.env_loader import load_env_file
from resilient_collector.extractors import (
    _extract_gurufocus_dcf_fair_value,
    _extract_gurufocus_earnings_term,
    _gurufocus_wait_out_cloudflare,
)
from resilient_collector.gurufocus_session import (
    _EMAIL_SELECTORS,
    _LAUNCH_ARGS,
    _PASSWORD_SELECTORS,
    _SUBMIT_SELECTORS,
    _USER_AGENTS,
    _VIEWPORTS,
    _apply_stealth,
    _first_visible,
    _looks_logged_in,
    _wait_out_cloudflare,
)

XVFB_MARKER = "GF_XVFB_ACTIVE"
STORAGE_STATE_PATH = ROOT / ".gurufocus-storage-state.json"


def should_use_xvfb(no_xvfb: bool) -> bool:
    return (
        not no_xvfb
        and not sys.platform.startswith("win")
        and not os.environ.get("DISPLAY")
        and os.environ.get(XVFB_MARKER) != "1"
    )


def reexec_with_xvfb() -> None:
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        print("[!] Warning: xvfb-run not found. Proceeding without xvfb.")
        return
    env = os.environ.copy()
    env[XVFB_MARKER] = "1"
    command = [xvfb, "-a", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    print("[*] Re-executing with xvfb-run for virtual display...")
    os.execvpe(command[0], command, env)


async def test_gurufocus(
    ticker: str,
    email: str,
    password: str,
    headless: bool,
    force_login: bool,
) -> int:
    print("=" * 65)
    print(f"[*] Testing GuruFocus Scraper for Ticker: {ticker}")
    print(f"[*] Headless mode: {headless}")
    print(f"[*] Credentials email: {email if email else '(None provided)'}")
    print(f"[*] Session file: {STORAGE_STATE_PATH.name} (exists={STORAGE_STATE_PATH.exists()})")
    print("=" * 65)

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=headless,
            args=list(_LAUNCH_ARGS),
        )

        context_kwargs: dict = {
            "user_agent": _USER_AGENTS[0],
            "viewport": _VIEWPORTS[0],
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

        # Check existing session if available and not forcing re-login
        if STORAGE_STATE_PATH.exists() and not force_login:
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
            print(f"[+] Reusing saved storage state: {STORAGE_STATE_PATH.name}")

        context: BrowserContext = await browser.new_context(**context_kwargs)
        page: Page = await context.new_page()
        await _apply_stealth(page)
        await page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

        # Step 1: Check login status / perform login
        is_logged_in = False
        if STORAGE_STATE_PATH.exists() and not force_login:
            print("[*] Checking if saved session is still authenticated...")
            try:
                await page.goto("https://www.gurufocus.com/", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                if await _looks_logged_in(page):
                    print("[✓] Already logged in with saved session!")
                    is_logged_in = True
                else:
                    print("[-] Saved session expired or not logged in.")
            except Exception as e:
                print(f"[!] Warm-up check failed: {e}")

        if not is_logged_in:
            if not email or not password:
                print("[!] ERROR: No GuruFocus email/password provided!")
                print("    Please set GURUFOCUS_EMAIL and GURUFOCUS_PASSWORD in .env or pass --email / --password.")
                await browser.close()
                return 1

            print(f"[*] Navigating to login page: https://www.gurufocus.com/login")
            try:
                response = await page.goto("https://www.gurufocus.com/login", wait_until="domcontentloaded", timeout=40000)
                status = response.status if response else 0
                title = await page.title()
                print(f"[*] Login page response status: {status} | Title: {title}")

                if "access denied" in title.lower():
                    print("[X] CLOUDFLARE ACCESS DENIED on login page.")
                    screenshot_path = LOG_DIR / "gf_login_blocked.png"
                    await page.screenshot(path=str(screenshot_path))
                    print(f"    Saved screenshot to {screenshot_path}")
                    await browser.close()
                    return 2

                cf_cleared = await _wait_out_cloudflare(page, timeout_ms=30000)
                if not cf_cleared:
                    print("[X] Cloudflare challenge did not clear on login page.")
                    await browser.close()
                    return 2

                email_input = await _first_visible(page, _EMAIL_SELECTORS, timeout_ms=10000)
                password_input = await _first_visible(page, _PASSWORD_SELECTORS, timeout_ms=10000)

                if email_input is None or password_input is None:
                    print("[!] Login fields not immediately found; checking homepage login modal...")
                    await page.goto("https://www.gurufocus.com/", wait_until="domcontentloaded", timeout=30000)
                    login_link = page.locator('a:has-text("Log in"), a:has-text("Login"), a:has-text("Sign in")').first
                    if await login_link.count() > 0:
                        await login_link.click(timeout=5000)
                        await page.wait_for_timeout(2000)
                    email_input = await _first_visible(page, _EMAIL_SELECTORS, timeout_ms=8000)
                    password_input = await _first_visible(page, _PASSWORD_SELECTORS, timeout_ms=8000)

                if email_input is None or password_input is None:
                    print("[X] FAILED: Could not locate login form inputs (email/password).")
                    screenshot_path = LOG_DIR / "gf_missing_login_fields.png"
                    await page.screenshot(path=str(screenshot_path))
                    print(f"    Saved debug screenshot to: {screenshot_path}")
                    await browser.close()
                    return 1

                print("[*] Entering email and password...")
                await email_input.fill("")
                await email_input.type(email, delay=30)
                await password_input.fill("")
                await password_input.type(password, delay=30)

                submit_btn = await _first_visible(page, _SUBMIT_SELECTORS, timeout_ms=5000)
                if submit_btn is not None:
                    print("[*] Clicking Login button...")
                    await submit_btn.click()
                else:
                    print("[*] Pressing Enter on password field...")
                    await password_input.press("Enter")

                print("[*] Waiting for authentication...")
                await page.wait_for_timeout(4000)
                await _wait_out_cloudflare(page, timeout_ms=20000)

                current_url = page.url or ""
                current_title = await page.title()
                print(f"[*] Post-login URL: {current_url} | Title: {current_title}")

                if await _looks_logged_in(page) or "/login" not in current_url:
                    print("[✓] Login SUCCESSFUL!")
                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                    print(f"[✓] Saved authenticated session to {STORAGE_STATE_PATH.name}")
                    is_logged_in = True
                else:
                    print("[!] Login verification was ambiguous. Checking if we can fetch data anyway...")
            except Exception as e:
                print(f"[!] Login process error: {e}")

        # Step 2: Navigate to target ticker DCF page
        dcf_url = f"https://www.gurufocus.com/stock/{ticker}/dcf?search={ticker}"
        print(f"\n[*] Navigating to primary DCF page: {dcf_url}")
        metric = None

        try:
            resp = await page.goto(dcf_url, wait_until="domcontentloaded", timeout=40000)
            status = resp.status if resp else 0
            title = await page.title()
            print(f"[*] Response status: {status} | Title: {title}")

            if "access denied" in title.lower():
                print("[X] Cloudflare Access Denied on DCF page.")
            else:
                await _wait_out_cloudflare(page, timeout_ms=20000)
                res = await _extract_gurufocus_dcf_fair_value(page, ticker)
                if res and res.metric:
                    metric = res.metric
                    print(f"[✓] Extracted Fair Value via DCF table: ${metric} (method: {res.method})")
        except Exception as e:
            print(f"[!] Primary DCF page fetch error: {e}")

        # Step 3: If not found, try fallback term URL
        if not metric:
            term_url = f"https://www.gurufocus.com/term/intrinsic-value-dcf-earnings-based/{ticker}"
            print(f"\n[*] Primary table not found. Trying fallback term page: {term_url}")
            try:
                resp = await page.goto(term_url, wait_until="domcontentloaded", timeout=40000)
                status = resp.status if resp else 0
                title = await page.title()
                print(f"[*] Fallback response status: {status} | Title: {title}")

                if "access denied" in title.lower():
                    print("[X] Cloudflare Access Denied on fallback term page.")
                else:
                    await _wait_out_cloudflare(page, timeout_ms=20000)
                    res = await _extract_gurufocus_earnings_term(page, ticker)
                    if res and res.metric:
                        metric = res.metric
                        print(f"[✓] Extracted Intrinsic Value via fallback term page: ${metric} (method: {res.method})")
            except Exception as e:
                print(f"[!] Fallback term page fetch error: {e}")

        # Final Result
        print("\n" + "=" * 65)
        if metric:
            print(f"[SUCCESS] Ticker: {ticker} | GuruFocus Fair Value: ${metric}")
            # Ensure storage state is saved
            await context.storage_state(path=str(STORAGE_STATE_PATH))
            print(f"[✓] Session saved to {STORAGE_STATE_PATH.name} for future cron runs!")
            exit_code = 0
        else:
            print(f"[FAILURE] Could not extract Fair Value for {ticker}.")
            screenshot_path = LOG_DIR / f"gf_{ticker}_failed.png"
            await page.screenshot(path=str(screenshot_path))
            print(f"[*] Saved debug screenshot to: {screenshot_path}")
            exit_code = 1
        print("=" * 65)

        await browser.close()
        return exit_code


def main() -> None:
    load_env_file()

    parser = argparse.ArgumentParser(description="Test GuruFocus login and DCF extraction.")
    parser.add_argument("--ticker", default="AAPL", help="Stock ticker to test (default: AAPL)")
    parser.add_argument("--email", default="", help="GuruFocus email (defaults to GURUFOCUS_EMAIL env)")
    parser.add_argument("--password", default="", help="GuruFocus password (defaults to GURUFOCUS_PASSWORD env)")
    parser.add_argument("--headless", action="store_true", help="Force headless browser")
    parser.add_argument("--no-xvfb", action="store_true", help="Do not wrap with xvfb-run on Linux")
    parser.add_argument("--force-login", action="store_true", help="Ignore saved storage state and force fresh login")
    args = parser.parse_args()

    email = args.email or os.environ.get("GURUFOCUS_EMAIL", "").strip()
    password = args.password or os.environ.get("GURUFOCUS_PASSWORD", "").strip()

    if should_use_xvfb(args.no_xvfb):
        reexec_with_xvfb()

    # Headless decision: on Linux with xvfb, headed is preferred to bypass bot walls
    headless = args.headless
    if not args.headless and not sys.platform.startswith("win"):
        # If running under xvfb or DISPLAY is available, headed (headless=False) is best for Cloudflare
        headless = False

    exit_code = asyncio.run(
        test_gurufocus(
            ticker=args.ticker.strip().upper(),
            email=email,
            password=password,
            headless=headless,
            force_login=args.force_login,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
