from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import re
from urllib.parse import quote_plus
from typing import Any

from playwright.async_api import Page

from config import (
    ALPHA_SPREAD_URL,
    DCF_JSON_KEY_HINTS,
    DCF_TEXT_REGEX_PATTERNS,
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
from resilient_collector.valueinvesting_errors import ValueInvestingLimitError
from scrapers.extract import extract_from_json_payloads, extract_from_text, normalize_numeric_string

logger = logging.getLogger(__name__)
DIRECT_TIMEOUT_MS = int(os.environ.get("DIRECT_SCRAPE_TIMEOUT_MS", "10000"))
SERP_TIMEOUT_MS = int(os.environ.get("SERP_TIMEOUT_MS", "10000"))
VALUEINVESTING_TABLE_WAIT_MS = int(os.environ.get("VALUEINVESTING_TABLE_WAIT_MS", "25000"))
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
class GoogleFallbackResult:
    metric: str | None
    snippets: list[str]


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

_SNIPPET_METRIC_PATTERNS = (
    re.compile(
        r"(?:DCF|intrinsic value|fair value)[^\n\r$]{0,80}\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)[^\n\r]{0,80}(?:DCF|intrinsic value|fair value)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Fair\s*Value[\s:]*\$?\s*(-?[\d,]+\.?\d*)",
        re.IGNORECASE,
    ),
)

_GOOGLE_SNIPPET_SELECTORS = (
    "#search > div",
    "div.VwiC3b",
    "span.hgKElc",
    "div[data-sncf]",
    "div[data-content-feature='1']",
    "div.IsZvec",
    "div.kb0PBd",
    "div.g",
    "#search",
)

_GOOGLE_BLOCKED_MARKERS = (
    "our systems have detected unusual traffic",
    "sorry, but your computer or network may be sending automated queries",
    "before you continue to google search",
    "captcha",
)

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


def extract_metric_from_snippet(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    for pattern in _SNIPPET_METRIC_PATTERNS:
        match = pattern.search(normalized)
        if match:
            value = normalize_numeric_string(match.group(1))
            if value:
                return value
    return None


def _google_query(job: ScrapeJob) -> str:
    if job.source == "alphaspread":
        return f"{job.ticker} alphaspread dcf valuation"
    if job.source == "valueinvesting":
        return f"{job.ticker} valueinvesting.io dcf valuation"
    if job.source == "gurufocus":
        return f"DCF earnings based {job.ticker} guru focus"
    if job.source == "price_target_low":
        return f"{job.ticker} analyst price target low yahoo finance"
    if job.source == "price_target_avg":
        return f"{job.ticker} analyst average price target yahoo finance"
    if job.source == "price_target_high":
        return f"{job.ticker} analyst high price target yahoo finance"
    return f"{job.ticker} {job.label}"


def _patterns_for_source(source: SourceKey) -> tuple[re.Pattern[str], ...]:
    if source == "gurufocus":
        return _GURUFOCUS_EARNINGS_PATTERNS
    if source == "price_target_low":
        return (
            re.compile(r"(?:low|lowest)[^\n\r$]{0,80}\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)", re.I),
            re.compile(r"\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)[^\n\r]{0,80}(?:low|lowest)", re.I),
        )
    if source == "price_target_avg":
        return (
            re.compile(r"(?:average|mean|consensus)[^\n\r$]{0,80}\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)", re.I),
            re.compile(r"\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)[^\n\r]{0,80}(?:average|mean|consensus)", re.I),
        )
    if source == "price_target_high":
        return (
            re.compile(r"(?:high|highest)[^\n\r$]{0,80}\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)", re.I),
            re.compile(r"\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)[^\n\r]{0,80}(?:high|highest)", re.I),
        )
    return _SNIPPET_METRIC_PATTERNS


def extract_metric_from_snippet_for_source(text: str, source: SourceKey) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    for pattern in _patterns_for_source(source):
        match = pattern.search(normalized)
        if match:
            value = normalize_numeric_string(match.group(1))
            if value:
                return value
    if source == "gurufocus":
        return None
    return extract_metric_from_snippet(text)


def _dedupe_texts(texts: list[str], *, max_items: int = 20) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for text in texts:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= max_items:
            break
    return deduped


async def _collect_google_snippets(page: Page) -> list[str]:
    snippets: list[str] = []
    for selector in _GOOGLE_SNIPPET_SELECTORS:
        try:
            texts = await page.locator(selector).all_text_contents()
            snippets.extend(t.strip() for t in texts if t.strip())
        except Exception:
            continue
    return _dedupe_texts(snippets)


def _extract_google_metric(snippets: list[str], source: SourceKey) -> str | None:
    for snippet in snippets:
        value = extract_metric_from_snippet_for_source(snippet, source)
        if value:
            return value
    return None


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
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if response is None:
        raise RuntimeError(f"{source_label} returned no response")
    status = response.status
    logger.info("[%s] %s status=%s", ticker, source_label, status)
    if status in {403, 429} or status >= 500:
        raise RuntimeError(f"{source_label} restrictive or unstable status: {status}")
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
            return
        except Exception:
            continue


async def _alpha_css(page: Page) -> str | None:
    loc = page.locator(".dcf-value-color.no-margin.valuation-scenario-value").first
    if await loc.count() > 0:
        return await loc.inner_text(timeout=5000)
    important = page.locator("tr.important td.right.aligned.restriction-sensitive-data").first
    if await important.count() > 0:
        return await important.inner_text(timeout=5000)
    return None


async def _extract_alpha_spread(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    url = ALPHA_SPREAD_URL.format(security_type=job.security_type or "nasdaq", ticker=job.ticker)
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
    json_value = extract_from_json_payloads(payloads, DCF_JSON_KEY_HINTS)
    if json_value:
        return PrimaryExtractionResult(metric=json_value, method="json")
    text_value = extract_from_text(text, DCF_TEXT_REGEX_PATTERNS)
    if text_value:
        return PrimaryExtractionResult(metric=text_value, method="text")
    css_value = await _alpha_css(page)
    normalized = normalize_numeric_string(css_value or "")
    if normalized:
        return PrimaryExtractionResult(metric=normalized, method="css")
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
            response = await page.goto(url, wait_until="domcontentloaded", timeout=DIRECT_TIMEOUT_MS)
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


async def _extract_gurufocus_earnings_term(page: Page, ticker: str) -> PrimaryExtractionResult | None:
    container = page.locator("#def_body_detail_height").first
    try:
        await container.wait_for(state="attached", timeout=15_000)
    except Exception as exc:
        logger.debug("[%s] GuruFocus earnings term container not visible: %s", ticker, exc)

    try:
        heading = container.locator("h1").first
        if await heading.count() > 0:
            heading_text = await heading.inner_text(timeout=5000)
            value = _extract_gurufocus_earnings_value(heading_text)
            if value:
                logger.info("[%s] GuruFocus earnings DCF via term h1 -> %s", ticker, value)
                return PrimaryExtractionResult(metric=value, method="term_h1")

            if _GURUFOCUS_EARNINGS_LABEL.lower() in heading_text.lower():
                spans = heading.locator("span")
                for index in range(await spans.count()):
                    span_text = await spans.nth(index).inner_text(timeout=3000)
                    match = _DOLLAR_RE.search(span_text)
                    if match:
                        value = normalize_numeric_string(match.group(1))
                        if value:
                            logger.info("[%s] GuruFocus earnings DCF via term h1 span -> %s", ticker, value)
                            return PrimaryExtractionResult(metric=value, method="term_h1_span")
    except Exception as exc:
        logger.debug("[%s] GuruFocus earnings heading extract failed: %s", ticker, exc)

    try:
        body_text = await container.inner_text(timeout=8000)
    except Exception:
        body_text = await page.locator("body").inner_text(timeout=8000)

    value = _extract_gurufocus_earnings_value(body_text)
    if value:
        logger.info("[%s] GuruFocus earnings DCF via term text -> %s", ticker, value)
        return PrimaryExtractionResult(metric=value, method="term_text")
    return None


async def _extract_gurufocus(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    ticker = job.ticker.strip().upper()
    url = GURUFOCUS_EARNINGS_TERM_URL.format(ticker=ticker)
    logger.info("[%s] GuruFocus primary navigation: %s", ticker, url)

    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
    })
    response = await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=DIRECT_TIMEOUT_MS,
    )
    if response is None:
        raise RuntimeError("No response from primary navigation")

    status = response.status
    logger.info("[%s] Primary status=%s", ticker, status)
    if status in {403, 429} or status >= 500:
        raise RuntimeError(f"Restrictive or unstable status: {status}")

    await _dismiss_overlays(page)
    term_result = await _extract_gurufocus_earnings_term(page, ticker)
    if term_result:
        return term_result

    raise RuntimeError("GuruFocus earnings-based DCF metric was not found")


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


async def _extract_price_target(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    kind = _PRICE_TARGET_KIND[job.source]
    ticker = job.ticker.strip().upper()
    url = YAHOO_QUOTE_URL.format(ticker=ticker)
    logger.info("[%s] Yahoo primary navigation: %s", job.ticker, url)
    payloads, text = await _navigate_collect(
        page,
        url,
        job.ticker,
        "Yahoo Finance",
        hints=("quote", "summary", "finance"),
        timeout=DIRECT_TIMEOUT_MS,
    )
    text_targets = _price_targets_from_text(text)
    if text_targets.get(kind):
        return PrimaryExtractionResult(metric=text_targets[kind], method="text")
    json_value = extract_from_json_payloads(
        payloads,
        PRICE_TARGET_JSON_KEY_HINTS.get(kind, PRICE_TARGET_JSON_KEY_HINTS["avg"]),
    )
    if json_value:
        return PrimaryExtractionResult(metric=json_value, method="json")
    quote_summary_base = YAHOO_QUOTE_SUMMARY_URL.format(ticker=ticker)
    payload_text = await page.evaluate(
        r"""async ({ ticker, crumbUrl, quoteSummaryBase }) => {
            let crumb = window?.YAHOO?.context?.user?.crumb || window?.YAHOO?.context?.crumb || '';
            if (!crumb) {
                const html = document.documentElement.innerHTML || '';
                const match = html.match(/"crumb"\s*:\s*"([^"]+)"/);
                crumb = match ? match[1] : '';
            }
            if (!crumb) {
                const crumbResponse = await fetch(crumbUrl, { credentials: 'include' });
                if (!crumbResponse.ok) throw new Error(`crumb status ${crumbResponse.status}`);
                crumb = (await crumbResponse.text()).trim();
            }
            const params = new URLSearchParams({
                modules: 'financialData',
                formatted: 'false',
                lang: 'en-US',
                region: 'US',
                crumb,
            });
            const url = `${quoteSummaryBase}?${params.toString()}`;
            const response = await fetch(url, { credentials: 'include' });
            if (!response.ok) throw new Error(`quoteSummary status ${response.status}`);
            return await response.text();
        }""",
        {
            "ticker": ticker,
            "crumbUrl": YAHOO_CRUMB_URL,
            "quoteSummaryBase": quote_summary_base,
        },
    )
    payload = json.loads(payload_text)
    financial_data = (((payload.get("quoteSummary") or {}).get("result") or [{}])[0]).get("financialData") or {}
    key = {
        "low": "targetLowPrice",
        "avg": "targetMeanPrice",
        "high": "targetHighPrice",
    }[kind]
    raw = financial_data.get(key)
    if isinstance(raw, dict):
        raw = raw.get("raw", raw.get("fmt"))
    value = normalize_numeric_string(str(raw)) if raw is not None else None
    if value:
        return PrimaryExtractionResult(metric=value, method="quote_summary")
    text_value = extract_from_text(text, PRICE_TARGET_TEXT_REGEX.get(kind, PRICE_TARGET_TEXT_REGEX["avg"]))
    if text_value:
        return PrimaryExtractionResult(metric=text_value, method="text_regex")
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


async def extract_google_fallback_metric_for_job(page: Page, job: ScrapeJob) -> GoogleFallbackResult:
    query = _google_query(job)
    logger.info("[%s] Fallback: Google search %r", job.ticker, query)
    search_url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en"
    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
    })
    await page.goto(search_url, wait_until="domcontentloaded", timeout=SERP_TIMEOUT_MS)
    try:
        await _dismiss_overlays(page)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=SERP_TIMEOUT_MS)
    except Exception:
        pass
    await page.wait_for_timeout(1500)

    snippets = await _collect_google_snippets(page)
    try:
        body_text = (await page.locator("body").inner_text(timeout=5000)).lower()
        if any(marker in body_text for marker in _GOOGLE_BLOCKED_MARKERS):
            logger.warning("[%s] Google fallback appears blocked or challenged", job.ticker)
    except Exception:
        pass

    logger.info("[%s] Fallback collected %d search snippets", job.ticker, len(snippets))
    metric = _extract_google_metric(snippets, job.source)
    if metric:
        logger.info("[%s] Google fallback extracted metric=%s", job.ticker, metric)
    return GoogleFallbackResult(metric=metric, snippets=snippets)


async def extract_google_fallback_metric(page: Page, ticker: str) -> GoogleFallbackResult:
    job = ScrapeJob(
        row=0,
        ticker=ticker,
        source="gurufocus",
        value_col="",
        value_col_index=0,
        label="GuruFocus",
    )
    return await extract_google_fallback_metric_for_job(page, job)
