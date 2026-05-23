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
    GURUFOCUS_DCF_URL,
    JSON_URL_HINTS_ALPHASPREAD,
    JSON_URL_HINTS_VALUEINVESTING,
    PRICE_TARGET_JSON_KEY_HINTS,
    PRICE_TARGET_TEXT_REGEX,
    SourceKey,
    USER_AGENT,
    VALUE_INVESTING_URL,
)
from models import ScrapeJob
from scrapers.extract import extract_from_json_payloads, extract_from_text, normalize_numeric_string

logger = logging.getLogger(__name__)
DIRECT_TIMEOUT_MS = int(os.environ.get("DIRECT_SCRAPE_TIMEOUT_MS", "10000"))
SERP_TIMEOUT_MS = int(os.environ.get("SERP_TIMEOUT_MS", "10000"))


@dataclass
class GoogleFallbackResult:
    metric: str | None
    snippets: list[str]


@dataclass
class PrimaryExtractionResult:
    metric: str
    method: str


_EXTRACT_FAIR_VALUE_JS = """
() => {
  const parseDollar = (text) => {
    if (!text) return null;
    const m = String(text).match(/\\$\\s*(-?[\\d]{1,3}(?:,[\\d]{3})*(?:\\.[\\d]+)?|-?[\\d]+(?:\\.[\\d]+)?)/);
    return m ? m[1].replace(/,/g, '') : null;
  };

  const tables = Array.from(document.querySelectorAll('.stock-dcf-table'));
  for (const table of tables) {
    const rows = table.querySelectorAll('.dcf-table-row, .el-row.dcf-table-row, .el-row');
    for (const row of rows) {
      const label = (row.innerText || '').trim();
      if (!/Fair Value/i.test(label) || /Saved Fair Values/i.test(label)) continue;
      const valueCell = row.querySelector(
        '.fs-x-large.fw-bolder.text-right.el-col-12, .fs-x-large.fw-bolder.text-right'
      );
      if (!valueCell) continue;
      const val = parseDollar(valueCell.innerText || valueCell.textContent);
      if (val && parseFloat(val) > 0) return val;
    }
  }
  return null;
}
"""

