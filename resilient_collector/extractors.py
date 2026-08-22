from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any

from playwright.async_api import Page

from config import (
    ALPHA_SPREAD_URL,
    DCF_JSON_KEY_HINTS,
    DCF_TEXT_REGEX_PATTERNS,
    GURUFOCUS_DCF_URL,
    GURUFOCUS_EARNINGS_TERM_URL,
    JSON_URL_HINTS_ALPHASPREAD,
    PRICE_TARGET_JSON_KEY_HINTS,
    PRICE_TARGET_TEXT_REGEX,
    SourceKey,
    USER_AGENT,
    VALUE_INVESTING_PATHS,
    VALUE_INVESTING_VALUATION_URL,
    YAHOO_CRUMB_URL,
    YAHOO_QUOTE_SUMMARY_URL,
    YAHOO_QUOTE_URL,
)
from models import ScrapeJob
from resilient_collector.nav_retry import goto_with_retry
from resilient_collector.valueinvesting_errors import ValueInvestingLimitError
from scrapers.extract import extract_from_json_payloads, extract_from_text, normalize_numeric_string

logger = logging.getLogger(__name__)
DIRECT_TIMEOUT_MS = int(os.environ.get("DIRECT_SCRAPE_TIMEOUT_MS", "30000"))
VALUEINVESTING_TABLE_WAIT_MS = int(os.environ.get("VALUEINVESTING_TABLE_WAIT_MS", "35000"))
VALUEINVESTING_CARD_WAIT_MS = int(os.environ.get("VALUEINVESTING_CARD_WAIT_MS", "8000"))

_FAIR_PRICE_LABELS = ("fair price", "fair value")
_VALUEINVESTING_BLOCKED_MARKERS = (
    "regional plan",
    "global plan",
    "start free trial",
    "create your account",
)
_VALUEINVESTING_LIMIT_MARKERS = (
    "view limit",
    "views remaining",
    "you have reached",
    "sign up to continue",
    "subscribe to continue",
    "unlock this content",
    "free views",
)
_VALUEINVESTING_PLAN_PRICE = "14.99"
_VALUEINVESTING_SUMMARY_TABLE_SELECTOR = "table.each_summary"
_VALUEINVESTING_FAIR_PRICE_ROW_XPATH = (
    "//table[contains(@class,'each_summary')]"
    "//tr[td[normalize-space()='Fair Price']]"
)


@dataclass
class PrimaryExtractionResult:
    metric: str
    method: str


_DOLLAR_RE = re.compile(
    r"\$\s*(-?[\d]{1,3}(?:,[\d]{3})*(?:\.\d+)?|-?[\d]+(?:\.\d+)?)"
)

