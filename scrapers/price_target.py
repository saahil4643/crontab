"""Price target low / average / high from Yahoo Finance quoteSummary."""

from __future__ import annotations

import json
import logging
import re
from typing import Literal, Optional

from playwright.sync_api import Page

from config import USER_AGENT
from scrapers.base import BaseScraper
from scrapers.extract import normalize_numeric_string

logger = logging.getLogger(__name__)

PriceTargetKind = Literal["low", "avg", "high"]

_TARGET_KEYS: dict[PriceTargetKind, str] = {
    "low": "targetLowPrice",
    "avg": "targetMeanPrice",
    "high": "targetHighPrice",
}
_TARGET_CACHE: dict[str, dict[PriceTargetKind, str]] = {}


def _coerce_target(value: object) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("raw", value.get("fmt"))
    if value is None:
        return None
    return normalize_numeric_string(str(value))


def _extract_targets_from_text(text: str) -> dict[PriceTargetKind, str]:
    if not text:
        return {}
    normalized = re.sub(r"\s+", " ", text)
    match = re.search(
        r"Analyst\s+Price\s+Targets\s+"
        r"(-?[\d,.]+)\s+"
        r"(-?[\d,.]+)\s+Average\s+"
        r"-?[\d,.]+\s+Current\s+"
        r"(-?[\d,.]+)\s+High",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return {}
    low = normalize_numeric_string(match.group(1))
    avg = normalize_numeric_string(match.group(2))
    high = normalize_numeric_string(match.group(3))
    targets: dict[PriceTargetKind, str] = {}
    if low is not None:
        targets["low"] = low
    if avg is not None:
        targets["avg"] = avg
    if high is not None:
        targets["high"] = high
    return targets


def _fetch_yahoo_targets(page: Page, ticker: str) -> dict[PriceTargetKind, str]:
    normalized_ticker = ticker.strip().upper()
    if normalized_ticker in _TARGET_CACHE:
        return _TARGET_CACHE[normalized_ticker]

    quote_url = f"https://finance.yahoo.com/quote/{normalized_ticker}/"
    page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
    })
    page.goto(quote_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    try:
        text_targets = _extract_targets_from_text(page.locator("body").inner_text(timeout=10_000))
        if text_targets:
            _TARGET_CACHE[normalized_ticker] = text_targets
            logger.info(
                "[Price Target] %s Yahoo page targets low=%r avg=%r high=%r",
                normalized_ticker,
                text_targets.get("low"),
                text_targets.get("avg"),
                text_targets.get("high"),
            )
            return text_targets
    except Exception as exc:
        logger.debug("Yahoo page text target extraction failed for %s: %s", normalized_ticker, exc)

    payload_text = page.evaluate(
        """async (ticker) => {
            let crumb = window?.YAHOO?.context?.user?.crumb || window?.YAHOO?.context?.crumb || '';
            if (!crumb) {
                const html = document.documentElement.innerHTML || '';
                const match = html.match(/"crumb"\s*:\s*"([^"]+)"/);
                crumb = match ? match[1] : '';
            }
            if (!crumb) {
                const crumbResponse = await fetch(
                    'https://query1.finance.yahoo.com/v1/test/getcrumb',
                    { credentials: 'include' }
                );
                if (!crumbResponse.ok) {
                    throw new Error(`crumb status ${crumbResponse.status}`);
                }
                crumb = (await crumbResponse.text()).trim();
            }
            const params = new URLSearchParams({
                modules: 'financialData',
                formatted: 'false',
                lang: 'en-US',
                region: 'US',
                crumb,
            });
            const url = `https://query1.finance.yahoo.com/v10/finance/quoteSummary/${ticker}?${params.toString()}`;
            const response = await fetch(url, { credentials: 'include' });
            if (!response.ok) {
                throw new Error(`quoteSummary status ${response.status}`);
            }
            return await response.text();
        }""",
        normalized_ticker,
    )
    payload = json.loads(payload_text)

    result = ((payload.get("quoteSummary") or {}).get("result") or [])
    financial_data = (result[0] if result else {}).get("financialData") or {}
    targets: dict[PriceTargetKind, str] = {}
    for kind, key in _TARGET_KEYS.items():
        target = _coerce_target(financial_data.get(key))
        if target is not None:
            targets[kind] = target

    _TARGET_CACHE[normalized_ticker] = targets
    logger.info(
        "[Price Target] %s Yahoo targets low=%r avg=%r high=%r",
        normalized_ticker,
        targets.get("low"),
        targets.get("avg"),
        targets.get("high"),
    )
    return targets


class PriceTargetScraper(BaseScraper):
    """
    Fills T / U / V on Missing Value using Yahoo quoteSummary financialData.
    """

    def __init__(self, kind: PriceTargetKind) -> None:
        self.kind = kind
        self.source_key = f"price_target_{kind}"
        self.source_label = f"Price Target {kind.upper()}"

    def scrape(self, page: Page, ticker: str) -> Optional[str]:
        ticker = ticker.strip().upper()
        logger.info("[%s] Yahoo quoteSummary %s", self.source_label, ticker)
        try:
            targets = _fetch_yahoo_targets(page, ticker)
        except Exception as exc:
            logger.warning("[%s] %s failed to fetch Yahoo targets: %s", self.source_label, ticker, exc)
            return None

        value = targets.get(self.kind)
        if value is None:
            logger.warning("[%s] %s target unavailable", self.source_label, ticker)
        return value