_DOLLAR_RE = re.compile(
    r"\$\s*(-?[\d]{1,3}(?:,[\d]{3})*(?:\.\d+)?|-?[\d]+(?:\.\d+)?)"
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

_SOURCE_LABELS: dict[SourceKey, str] = {
    "alphaspread": "Alpha Spread DCF fair value",
    "valueinvesting": "ValueInvesting DCF fair value",
    "gurufocus": "GuruFocus DCF fair value",
    "price_target_low": "Yahoo Finance analyst low price target",
    "price_target_avg": "Yahoo Finance analyst average price target",
    "price_target_high": "Yahoo Finance analyst high price target",
}

_PRICE_TARGET_KIND: dict[SourceKey, str] = {
    "price_target_low": "low",
    "price_target_avg": "avg",
    "price_target_high": "high",
}

_VALUEINVESTING_JSON_HINTS = (
    "dcfvaluation",
    "dcfvalue",
    "fairvalue",
    "fairprice",
    "intrinsicvalue",
    "estimatedvalue",
    "valuepershare",
)

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
        return f"{job.ticker} dcf gurufocus"
    if job.source == "price_target_low":
        return f"{job.ticker} analyst price target low yahoo finance"
    if job.source == "price_target_avg":
        return f"{job.ticker} analyst average price target yahoo finance"
    if job.source == "price_target_high":
        return f"{job.ticker} analyst high price target yahoo finance"
    return f"{job.ticker} {job.label}"


def _patterns_for_source(source: SourceKey) -> tuple[re.Pattern[str], ...]:
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
    return extract_metric_from_snippet(text)


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


async def _extract_valueinvesting(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    url = VALUE_INVESTING_URL.format(ticker=job.ticker)
    logger.info("[%s] ValueInvesting primary navigation: %s", job.ticker, url)
    payloads, text = await _navigate_collect(
        page,
        url,
        job.ticker,
        "ValueInvesting",
        hints=JSON_URL_HINTS_VALUEINVESTING,
        timeout=DIRECT_TIMEOUT_MS,
    )
    lowered = text.lower()
    if "discounted cash flow" not in lowered and "dcf valuation" not in lowered:
        blocked_markers = ("regional plan", "global plan", "start free trial", "create your account")
        if any(marker in lowered for marker in blocked_markers):
            raise RuntimeError("ValueInvesting pricing/login page")
    json_value = extract_from_json_payloads(payloads, _VALUEINVESTING_JSON_HINTS)
    if json_value and json_value != "14.99":
        return PrimaryExtractionResult(metric=json_value, method="json")
    try:
        norm_texts = [
            (await el.text_content(timeout=3000) or "").strip()
            for el in await page.locator(".norm").all()
        ]
        for index in (2, 1, 0):
            if len(norm_texts) > index and "%" not in norm_texts[index]:
                value = normalize_numeric_string(norm_texts[index].split()[0])
                if value and value != "14.99":
                    return PrimaryExtractionResult(metric=value, method="norm")
    except Exception:
        pass
    text_value = extract_from_text(text, _VALUEINVESTING_TEXT_PATTERNS)
    if text_value:
        return PrimaryExtractionResult(metric=text_value, method="text")
    raise RuntimeError("ValueInvesting metric not found")


async def _extract_fair_value_dom(page: Page) -> str | None:
    try:
        raw = await page.evaluate(_EXTRACT_FAIR_VALUE_JS)
        if raw:
            return normalize_numeric_string(str(raw))
    except Exception as exc:
        logger.debug("GuruFocus JS extract failed: %s", exc)

    try:
        panel = page.locator(
            ".el-col.inline.p-l-xs .stock-dcf-table, "
            ".inline.p-l-xs .stock-dcf-table"
        ).first
        if await panel.count() > 0:
            row = panel.locator(".dcf-table-row").filter(has_text="Fair Value").first
            if await row.count() > 0:
                cell = row.locator(".fs-x-large.fw-bolder.text-right").first
                if await cell.count() > 0:
                    text = await cell.inner_text(timeout=5000)
                    match = _DOLLAR_RE.search(text)
                    if match:
                        return normalize_numeric_string(match.group(1))
    except Exception as exc:
        logger.debug("GuruFocus locator extract failed: %s", exc)

    return None


async def _extract_gurufocus(page: Page, job: ScrapeJob) -> PrimaryExtractionResult:
    ticker = job.ticker
    url = GURUFOCUS_DCF_URL.format(ticker=ticker.strip().upper())
    logger.info("[%s] GuruFocus primary navigation: %s", ticker, url)

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

    for label in ("Accept", "Accept All", "I agree", "Got it"):
        try:
            await page.get_by_role("button", name=label).click(timeout=2000)
            break
        except Exception:
            continue

    try:
        await page.wait_for_selector(".stock-dcf-table", state="visible", timeout=10_000)
    except Exception as exc:
        logger.warning("[%s] .stock-dcf-table not visible: %s", ticker, exc)

    for attempt in range(1, 4):
        value = await _extract_fair_value_dom(page)
        if value:
            logger.info("[%s] Primary Fair Value on poll %d: %s", ticker, attempt, value)
            return PrimaryExtractionResult(metric=value, method="dom")
        if attempt < 15:
            await page.wait_for_timeout(2000)

    raise RuntimeError("Primary page loaded but Fair Value metric was not found")


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
    url = f"https://finance.yahoo.com/quote/{job.ticker.strip().upper()}/"
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
    payload_text = await page.evaluate(
        """async (ticker) => {
            let crumb = window?.YAHOO?.context?.user?.crumb || window?.YAHOO?.context?.crumb || '';
            if (!crumb) {
                const html = document.documentElement.innerHTML || '';
                const match = html.match(/"crumb"\s*:\s*"([^"]+)"/);
                crumb = match ? match[1] : '';
            }
            if (!crumb) {
                const crumbResponse = await fetch('https://query1.finance.yahoo.com/v1/test/getcrumb', { credentials: 'include' });
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
            const url = `https://query1.finance.yahoo.com/v10/finance/quoteSummary/${ticker}?${params.toString()}`;
            const response = await fetch(url, { credentials: 'include' });
            if (!response.ok) throw new Error(`quoteSummary status ${response.status}`);
            return await response.text();
        }""",
        job.ticker.strip().upper(),
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
    await page.goto(search_url, wait_until="domcontentloaded", timeout=SERP_TIMEOUT_MS)
    await page.wait_for_load_state("domcontentloaded", timeout=SERP_TIMEOUT_MS)

    selectors = ("div.VwiC3b", "span.hgKElc", "div[data-sncf]", "div[data-content-feature='1']")
    snippets: list[str] = []
    for selector in selectors:
        try:
            texts = await page.locator(selector).all_text_contents()
            snippets.extend(t.strip() for t in texts if t.strip())
        except Exception:
            continue

    logger.info("[%s] Fallback collected %d search snippets", job.ticker, len(snippets))
    return GoogleFallbackResult(metric=None, snippets=snippets)


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