_GURUFOCUS_EARNINGS_LABEL = "Intrinsic Value: DCF (Earnings Based)"
_GURUFOCUS_EARNINGS_PATTERNS = (
    re.compile(
        r"Intrinsic\s+Value\s*:\s*DCF\s*\(\s*Earnings\s+Based\s*\)\s*:\s*\$?\s*(-?[\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Intrinsic\s+Value\s*:\s*DCF\s*\(\s*Earnings\s+Based\s*\)\s+is\s+\$?\s*(-?[\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"intrinsic\s+value\s+calculated\s+from\s+the\s+Discounted\s+Earnings\s+model\s+is\s+\$?\s*(-?[\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
)
_GURUFOCUS_DCF_TABLE_SELECTOR = "div.stock-dcf-table"
_GURUFOCUS_FAIR_VALUE_LABEL_RE = re.compile(r"^Fair\s+Value\b", re.IGNORECASE)
# Value cell in the Fair Value dcf-table-row (right column).
_GURUFOCUS_FAIR_VALUE_CELL_SELECTOR = (
    "div.el-col.text-right, div.text-right.el-col, "
    "div.fs-x-large.fw-bolder.text-right, div.text-right"
)
# Reject ambiguous blobs; a value cell is short (e.g. "$ 8.48").
_GURUFOCUS_DOLLAR_CELL_MAX_LEN = 64
GURUFOCUS_TABLE_WAIT_MS = int(os.environ.get("GURUFOCUS_TABLE_WAIT_MS", "20000"))

_SOURCE_LABELS: dict[SourceKey, str] = {
    "alphaspread": "Alpha Spread DCF fair value",
    "valueinvesting": "ValueInvesting DCF fair value",
    "gurufocus": "GuruFocus intrinsic value DCF earnings based",
    "price_target_low": "Yahoo Finance analyst low price target",
    "price_target_avg": "Yahoo Finance analyst average price target",
    "price_target_high": "Yahoo Finance analyst high price target",
}

_PRICE_TARGET_KIND: dict[SourceKey, str] = {
    "price_target_low": "low",
    "price_target_avg": "avg",
    "price_target_high": "high",
}

_VALUEINVESTING_TEXT_PATTERNS = (
    r"Discounted\s+Cash\s+Flow\s*\(DCF\)\s+valuation\b.*?\bis\s+(-?[\d,.]+)\s+USD\b",
    r"\bDCF\s+valuation\b.*?\bis\s+(-?[\d,.]+)\s+USD\b",
    r"\bFair\s+Value\b[\s:]*(-?[\d,.]+)\s+USD\b",
    r"(-?[\d,.]+)\s+USD\s+Fair\s+Value\b",
)

_PRICE_TEXT_PATTERN = re.compile(
    r"Analyst\s+Price\s+Targets\s+"
    r"(-?[\d,.]+)\s+"
    r"(-?[\d,.]+)\s+Average\s+"
    r"-?[\d,.]+\s+Current\s+"
    r"(-?[\d,.]+)\s+High",
    re.IGNORECASE,
)


def source_metric_label(source: SourceKey) -> str:
    return _SOURCE_LABELS.get(source, source)


async def _capture_response_json(response: Any, hints: tuple[str, ...], payloads: list[Any]) -> None:
    try:
        url = response.url.lower()
        if hints and not any(hint.lower() in url for hint in hints):
            return
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type and not url.endswith(".json"):
            return
        payloads.append(await response.json())
    except Exception:
        return


async def _navigate_collect(
    page: Page,
    url: str,
    ticker: str,
    source_label: str,
    *,
    hints: tuple[str, ...] = (),
    timeout: int = 40_000,
) -> tuple[list[Any], str]:
    payloads: list[Any] = []

    def handler(response: Any) -> None:
        asyncio.create_task(_capture_response_json(response, hints, payloads))

    page.on("response", handler)
    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
    })
    response = await goto_with_retry(
        page,
        url,
        ticker=ticker,
        label=source_label,
        wait_until="domcontentloaded",
        timeout_ms=timeout,
    )
    if response is None:
        raise RuntimeError(f"{source_label} returned no response")
    status = response.status
    logger.info("[%s] %s status=%s", ticker, source_label, status)
    if status in {403, 429} or status >= 500:
        raise RuntimeError(f"{source_label} restrictive or unstable status: {status}")
        
    # Handle Yahoo Consent Redirect if detected
    current_url = page.url.lower()
    if "consent.yahoo.com" in current_url or "guce.yahoo.com" in current_url:
        logger.info("[%s] Yahoo consent redirect detected. Handling...", ticker)
        try:
            scroll_btn = page.locator('#scroll-down-btn').first
            if await scroll_btn.count() > 0:
                await scroll_btn.click(timeout=3000)
                await page.wait_for_timeout(1000)
            
            accept_btn = page.locator('button.accept-all, button[name="agree"]').first
            if await accept_btn.count() > 0:
                await accept_btn.click(force=True, timeout=5000)
                logger.info("[%s] Yahoo consent accepted. Waiting for redirect...", ticker)
                # Wait for redirection back to the target page / domain (allow up to 45s due to proxy latency)
                await page.wait_for_url(
                    lambda u: "consent" not in u.lower() and "guce" not in u.lower(),
                    timeout=45000
                )
                logger.info("[%s] Redirect completed successfully back to: %s", ticker, page.url)
        except Exception as e:
            logger.warning("[%s] Failed to resolve Yahoo consent redirect: %s", ticker, e)

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)
    try:
        text = await page.locator("body").inner_text(timeout=10_000)
    except Exception:
        text = await page.content()
    return payloads, text


async def _dismiss_overlays(page: Page) -> None:
    for label in ("Accept", "Accept All", "I agree", "Got it", "Agree"):
        try:
            await page.get_by_role("button", name=label).click(timeout=1500)
            break
        except Exception:
            continue
    await _dismiss_gurufocus_signup_modal(page)


async def _dismiss_gurufocus_signup_modal(page: Page) -> None:
    """Close GuruFocus 'Track like a Guru' / signup dialogs without logging in."""
    close_selectors = (
        "button.el-dialog__headerbtn",
        ".el-dialog__headerbtn",
        ".el-message-box__headerbtn",
        "button[aria-label='Close']",
        ".el-overlay .el-dialog__close",
    )
    for selector in close_selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                continue
            await loc.click(timeout=1500)
            logger.debug("Dismissed GuruFocus overlay via %s", selector)
            await page.wait_for_timeout(400)
            return
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
    except Exception:
        pass


