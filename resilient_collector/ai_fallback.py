"""Unified AI valuation metric fallback module supporting Google Gemini and Grok (xAI)."""

from __future__ import annotations

import logging
import os

from resilient_collector.gemini_collector import (
    fetch_metric_from_gemini,
    gemini_is_configured,
    get_gemini_api_key,
)
from resilient_collector.grok_collector import (
    fetch_metric_from_grok,
    get_grok_api_key,
    grok_is_configured,
)

logger = logging.getLogger(__name__)


def ai_is_configured() -> bool:
    """Return True if either Gemini or Grok is configured with an API key."""
    return gemini_is_configured() or grok_is_configured()


def ai_primary_sources() -> set[str]:
    """Return set of source names that should skip browser scraping and use AI directly."""
    raw = (
        os.environ.get("AI_PRIMARY_FOR")
        or os.environ.get("GEMINI_PRIMARY_FOR")
        or os.environ.get("GROK_PRIMARY_FOR")
        or ""
    )
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


async def fetch_ai_valuation_metric(ticker: str, source: str) -> str | None:
    """Fetch valuation metric using Gemini (Google Search grounding) or Grok."""
    provider = os.environ.get("AI_PROVIDER", "").strip().lower()

    # Prioritize Gemini if configured or explicitly requested
    if provider == "gemini" or (not provider and gemini_is_configured()):
        logger.info("[%s] Using Google Gemini AI (Google Search grounding) for %s...", ticker, source)
        val = await fetch_metric_from_gemini(ticker, source)
        if val:
            return val
        if grok_is_configured():
            logger.info("[%s] Gemini lookup yielded no result; trying Grok as secondary fallback...", ticker)
            return await fetch_metric_from_grok(ticker, source)
        return None

    # Fallback to Grok if configured
    if grok_is_configured():
        logger.info("[%s] Using Grok xAI for %s...", ticker, source)
        return await fetch_metric_from_grok(ticker, source)

    logger.debug("[%s] No AI API keys configured (set GEMINI_API_KEY in .env)", ticker)
    return None
