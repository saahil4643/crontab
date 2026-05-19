"""Build scrape URLs for capture testing and production scrapers."""

from __future__ import annotations

from config import (
    ALPHA_SPREAD_URL,
    DEFAULT_SECURITY_TYPE,
    GURUFOCUS_DCF_URL,
    PRICE_TARGET_URL_TEMPLATE,
    VALUE_INVESTING_URL,
)

CAPTURE_SOURCES = frozenset({
    "alphaspread",
    "valueinvesting",
    "gurufocus",
    "price_target",
})


def build_source_url(
    source: str,
    ticker: str,
    *,
    security_type: str = DEFAULT_SECURITY_TYPE,
) -> str:
    ticker = ticker.strip().upper()
    if source == "alphaspread":
        return ALPHA_SPREAD_URL.format(security_type=security_type, ticker=ticker)
    if source == "valueinvesting":
        return VALUE_INVESTING_URL.format(ticker=ticker)
    if source == "gurufocus":
        return GURUFOCUS_DCF_URL.format(ticker=ticker)
    if source == "price_target":
        return PRICE_TARGET_URL_TEMPLATE.format(ticker=ticker)
    raise ValueError(
        f"Unknown source {source!r}. Choose from: {', '.join(sorted(CAPTURE_SOURCES))}"
    )