_ADR_EXCHANGE_MAP = {
    "PBR": "nyse",
    "BABA": "nyse",
    "NVO": "nyse",
    "SAP": "nyse",
    "BP": "nyse",
    "DEO": "nyse",
    "INFY": "nyse",
    "ENB": "nyse",
    "VALE": "nyse",
    "EZPW": "nasdaq",
    "GCT": "nasdaq",
    "AMCX": "nasdaq",
    "RGP": "nasdaq",
    "KRT": "nasdaq",
    "MCFT": "nasdaq",
}

def get_exchange_for_ticker(ticker: str) -> str:
    return _ADR_EXCHANGE_MAP.get(ticker.upper(), "nasdaq")


async def _alpha_css(page: Page) -> str | None:
    # 1. Livewire DCF base-case control (colon in attr must be escaped for CSS)
    loc = page.locator('a[wire\\:key*="dcf-base-case"] div.detail').first
    if await loc.count() > 0:
        val = await loc.inner_text(timeout=2000)
        if val.strip():
            return val.strip()

    # XPath fallback if CSS attribute escaping differs across engines
    xpath_loc = page.locator(
        "xpath=//a[contains(@*[name()='wire:key'], 'dcf-base-case')]//div[contains(@class,'detail')]"
    ).first
    if await xpath_loc.count() > 0:
        val = await xpath_loc.inner_text(timeout=2000)
        if val.strip():
            return val.strip()

    # 2. Next check the legacy selector as a fallback for the summary page
    summary_loc = page.locator("a.intrinsic-value-dcf-link div.detail").first
    if await summary_loc.count() > 0:
        val = await summary_loc.inner_text(timeout=2000)
        if val.strip():
            return val.strip()

    # 3. Target the first occurrence of pure DCF valuation element on dcf page fallback
    loc = page.locator('div[class*="valuation-value"], div[class*="valuation-scenario-value"]').first
    if await loc.count() > 0:
        return await loc.inner_text(timeout=5000)
    important = page.locator("tr.important td.right.aligned.restriction-sensitive-data").first
    if await important.count() > 0:
        return await important.inner_text(timeout=5000)
    return None


