"""
GuruFocus DCF — https://www.gurufocus.com/dcf-calculator?ticker={TICKER}

Fair Value (DCF output) is read from the calculator page DOM (.stock-dcf-table),
not from generic network JSON captures.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from playwright.sync_api import Page

from config import (
    GURUFOCUS_DCF_URL,
    GURUFOCUS_FAIR_VALUE_POLL_ATTEMPTS,
    GURUFOCUS_FAIR_VALUE_POLL_INTERVAL_MS,
    GURUFOCUS_PAGE_SETTLE_MS,
    GURUFOCUS_WAIT_FAIR_VALUE_TIMEOUT_MS,
    SCRAPE_RETRIES,
)
from scrapers.base import BaseScraper
from scrapers.extract import ExtractionResult, normalize_numeric_string

logger = logging.getLogger(__name__)

# Browser-side scan for Fair Value row in DCF calculator tables
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

# Returns true when Fair Value cell contains a dollar amount (for wait_for_function)
_FAIR_VALUE_READY_JS = """
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
      if (val && parseFloat(val) > 0) return true;
    }
  }
  return false;
}
"""

_DOLLAR_RE = re.compile(
    r"\$\s*(-?[\d]{1,3}(?:,[\d]{3})*(?:\.\d+)?|-?[\d]+(?:\.\d+)?)"
)


def prepare_dcf_calculator_page(page: Page) -> None:
    """Dismiss overlays and wait for calculator tables after navigation."""
    for label in ("Accept", "Accept All", "I agree", "Got it"):
        try:
            page.get_by_role("button", name=label).click(timeout=2000)
            break
        except Exception:
            continue

    try:
        page.wait_for_selector(".stock-dcf-table", state="visible", timeout=45_000)
    except Exception as exc:
        logger.warning("GuruFocus .stock-dcf-table not visible: %s", exc)

    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        logger.debug("GuruFocus networkidle timeout (continuing)")

    page.wait_for_timeout(GURUFOCUS_PAGE_SETTLE_MS)

    # Wait up to 90s for Vue/DCF model to write Fair Value (no login required if site allows)
    try:
        page.wait_for_function(
            _FAIR_VALUE_READY_JS,
            timeout=GURUFOCUS_WAIT_FAIR_VALUE_TIMEOUT_MS,
        )
        logger.info("GuruFocus Fair Value cell populated")
    except Exception:
        logger.warning(
            "GuruFocus Fair Value not ready after %ss — will keep polling",
            GURUFOCUS_WAIT_FAIR_VALUE_TIMEOUT_MS // 1000,
        )


def extract_fair_value_from_dcf_calculator(page: Page) -> Optional[str]:
    """
    Extract Fair Value from https://www.gurufocus.com/dcf-calculator page.
    Tries JS row scan, Playwright locators, then legacy CSS selectors.
    """
    # 1) JavaScript — Fair Value row in .stock-dcf-table
    try:
        raw = page.evaluate(_EXTRACT_FAIR_VALUE_JS)
        normalized = normalize_numeric_string(str(raw)) if raw else None
        if normalized:
            logger.debug("GuruFocus Fair Value via JS: %s", normalized)
            return normalized
    except Exception as exc:
        logger.debug("GuruFocus JS extract failed: %s", exc)

    # 2) Playwright — right summary panel (Fair Value block)
    try:
        panel = page.locator(
            ".el-col.inline.p-l-xs .stock-dcf-table, "
            ".inline.p-l-xs .stock-dcf-table"
        ).first
        if panel.count() > 0:
            row = panel.locator(".dcf-table-row").filter(has_text="Fair Value").first
            if row.count() > 0:
                cell = row.locator(
                    ".fs-x-large.fw-bolder.text-right"
                ).first
                if cell.count() > 0:
                    text = cell.inner_text(timeout=5000)
                    match = _DOLLAR_RE.search(text)
                    if match:
                        val = normalize_numeric_string(match.group(1))
                        if val:
                            logger.debug("GuruFocus Fair Value via panel locator: %s", val)
                            return val
    except Exception as exc:
        logger.debug("GuruFocus panel locator failed: %s", exc)

    # 3) Regex on right-panel text only (avoid full-page false positives)
    try:
        panel = page.locator(".inline.p-l-xs, .el-col.inline.p-l-xs").first
        if panel.count() > 0:
            text = panel.inner_text(timeout=5000)
            match = re.search(
                r"Fair\s*Value[^\d$]{0,40}\$\s*(-?[\d,]+\.?\d*)",
                text,
                re.IGNORECASE | re.DOTALL,
            )
            if match:
                val = normalize_numeric_string(match.group(1))
                if val:
                    logger.debug("GuruFocus Fair Value via panel regex: %s", val)
                    return val
    except Exception as exc:
        logger.debug("GuruFocus panel regex failed: %s", exc)

    # 4) Legacy step-53 CSS (any table cell with $ in fair-value styling)
    selectors = [
        ".stock-dcf-table > div:nth-child(3) .fs-x-large.fw-bolder.text-right.el-col.el-col-12",
        ".stock-dcf-table div.fs-x-large.fw-bolder.text-right.el-col.el-col-12",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).filter(has_text="$").first
            if loc.count() == 0:
                continue
            raw = loc.inner_text(timeout=5000)
            match = _DOLLAR_RE.search(raw or "")
            if match:
                val = normalize_numeric_string(match.group(1))
                if val:
                    logger.debug("GuruFocus Fair Value via CSS %s: %s", selector[:40], val)
                    return val
        except Exception:
            continue

    return None


def _calculator_model_ran(page: Page) -> bool:
    """True if Growth/Terminal value areas show numbers (not just '$')."""
    try:
        text = page.locator(".dcf-calculator").first.inner_text(timeout=5000)
        return bool(re.search(r"Growth Value[\s\S]{0,80}\$\s*[\d,]+\.?\d+", text, re.I))
    except Exception:
        return False


def wait_for_fair_value_on_calculator(page: Page) -> Optional[str]:
    """Poll until Fair Value appears or attempts exhausted."""
    for attempt in range(1, GURUFOCUS_FAIR_VALUE_POLL_ATTEMPTS + 1):
        value = extract_fair_value_from_dcf_calculator(page)
        if value is not None:
            logger.info("GuruFocus Fair Value ready on poll %d: %s", attempt, value)
            return value
        if attempt < GURUFOCUS_FAIR_VALUE_POLL_ATTEMPTS:
            page.wait_for_timeout(GURUFOCUS_FAIR_VALUE_POLL_INTERVAL_MS)

    if not _calculator_model_ran(page):
        logger.warning(
            "GuruFocus DCF model did not populate (Growth/Terminal values empty). "
            "Headless Chrome may be blocked — retry with: "
            "python scripts/capture_json.py --source gurufocus --ticker TICKER --headed"
        )
    return None


class GuruFocusScraper(BaseScraper):
    """
    Scrapes DCF Fair Value from GuruFocus DCF Calculator only.
    URL: https://www.gurufocus.com/dcf-calculator?ticker={ticker}
    """

    source_key = "gurufocus"
    source_label = "GuruFocus"

    def scrape(self, page: Page, ticker: str) -> Optional[str]:
        url = GURUFOCUS_DCF_URL.format(ticker=ticker.strip().upper())
        logger.info("[%s] DCF Calculator %s", self.source_label, url)

        for attempt in range(1, SCRAPE_RETRIES + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                prepare_dcf_calculator_page(page)
                value = wait_for_fair_value_on_calculator(page)
                if value is not None:
                    return value

                logger.warning(
                    "[%s] %s attempt %d: Fair Value not found on calculator page",
                    self.source_label,
                    ticker,
                    attempt,
                )
                if attempt < SCRAPE_RETRIES:
                    page.reload(wait_until="commit")
            except Exception as exc:
                logger.error(
                    "[%s] %s attempt %d failed: %s",
                    self.source_label,
                    ticker,
                    attempt,
                    exc,
                )
        return None

    def scrape_with_meta(self, page: Page, ticker: str) -> Optional[ExtractionResult]:
        """Same as scrape but returns extraction metadata for tests."""
        value = self.scrape(page, ticker)
        if value is None:
            return None
        return ExtractionResult(value=value, method="dcf_calculator_dom")
