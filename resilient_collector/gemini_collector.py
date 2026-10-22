"""Google Gemini API client with Google Search grounding and live search fallback for valuation metrics."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
FALLBACK_GEMINI_MODEL = "gemini-flash-latest"


def get_gemini_api_key() -> str:
    """Return the configured Gemini / Google AI Studio API key."""
    raw = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
    return raw


def gemini_is_configured() -> bool:
    """True when a Gemini API key is present."""
    return bool(get_gemini_api_key())


def _build_gemini_prompt(ticker: str, source: str) -> str:
    ticker_clean = ticker.strip().upper()
    if source == "gurufocus":
        return (
            f"Search Google for the current GuruFocus DCF Fair Value (or earnings-based DCF Intrinsic Value) "
            f"for stock ticker {ticker_clean}.\n"
            f"Find the exact dollar number calculated by GuruFocus's DCF model.\n"
            f"Respond strictly with valid JSON only (no markdown, no backticks, no other text):\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "gurufocus"}}'
        )
    elif source == "alphaspread":
        return (
            f"Search Google for the current Alpha Spread DCF Intrinsic Value (Base Case fair value) "
            f"for stock ticker {ticker_clean}.\n"
            f"Find the exact dollar number calculated by Alpha Spread's DCF summary.\n"
            f"Respond strictly with valid JSON only (no markdown, no backticks, no other text):\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "alphaspread"}}'
        )
    elif source == "valueinvesting":
        return (
            f"Search Google for the current ValueInvesting.io DCF Fair Value (5y growth exit model) "
            f"for stock ticker {ticker_clean}.\n"
            f"Respond strictly with valid JSON only (no markdown, no backticks, no other text):\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "valueinvesting"}}'
        )
    else:
        return (
            f"Search Google for current financial valuation fair value / price target for {ticker_clean} from {source}.\n"
            f"Respond strictly with valid JSON only:\n"
            f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "{source}"}}'
        )


def _build_extraction_prompt(ticker: str, source: str, context_text: str) -> str:
    ticker_clean = ticker.strip().upper()
    return (
        f"You are an expert financial valuation data extractor.\n"
        f"Below are live web search results for {ticker_clean} on {source}:\n\n"
        f"{context_text}\n\n"
        f"Task: Extract the {source} DCF Fair Value / Intrinsic Value (per share dollar number) for {ticker_clean}.\n"
        f"If multiple values are mentioned, select the primary or base case DCF intrinsic value.\n"
        f"Respond strictly with valid JSON only (no markdown code blocks, no other text):\n"
        f'{{"ticker": "{ticker_clean}", "metric": 123.45, "source": "{source}"}}'
    )


def _parse_gemini_metric(raw_content: str) -> str | None:
    """Extract and validate the numeric metric from Gemini's response."""
    if not raw_content:
        return None

    cleaned = raw_content.strip()
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

    # Regex search: "metric": 123.45 or metric: $123.45
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


async def _fetch_live_search_snippets(ticker: str, source: str) -> str:
    """Fetch live search snippets from DuckDuckGo HTML for grounding when AI tools are quota-restricted."""
    query = f"{source} {ticker} DCF intrinsic value fair value"
    url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                snippets = re.findall(r'<a class="result__snippet[^"]*"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
                cleaned_snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets[:6]]
                if cleaned_snippets:
                    return "\n".join(cleaned_snippets)
    except Exception as exc:
        logger.debug("[%s] Live search snippet retrieval exception: %s", ticker, exc)
    return ""


async def fetch_metric_from_gemini(
    ticker: str,
    source: str,
    *,
    timeout_sec: float = 35.0,
) -> str | None:
    """Query Google Gemini API with Google Search grounding or live web search to extract valuation metric."""
    api_key = get_gemini_api_key()
    if not api_key:
        logger.debug("[gemini] No API key configured (set GEMINI_API_KEY in .env)")
        return None

    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    # Step 1: Attempt native Google Search grounding
    prompt = _build_gemini_prompt(ticker, source)
    payload: dict[str, Any] = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "tools": [
            {"googleSearch": {}}
        ],
    }

    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    logger.info("[%s] Querying Google Gemini API (%s) for %s fair value...", ticker, model, source)

    for attempt in range(1, 3):
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            try:
                resp = await client.post(url, headers=headers, json=payload)

                # If camelCase googleSearch failed, try snake_case google_search
                if resp.status_code == 400 and "googleSearch" in resp.text:
                    payload["tools"] = [{"google_search": {}}]
                    resp = await client.post(url, headers=headers, json=payload)

                # Check if native response was successful
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        content_parts = candidates[0].get("content", {}).get("parts", [])
                        raw_text = "".join(part.get("text", "") for part in content_parts)
                        metric = _parse_gemini_metric(raw_text)
                        if metric:
                            logger.info(
                                "[%s] Gemini AI successfully extracted %s DCF metric -> %s (via Google Search)",
                                ticker,
                                source,
                                metric,
                            )
                            return metric

                # Step 2: If Google Search grounding is quota-limited (429) or unavailable on free tier:
                logger.info("[%s] Fetching live web search snippets to ground Gemini...", ticker)
                search_context = await _fetch_live_search_snippets(ticker, source)
                if not search_context:
                    # If DuckDuckGo was empty, try with fallback search term
                    search_context = await _fetch_live_search_snippets(ticker, f"{ticker} stock {source}")

                extraction_prompt = _build_extraction_prompt(ticker, source, search_context or f"Search for {ticker} on {source}")
                plain_payload: dict[str, Any] = {
                    "contents": [
                        {
                            "parts": [
                                {"text": extraction_prompt}
                            ]
                        }
                    ]
                }

                resp = await client.post(url, headers=headers, json=plain_payload)
                if resp.status_code == 404 and model != FALLBACK_GEMINI_MODEL:
                    fallback_url = f"{GEMINI_API_BASE}/{FALLBACK_GEMINI_MODEL}:generateContent?key={api_key}"
                    resp = await client.post(fallback_url, headers=headers, json=plain_payload)

                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        content_parts = candidates[0].get("content", {}).get("parts", [])
                        raw_text = "".join(part.get("text", "") for part in content_parts)
                        metric = _parse_gemini_metric(raw_text)
                        if metric:
                            logger.info(
                                "[%s] Gemini AI successfully extracted %s DCF metric -> %s (via live search grounding)",
                                ticker,
                                source,
                                metric,
                            )
                            return metric
                        else:
                            logger.warning(
                                "[%s] Could not parse numeric metric from Gemini response: %r",
                                ticker,
                                raw_text[:200],
                            )
                else:
                    logger.warning(
                        "[%s] Gemini API returned HTTP %s: %s",
                        ticker,
                        resp.status_code,
                        resp.text[:300],
                    )

            except httpx.TimeoutException:
                logger.warning("[%s] Gemini API request timed out after %.1fs (attempt %d/2)", ticker, timeout_sec, attempt)
            except Exception as exc:
                logger.warning("[%s] Gemini API error on attempt %d/2: %s", ticker, attempt, exc)
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(1.0)
                    continue

    return None