async def _extract_alpha_spread(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    exchange = get_exchange_for_ticker(job.ticker)
    url = ALPHA_SPREAD_URL.format(security_type=exchange, ticker=job.ticker.lower())
    logger.info("[%s] Alpha Spread primary navigation: %s", job.ticker, url)
    payloads, text = await _navigate_collect(
        page,
        url,
        job.ticker,
        "Alpha Spread",
        hints=JSON_URL_HINTS_ALPHASPREAD,
        timeout=DIRECT_TIMEOUT_MS,
    )
    await _dismiss_overlays(page)

    # Anti-Redirect Safety Guardrail
    current_url = page.url.lower()
    foreign_exchanges = ["bovespa", "tse", "lse", "xetra"]
    if any(marker in current_url for marker in foreign_exchanges):
        logger.warning(
            "[%s] Foreign exchange redirect detected (%s). Forcing back to strict %s path: %s",
            job.ticker, current_url, exchange.upper(), url
        )
        payloads, text = await _navigate_collect(
            page,
            url,
            job.ticker,
            "Alpha Spread",
            hints=JSON_URL_HINTS_ALPHASPREAD,
            timeout=DIRECT_TIMEOUT_MS,
        )
        await _dismiss_overlays(page)

    css_value = await _alpha_css(page)
    normalized = normalize_numeric_string(css_value or "")
    if normalized:
        return PrimaryExtractionResult(metric=normalized, method="css")
    json_value = extract_from_json_payloads(payloads, DCF_JSON_KEY_HINTS)
    if json_value:
        return PrimaryExtractionResult(metric=json_value, method="json")
    text_value = extract_from_text(text, DCF_TEXT_REGEX_PATTERNS)
    if text_value:
        return PrimaryExtractionResult(metric=text_value, method="text")
    raise RuntimeError("Alpha Spread metric not found")


def _label_matches_fair_price(label: str) -> bool:
    lowered = label.strip().lower()
    return any(marker in lowered for marker in _FAIR_PRICE_LABELS)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def valueinvesting_paths() -> tuple[str, ...]:
    """Default: one URL per ticker to preserve free-tier view budget."""
    if _env_bool("RESILIENT_VALUEINVESTING_MULTI_PATH", False):
        return VALUE_INVESTING_PATHS
    return (VALUE_INVESTING_PATHS[0],)


def _valid_valueinvesting_metric(value: str | None) -> str | None:
    if not value or value == _VALUEINVESTING_PLAN_PRICE:
        return None
    if " - " in value:
        return None
    if value.endswith("%"):
        return None
    return value


async def _valueinvesting_metric_from_norm(locator) -> str | None:
    if await locator.count() == 0:
        return None
    raw = await locator.inner_text(timeout=5000)
    return _valid_valueinvesting_metric(normalize_numeric_string(raw))


async def _valueinvesting_extract_from_summary_table(page: Page, ticker: str) -> tuple[str, str] | None:
    table = page.locator(_VALUEINVESTING_SUMMARY_TABLE_SELECTOR).first
    try:
        await table.wait_for(state="visible", timeout=VALUEINVESTING_TABLE_WAIT_MS)
    except Exception:
        logger.info(
            "[%s] ValueInvesting %s not visible within %dms",
            ticker,
            _VALUEINVESTING_SUMMARY_TABLE_SELECTOR,
            VALUEINVESTING_TABLE_WAIT_MS,
        )
        return None

    fair_row = page.locator(f"xpath={_VALUEINVESTING_FAIR_PRICE_ROW_XPATH}").first
    if await fair_row.count() == 0:
        logger.info("[%s] ValueInvesting Fair Price row not found in each_summary", ticker)
        return None

    cells = fair_row.locator("td")
    cell_count = await cells.count()
    if cell_count < 3:
        logger.info("[%s] ValueInvesting Fair Price row has %d cells (expected >= 3)", ticker, cell_count)
        return None

    cell_text = await cells.nth(2).inner_text(timeout=5000)
    value = _valid_valueinvesting_metric(normalize_numeric_string(cell_text))
    if value:
        logger.info("[%s] ValueInvesting fair value via each_summary table -> %s", ticker, value)
        return value, "each_summary_table"
    return None


async def _valueinvesting_extract_legacy_cards(page: Page, ticker: str) -> tuple[str, str] | None:
    card_locator = page.locator(".price_square").first
    try:
        await card_locator.wait_for(state="visible", timeout=VALUEINVESTING_CARD_WAIT_MS)
    except Exception:
        logger.debug("[%s] ValueInvesting .price_square not visible within timeout", ticker)
        return None

    labels = page.locator(".tiny")
    label_count = await labels.count()
    for index in range(label_count):
        label_text = (await labels.nth(index).inner_text(timeout=3000) or "").strip()
        if not _label_matches_fair_price(label_text):
            continue
        norm = labels.nth(index).locator("xpath=..").locator(".norm").first
        value = await _valueinvesting_metric_from_norm(norm)
        if value:
            logger.info("[%s] ValueInvesting fair value via card label %r -> %s", ticker, label_text, value)
            return value, "card_label"

    squares = page.locator(".price_square")
    square_count = await squares.count()
    for index in range(square_count):
        square = squares.nth(index)
        square_text = (await square.inner_text(timeout=3000) or "").strip()
        if not _label_matches_fair_price(square_text):
            continue
        value = await _valueinvesting_metric_from_norm(square.locator(".norm").first)
        if value:
            logger.info("[%s] ValueInvesting fair value via price_square -> %s", ticker, value)
            return value, "card_square"

    return None


async def _valueinvesting_extract_fair_price(page: Page, ticker: str) -> tuple[str, str] | None:
    extracted = await _valueinvesting_extract_from_summary_table(page, ticker)
    if extracted:
        return extracted
    return await _valueinvesting_extract_legacy_cards(page, ticker)


async def _valueinvesting_body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return await page.content()


async def _valueinvesting_page_limit_reached(page: Page) -> bool:
    try:
        body = (await _valueinvesting_body_text(page)).lower()
    except Exception:
        return False
    return any(marker in body for marker in _VALUEINVESTING_LIMIT_MARKERS)


async def _valueinvesting_page_blocked(page: Page) -> bool:
    try:
        body = (await _valueinvesting_body_text(page)).lower()
    except Exception:
        return False
    if await page.locator(_VALUEINVESTING_SUMMARY_TABLE_SELECTOR).count() > 0:
        return False
    if "discounted cash flow" in body or "dcf valuation" in body:
        return False
    return any(marker in body for marker in _VALUEINVESTING_BLOCKED_MARKERS)


async def extract_valueinvesting_metric(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    """
    Navigate to ValueInvesting and read Fair Price from each_summary (Selected column).

    Raises ValueInvestingLimitError when the free view / signup wall is detected.
    """
    ticker = job.ticker.strip().upper()
    last_error: Exception | None = None

    for path in valueinvesting_paths():
        url = VALUE_INVESTING_VALUATION_URL.format(ticker=ticker, path=path)
        logger.info("[%s] ValueInvesting primary navigation: %s", ticker, url)
        try:
            await page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": USER_AGENT,
            })
            response = await goto_with_retry(
                page,
                url,
                ticker=ticker,
                label="ValueInvesting",
                wait_until="domcontentloaded",
                timeout_ms=DIRECT_TIMEOUT_MS,
            )
            if response is None:
                raise RuntimeError("ValueInvesting returned no response")
            status = response.status
            logger.info("[%s] ValueInvesting status=%s path=%s", ticker, status, path)
            if status in {403, 429} or status >= 500:
                raise RuntimeError(f"ValueInvesting restrictive or unstable status: {status}")

            await _dismiss_overlays(page)

            if await _valueinvesting_page_limit_reached(page):
                raise ValueInvestingLimitError("ValueInvesting free view limit or signup wall")

            if await _valueinvesting_page_blocked(page):
                raise RuntimeError("ValueInvesting pricing/login page")

            extracted = await _valueinvesting_extract_fair_price(page, ticker)
            if extracted:
                metric, method = extracted
                return PrimaryExtractionResult(metric=metric, method=method)

            text = await _valueinvesting_body_text(page)
            text_value = extract_from_text(text, _VALUEINVESTING_TEXT_PATTERNS)
            if text_value and text_value != _VALUEINVESTING_PLAN_PRICE:
                logger.info("[%s] ValueInvesting fair value via text regex -> %s", ticker, text_value)
                return PrimaryExtractionResult(metric=text_value, method="text")
        except ValueInvestingLimitError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning("[%s] ValueInvesting path %s failed: %s", ticker, path, exc)
            continue

    detail = str(last_error) if last_error else "Fair Price metric not found on any valuation page"
    raise RuntimeError(f"ValueInvesting metric not found ({detail})")


