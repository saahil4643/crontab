#!/usr/bin/env python3
"""
Test JSON capture only — no Google Sheets.

Saves every JSON response Playwright sees while loading a ticker page, plus
optional HTML and a trial DCF extraction using config key hints.

Usage:
  python scripts/capture_json.py --source gurufocus --ticker AAPL
  python scripts/capture_json.py --source gurufocus --ticker AAPL --try-extract
  python scripts/capture_json.py --source alphaspread --ticker MSFT --try-extract
  python scripts/capture_json.py --source valueinvesting --ticker GOOGL --headed
  python scripts/capture_json.py --list-sources

When deploying, use run.py for full sheet integration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Project root on sys.path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from capture_urls import CAPTURE_SOURCES, build_source_url
from config import (
    CAPTURE_TEST_OUTPUT_DIR,
    CAPTURE_TEST_POST_LOAD_WAIT_MS,
    CAPTURE_TEST_TIMEOUT_MS,
    CAPTURE_TEST_WAIT_UNTIL,
    DCF_JSON_KEY_HINTS,
    DCF_TEXT_REGEX_PATTERNS,
    GURUFOCUS_CAPTURE_POST_LOAD_MS,
    GURUFOCUS_DCF_URL,
    GURUFOCUS_STORAGE_STATE,
    HEADLESS,
    PRICE_TARGET_JSON_KEY_HINTS,
)
from logging_setup import setup_logging
from scrapers.base import PlaywrightSession
from scrapers.capture import NetworkCapture, save_debug_capture
from scrapers.extract import extract_from_json_payloads, extract_from_text

import logging

logger = logging.getLogger(__name__)


def _make_navigate(page, source: str, url: str, wait_until: str, timeout_ms: int):
    load_until = "commit" if source == "gurufocus" else wait_until

    def navigate() -> None:
        page.goto(url, wait_until=load_until, timeout=timeout_ms)
        if source == "gurufocus":
            from scrapers.gurufocus import prepare_dcf_calculator_page

            prepare_dcf_calculator_page(page)

    return navigate


def capture_one(
    source: str,
    ticker: str,
    *,
    headless: bool,
    security_type: str,
    save_html: bool,
    output_dir: Path,
    wait_until: str,
    timeout_ms: int,
    post_load_wait_ms: int,
) -> tuple[Path, Optional[str]]:
    url = build_source_url(source, ticker, security_type=security_type)
    logger.info("Capturing JSON for %s %s", source, ticker)
    logger.info("URL: %s", url)

    live_dcf: Optional[str] = None
    storage = GURUFOCUS_STORAGE_STATE if source == "gurufocus" else None
    with PlaywrightSession(headless=headless, storage_state=storage) as session:
        page = session.new_page()
        cap = NetworkCapture(page)
        navigate = _make_navigate(page, source, url, wait_until, timeout_ms)
        effective_post_load = (
            GURUFOCUS_CAPTURE_POST_LOAD_MS if source == "gurufocus" else post_load_wait_ms
        )
        capture = cap.collect_during(
            navigate,
            post_load_wait_ms=effective_post_load,
            save_html=save_html,
        )
        if source == "gurufocus":
            from scrapers.gurufocus import wait_for_fair_value_on_calculator

            live_dcf = wait_for_fair_value_on_calculator(page)

    save_debug_capture(capture, output_dir, source=source, ticker=ticker)
    base = output_dir / f"{source}_{''.join(c if c.isalnum() else '_' for c in ticker.upper())}"
    return base, live_dcf


def print_summary(
    base: Path,
    source: str,
    ticker: str,
    try_extract: bool,
    live_dcf: Optional[str] = None,
) -> None:
    meta_path = base.with_suffix(".meta.json")
    if not meta_path.is_file():
        print(f"No meta file at {meta_path}")
        return

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    count = meta.get("json_count", 0)
    urls = meta.get("json_urls", [])

    print()
    print("=" * 60)
    print(f"Capture complete: {source} / {ticker}")
    print("=" * 60)
    print(f"Final page URL: {meta.get('url', '')}")
    print(f"JSON responses saved: {count}")
    print(f"Output directory: {base.parent}")
    print()
    print("Files:")
    print(f"  {meta_path.name}")
    for i in range(count):
        print(f"  {base.name}_json_{i}.json")
    html_path = base.with_suffix(".html")
    if html_path.is_file():
        print(f"  {html_path.name}")

    if urls:
        print()
        print("JSON response URLs:")
        for i, u in enumerate(urls):
            print(f"  [{i}] {u}")

    if source == "gurufocus":
        print()
        print("GuruFocus DCF Calculator Fair Value (live DOM):")
        print(f"  URL: {GURUFOCUS_DCF_URL.format(ticker=ticker)}")
        print(f"  Fair Value: {live_dcf!r}")
        if live_dcf is None:
            print(
                "  Tip: Fair Value can take up to ~2 min to render. "
                "Re-run or use --headed to watch the page load."
            )

    if try_extract:
        print()
        print("Trial extraction (from saved files, no sheet write):")
        from scrapers.capture import CapturedPayload

        payloads = []
        for i in range(count):
            p = base.parent / f"{base.name}_json_{i}.json"
            if p.is_file():
                payloads.append(
                    CapturedPayload(url=urls[i] if i < len(urls) else "", body=json.loads(p.read_text()))
                )
        hints = DCF_JSON_KEY_HINTS
        if source == "price_target":
            hints = PRICE_TARGET_JSON_KEY_HINTS["avg"]

        json_val = extract_from_json_payloads(payloads, hints)
        text_val = None
        if html_path.is_file():
            text_val = extract_from_text(
                html_path.read_text(encoding="utf-8")[:200_000],
                DCF_TEXT_REGEX_PATTERNS,
            )

        print(f"  From JSON keys: {json_val!r}")
        print(f"  From regex on HTML: {text_val!r}")

    print()
    print("Next: open *_json_*.json and search for dcf / fairValue / target keys.")
    print("Then update DCF_JSON_KEY_HINTS in config.py if needed.")
    print("Deploy test: python run.py --dry-run   (requires client sheet shared)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture network JSON from finance sites (no Google Sheets)",
    )
    parser.add_argument(
        "--source",
        choices=sorted(CAPTURE_SOURCES),
        help="Which site to open",
    )
    parser.add_argument("--ticker", default="AAPL", help="Stock symbol (default: AAPL)")
    parser.add_argument(
        "--security-type",
        default="nasdaq",
        help="Alpha Spread path segment (default: nasdaq)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=CAPTURE_TEST_OUTPUT_DIR,
        help=f"Where to write JSON files (default: {CAPTURE_TEST_OUTPUT_DIR})",
    )
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--no-html", action="store_true", help="Skip saving .html file")
    parser.add_argument(
        "--try-extract",
        action="store_true",
        help="Run trial DCF extraction on captured JSON using config key hints",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="Print available sources and exit",
    )
    args = parser.parse_args()

    setup_logging()

    if args.list_sources:
        print("Available sources:")
        for s in sorted(CAPTURE_SOURCES):
            try:
                u = build_source_url(s, "AAPL")
            except Exception:
                u = "(url builder error)"
            print(f"  {s:16} example: {u}")
        return 0

    if not args.source:
        parser.error("--source is required (or use --list-sources)")

    headless = False if args.headed else HEADLESS

    try:
        base, live_dcf = capture_one(
            args.source,
            args.ticker,
            headless=headless,
            security_type=args.security_type,
            save_html=not args.no_html,
            output_dir=args.output_dir,
            wait_until=CAPTURE_TEST_WAIT_UNTIL,
            timeout_ms=CAPTURE_TEST_TIMEOUT_MS,
            post_load_wait_ms=CAPTURE_TEST_POST_LOAD_WAIT_MS,
        )
        print_summary(
            base,
            args.source,
            args.ticker.upper(),
            args.try_extract,
            live_dcf=live_dcf,
        )
    except Exception as exc:
        logger.exception("Capture failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
