"""Grok (xAI) API client with web search for valuation metrics fallback."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GROK_API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_GROK_MODEL = "grok-2-latest"


def get_grok_api_key() -> str:
    """Return the configured xAI / Grok API key, if any."""
    return (
        os.environ.get("XAI_API_KEY")
        or os.environ.get("GROK_API_KEY")
        or ""
    ).strip()


def grok_is_configured() -> bool:
    """True when an xAI API key is present in environment."""
    return bool(get_grok_api_key())


def _build_valuation_prompt(ticker: str, source: str) -> str:
    ticker_clean = ticker.strip().upper()
    if source == "gurufocus":
        return (
            f"Search the web for the current GuruFocus DCF Fair Value (or earnings-based DCF Intrinsic Value) "
            f"for stock ticker {ticker_clean}.\n"
            f"Find the exact dollar number currently calculated by GuruFocus's DCF model.\n"
            f"Respond strictly with valid JSON only (no markdown, no backticks, no extra text):\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "gurufocus"}}'
        )
    elif source == "alphaspread":
        return (
            f"Search the web for the current Alpha Spread DCF Intrinsic Value (Base Case fair value) "
            f"for stock ticker {ticker_clean}.\n"
            f"Find the exact dollar number currently calculated by Alpha Spread's DCF summary.\n"
            f"Respond strictly with valid JSON only (no markdown, no backticks, no extra text):\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "alphaspread"}}'
        )
    elif source == "valueinvesting":
        return (
            f"Search the web for the current ValueInvesting.io DCF Fair Value (5y growth exit model) "
            f"for stock ticker {ticker_clean}.\n"
            f"Respond strictly with valid JSON only (no markdown, no backticks, no extra text):\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "valueinvesting"}}'
        )
    else:
        return (
            f"Search the web for current financial valuation fair value / price target for {ticker_clean} from {source}.\n"
            f"Respond strictly with valid JSON only:\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "{source}"}}'
        )


def _parse_grok_metric(raw_content: str) -> str | None:
    """Extract and validate the numeric metric from Grok's response."""
    if not raw_content:
        return None

    cleaned = raw_content.strip()
    # Strip markdown code blocks if the model wrapped the JSON
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # Try JSON parse first
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            metric_val = data.get("metric")
            if metric_val is not None:
                val_str = str(metric_val).replace("$", "").replace(",", "").strip()
                float_val = float(val_str)
                if float_val > 0:
                    return f"{float_val:.2f}"
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Regex fallback to find numeric metric: "metric": 123.45 or $123.45
    match = re.search(r'"metric"\s*:\s*"?\$?\s*([0-9]+(?:\.[0-9]+)?)"?', cleaned, re.IGNORECASE)
    if match:
        try:
            val = float(match.group(1))
            if val > 0:
                return f"{val:.2f}"
        except ValueError:
            pass

    dollar_match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", cleaned)
    if dollar_match:
        try:
            val = float(dollar_match.group(1))
            if val > 0:
                return f"{val:.2f}"
        except ValueError:
            pass

    return None


async def fetch_metric_from_grok(
    ticker: str,
    source: str,
    *,
    timeout_sec: float = 35.0,
) -> str | None:
    """Query Grok API to search live web data and extract valuation metric."""
    api_key = get_grok_api_key()
    if not api_key:
        logger.debug("[grok] No API key configured (set XAI_API_KEY in .env)")
        return None

    model = os.environ.get("GROK_MODEL", DEFAULT_GROK_MODEL).strip()
    prompt = _build_valuation_prompt(ticker, source)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a real-time financial analyst. Your job is to search the web and extract "
                    "exact DCF fair values and valuation metrics from specified financial platforms. "
                    "Always output strictly in the requested JSON format without preamble or commentary."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.0,
    }

    logger.info("[%s] Querying Grok API (%s) for %s fair value...", ticker, model, source)
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.post(GROK_API_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "[%s] Grok API returned HTTP %s: %s",
                    ticker,
                    resp.status_code,
                    resp.text[:300],
                )
                return None

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("[%s] Grok API returned no choices", ticker)
                return None

            content = choices[0].get("message", {}).get("content", "")
            metric = _parse_grok_metric(content)
            if metric:
                logger.info(
                    "[%s] Grok AI successfully extracted %s DCF metric -> %s",
                    ticker,
                    source,
                    metric,
                )
                return metric
            else:
                logger.warning(
                    "[%s] Could not parse numeric metric from Grok response: %r",
                    ticker,
                    content[:200],
                )
                return None
    except httpx.TimeoutException:
        logger.warning("[%s] Grok API request timed out after %.1fs", ticker, timeout_sec)
        return None
    except Exception as exc:
        logger.warning("[%s] Grok API error: %s", ticker, exc)
        return None