async def _extract_valueinvesting(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    return await extract_valueinvesting_metric(page, job)


def _extract_gurufocus_earnings_value(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return None
    for pattern in _GURUFOCUS_EARNINGS_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        value = normalize_numeric_string(match.group(1))
        if value:
            return value
    return None


def _parse_gurufocus_dollar_cell(text: str) -> str | None:
    """Parse a single value cell like '$ 8.48'. Reject large or ambiguous blobs."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized or len(normalized) > _GURUFOCUS_DOLLAR_CELL_MAX_LEN:
        return None
    # Value cell must be essentially only the dollar/number (no extra labels).
    if not re.fullmatch(
        r"\$?\s*-?[\d,]+(?:\.\d+)?\s*",
        normalized,
    ):
        return None
    matches = list(_DOLLAR_RE.finditer(normalized))
    if len(matches) == 1:
        return normalize_numeric_string(matches[0].group(1))
    stripped = normalized.replace("$", "").replace(",", "").replace(" ", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
        return normalize_numeric_string(stripped)
    return None


def _is_gurufocus_fair_value_label(label: str) -> bool:
    """True for the DCF 'Fair Value' tip label; false for 'Saved Fair Values'."""
    normalized = re.sub(r"\s+", " ", label or "").strip()
    if not normalized:
        return False
    if "saved" in normalized.lower():
        return False
    if re.search(r"fair\s+values\b", normalized, re.IGNORECASE):
        return False
    return bool(_GURUFOCUS_FAIR_VALUE_LABEL_RE.match(normalized))


async def _extract_gurufocus_dcf_fair_value(page: Page, ticker: str) -> PrimaryExtractionResult | None:
    """Read Fair Value from the labeled dcf-table-row value cell only."""
    await _dismiss_gurufocus_signup_modal(page)
    table = page.locator(_GURUFOCUS_DCF_TABLE_SELECTOR).first
    try:
        await table.wait_for(state="visible", timeout=GURUFOCUS_TABLE_WAIT_MS)
    except Exception as exc:
        logger.debug("[%s] GuruFocus DCF table not visible: %s", ticker, exc)
        return None

    rows = table.locator("div.dcf-table-row")
    try:
        row_count = await rows.count()
    except Exception as exc:
        logger.debug("[%s] GuruFocus DCF rows unavailable: %s", ticker, exc)
        return None

    fair_row = None
    for index in range(row_count):
        row = rows.nth(index)
        tips = row.locator(".tips").first
        try:
            if await tips.count() == 0:
                continue
            label_text = await tips.inner_text(timeout=3000)
        except Exception:
            continue
        if _is_gurufocus_fair_value_label(label_text):
            fair_row = row
            break

    if fair_row is None:
        logger.debug("[%s] GuruFocus Fair Value row not found in stock-dcf-table", ticker)
        return None

    value_cell = fair_row.locator(_GURUFOCUS_FAIR_VALUE_CELL_SELECTOR).first
    try:
        if await value_cell.count() == 0:
            logger.debug("[%s] GuruFocus Fair Value value cell not found", ticker)
            return None
        cell_text = await value_cell.inner_text(timeout=5000)
    except Exception as exc:
        logger.debug("[%s] GuruFocus Fair Value cell text failed: %s", ticker, exc)
        return None

    value = _parse_gurufocus_dollar_cell(cell_text)
    if value:
        logger.info(
            "[%s] GuruFocus Fair Value via dcf table -> %s (cell=%r)",
            ticker,
            value,
            re.sub(r"\s+", " ", cell_text or "").strip(),
        )
        return PrimaryExtractionResult(metric=value, method="dcf_table_fair_value")
    logger.debug("[%s] GuruFocus Fair Value cell did not parse: %r", ticker, cell_text)
    return None


async def _extract_gurufocus_earnings_term(page: Page, ticker: str) -> PrimaryExtractionResult | None:
    """Read Intrinsic Value from term page h1 / h1 span only (no full-page text scrape)."""
    container = page.locator("#def_body_detail_height").first
    try:
        await container.wait_for(state="attached", timeout=15_000)
    except Exception as exc:
        logger.debug("[%s] GuruFocus earnings term container not visible: %s", ticker, exc)
        return None

    try:
        heading = container.locator("h1").first
        if await heading.count() == 0:
            return None
        heading_text = await heading.inner_text(timeout=5000)
        value = _extract_gurufocus_earnings_value(heading_text)
        if value:
            logger.info("[%s] GuruFocus earnings DCF via term h1 -> %s", ticker, value)
            return PrimaryExtractionResult(metric=value, method="term_h1")

        if _GURUFOCUS_EARNINGS_LABEL.lower() in heading_text.lower():
            spans = heading.locator("span")
            for index in range(await spans.count()):
                span_text = await spans.nth(index).inner_text(timeout=3000)
                value = _parse_gurufocus_dollar_cell(span_text)
                if value:
                    logger.info(
                        "[%s] GuruFocus earnings DCF via term h1 span -> %s",
                        ticker,
                        value,
                    )
                    return PrimaryExtractionResult(metric=value, method="term_h1_span")
    except Exception as exc:
        logger.debug("[%s] GuruFocus earnings heading extract failed: %s", ticker, exc)
    return None


async def _gurufocus_wait_out_cloudflare(page: Page, ticker: str, timeout_ms: int = 25_000) -> int | None:
    """If Cloudflare interstitial is showing, wait briefly for it to clear."""
    import time

    try:
        title = (await page.title() or "").lower()
    except Exception:
        title = ""
    content_hint = ""
    try:
        content_hint = (await page.content())[:2000].lower()
    except Exception:
        pass
    challenged = (
        "attention required" in title
        or "just a moment" in title
        or "cf-browser-verification" in content_hint
        or ("cdn-cgi" in content_hint and "cloudflare" in content_hint)
    )
    if not challenged:
        return None

    logger.info("[%s] GuruFocus Cloudflare challenge detected; waiting up to %dms", ticker, timeout_ms)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        await page.wait_for_timeout(1500)
        try:
            title = (await page.title() or "").lower()
        except Exception:
            continue
        if "attention required" not in title and "just a moment" not in title:
            logger.info("[%s] GuruFocus Cloudflare challenge cleared; title=%r", ticker, await page.title())
            return 200
    logger.warning("[%s] GuruFocus Cloudflare challenge did not clear", ticker)
    return 403


async def _gurufocus_goto(page: Page, url: str, ticker: str) -> int:
    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
        "Referer": "https://www.gurufocus.com/",
    })
    response = await goto_with_retry(
        page,
        url,
        ticker=ticker,
        label="GuruFocus",
        wait_until="domcontentloaded",
        timeout_ms=DIRECT_TIMEOUT_MS,
    )
    if response is None:
        raise RuntimeError("No response from primary navigation")
    status = response.status
    logger.info("[%s] Primary status=%s", ticker, status)
    if status in {403, 429}:
        cleared = await _gurufocus_wait_out_cloudflare(page, ticker)
        if cleared == 200:
            return 200
        raise RuntimeError(f"Restrictive or unstable status: {status}")
    if status >= 500:
        raise RuntimeError(f"Restrictive or unstable status: {status}")
    return status


async def _gurufocus_goto_via_site_search(page: Page, ticker: str) -> bool:
    """Reach the DCF page from homepage search to reuse an already-cleared CF session."""
    home = "https://www.gurufocus.com/"
    logger.info("[%s] GuruFocus in-site navigation via homepage search", ticker)
    try:
        response = await page.goto(home, wait_until="domcontentloaded", timeout=DIRECT_TIMEOUT_MS)
        if response is not None and response.status in {403, 429}:
            cleared = await _gurufocus_wait_out_cloudflare(page, ticker)
            if cleared != 200:
                return False
        await page.wait_for_timeout(1500)
        await _dismiss_overlays(page)

        search = page.locator(
            "input[placeholder*='Search' i], input[type='search'], "
            "header input.el-input__inner, .gf-search input"
        ).first
        if await search.count() == 0:
            # Fallback: SPA path change from a cleared homepage session.
            await page.evaluate(
                "(ticker) => { window.location.href = `/stock/${ticker}/dcf?search=${ticker}`; }",
                ticker,
            )
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)
        else:
            await search.click(timeout=5000)
            await search.fill(ticker)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)
            link = page.locator(f"a[href*='/stock/{ticker}']").first
            if await link.count() > 0:
                await link.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1500)
            # Ensure DCF tab/path
            if "/dcf" not in (page.url or ""):
                await page.goto(
                    f"https://www.gurufocus.com/stock/{ticker}/dcf?search={ticker}",
                    wait_until="domcontentloaded",
                    timeout=DIRECT_TIMEOUT_MS,
                )

        title = (await page.title() or "").lower()
        if "attention required" in title or "just a moment" in title:
            cleared = await _gurufocus_wait_out_cloudflare(page, ticker)
            if cleared != 200:
                return False
        return "gurufocus.com" in (page.url or "") and "attention required" not in title
    except Exception as exc:
        logger.warning("[%s] GuruFocus in-site navigation failed: %s", ticker, exc)
        return False


async def extract_gurufocus_metric(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    """
    Navigate to GuruFocus stock DCF page and read Fair Value from the labeled table row.

    If the Fair Value cell is missing, try the earnings-based term page h1 only.
    Never scrapes full-page body text (avoids invented numbers).
    """
    ticker = job.ticker.strip().upper()
    dcf_url = GURUFOCUS_DCF_URL.format(ticker=ticker)
    logger.info("[%s] GuruFocus primary navigation: %s", ticker, dcf_url)

    try:
        await _gurufocus_goto(page, dcf_url, ticker)
    except RuntimeError as exc:
        if "Restrictive or unstable status" not in str(exc):
            raise
        logger.warning("[%s] Direct DCF blocked (%s); trying in-site navigation", ticker, exc)
        navigated = await _gurufocus_goto_via_site_search(page, ticker)
        if not navigated:
            raise

    await _dismiss_overlays(page)

    dcf_result = await _extract_gurufocus_dcf_fair_value(page, ticker)
    if dcf_result:
        return dcf_result

    term_url = GURUFOCUS_EARNINGS_TERM_URL.format(ticker=ticker)
    logger.info("[%s] GuruFocus fallback term navigation: %s", ticker, term_url)
    await _gurufocus_goto(page, term_url, ticker)
    await _dismiss_overlays(page)
    term_result = await _extract_gurufocus_earnings_term(page, ticker)
    if term_result:
        return term_result

    raise RuntimeError("GuruFocus Fair Value / earnings-based DCF metric was not found")


async def _extract_gurufocus(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    return await extract_gurufocus_metric(page, job)


def _price_targets_from_text(text: str) -> dict[str, str]:
    normalized = re.sub(r"\s+", " ", text or "")
    match = _PRICE_TEXT_PATTERN.search(normalized)
    if not match:
        return {}
    values = {
        "low": normalize_numeric_string(match.group(1)),
        "avg": normalize_numeric_string(match.group(2)),
        "high": normalize_numeric_string(match.group(3)),
    }
    return {key: value for key, value in values.items() if value}


async def _yahoo_dom_targets(page: Page) -> dict[str, str]:
    targets: dict[str, str] = {}
    try:
        card = page.locator('[data-testid="analyst-price-target-card"]')
        if await card.count() == 0:
            return targets
        selector_map = {
            "low": ".lowLabel .price",
            "avg": ".average .price",
            "high": ".highLabel .price",
        }
        for kind, selector in selector_map.items():
            price_element = card.locator(selector).first
            if await price_element.count() == 0:
                continue
            value = normalize_numeric_string(await price_element.inner_text())
            if value:
                targets[kind] = value
    except Exception as dom_exc:
        logger.debug("Yahoo DOM target scrape failed: %s", dom_exc)
    return targets


async def _yahoo_quote_summary_targets(page: Page, ticker: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    try:
        quote_summary_base = YAHOO_QUOTE_SUMMARY_URL.format(ticker=ticker)
        html_content = await page.content()
        crumb_match = re.search(r'"crumb"\s*:\s*"([^"]+)"', html_content)
        crumb = crumb_match.group(1) if crumb_match else None

        if not crumb:
            try:
                crumb_resp = await page.context.request.get(YAHOO_CRUMB_URL)
                if crumb_resp.ok:
                    crumb = (await crumb_resp.text()).strip()
            except Exception as exc:
                logger.debug("[%s] Failed to fetch crumb via API: %s", ticker, exc)

        params = {
            "modules": "financialData",
            "formatted": "false",
            "lang": "en-US",
            "region": "US",
        }
        if crumb:
            params["crumb"] = crumb

        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        api_url = f"{quote_summary_base}?{query_string}"
        resp = await page.context.request.get(api_url)
        if not resp.ok:
            logger.debug("[%s] Yahoo quoteSummary status: %d", ticker, resp.status)
            return targets

        payload = await resp.json()
        financial_data = (
            ((payload.get("quoteSummary") or {}).get("result") or [{}])[0]
        ).get("financialData") or {}
        key_map = {
            "low": "targetLowPrice",
            "avg": "targetMeanPrice",
            "high": "targetHighPrice",
        }
        for kind, key in key_map.items():
            raw = financial_data.get(key)
            if isinstance(raw, dict):
                raw = raw.get("raw", raw.get("fmt"))
            value = normalize_numeric_string(str(raw)) if raw is not None else None
            if value:
                targets[kind] = value
    except Exception as api_exc:
        logger.debug("[%s] Yahoo quoteSummary failed: %s", ticker, api_exc)
    return targets


async def extract_yahoo_price_targets(
    page: Page,
    ticker: str,
    *,
    kinds: set[str] | None = None,
) -> dict[str, PrimaryExtractionResult]:
    """
    One Yahoo quote navigation → low/avg/high targets.
    `kinds` limits which targets to return (e.g. {"low","avg"}).
    """
    wanted = kinds or {"low", "avg", "high"}
    symbol = ticker.strip().upper()
    url = YAHOO_QUOTE_URL.format(ticker=symbol)
    logger.info("[%s] Yahoo primary navigation (coalesced): %s", symbol, url)
    payloads, text = await _navigate_collect(
        page,
        url,
        symbol,
        "Yahoo Finance",
        hints=("quote", "summary", "finance"),
        timeout=DIRECT_TIMEOUT_MS,
    )

    found: dict[str, PrimaryExtractionResult] = {}

    for kind, value in (await _yahoo_dom_targets(page)).items():
        if kind in wanted and kind not in found:
            found[kind] = PrimaryExtractionResult(metric=value, method="dom_selector")

    text_targets = _price_targets_from_text(text)
    for kind, value in text_targets.items():
        if kind in wanted and kind not in found:
            found[kind] = PrimaryExtractionResult(metric=value, method="text")

    for kind in wanted:
        if kind in found:
            continue
        json_value = extract_from_json_payloads(
            payloads,
            PRICE_TARGET_JSON_KEY_HINTS.get(kind, PRICE_TARGET_JSON_KEY_HINTS["avg"]),
        )
        if json_value:
            found[kind] = PrimaryExtractionResult(metric=json_value, method="json")

    for kind, value in (await _yahoo_quote_summary_targets(page, symbol)).items():
        if kind in wanted and kind not in found:
            found[kind] = PrimaryExtractionResult(metric=value, method="quote_summary")

    for kind in wanted:
        if kind in found:
            continue
        text_value = extract_from_text(
            text, PRICE_TARGET_TEXT_REGEX.get(kind, PRICE_TARGET_TEXT_REGEX["avg"])
        )
        if text_value:
            found[kind] = PrimaryExtractionResult(metric=text_value, method="text_regex")

    return found


async def _extract_price_target(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    kind = _PRICE_TARGET_KIND[job.source]
    found = await extract_yahoo_price_targets(page, job.ticker, kinds={kind})
    result = found.get(kind)
    if result:
        logger.info(
            "[%s] Yahoo %s price target extracted via %s: %s",
            job.ticker,
            kind,
            result.method,
            result.metric,
        )
        return result
    raise RuntimeError(f"Yahoo {kind} price target not found")


async def extract_primary_metric_for_job(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    if job.source == "alphaspread":
        return await _extract_alpha_spread(page, job)
    if job.source == "valueinvesting":
        return await _extract_valueinvesting(page, job)
    if job.source == "gurufocus":
        return await _extract_gurufocus(page, job)
    if job.source in _PRICE_TARGET_KIND:
        return await _extract_price_target(page, job)
    raise RuntimeError(f"Unsupported source {job.source}")


async def extract_primary_metric(page: Page, ticker: str) -> str:
    job = ScrapeJob(
        row=0,
        ticker=ticker,
        source="gurufocus",
        value_col="",
        value_col_index=0,
        label="GuruFocus",
    )
    return (await extract_primary_metric_for_job(page, job)).metric
