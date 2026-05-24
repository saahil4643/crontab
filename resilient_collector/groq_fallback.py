from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import httpx

from scrapers.extract import normalize_numeric_string

logger = logging.getLogger(__name__)

GROQ_ENDPOINT = os.environ.get(
    "GROQ_ENDPOINT",
    "https://api.groq.com/openai/v1/chat/completions",
)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_TIMEOUT_SECONDS = float(os.environ.get("GROQ_TIMEOUT_SECONDS", "25"))


@dataclass
class GroqFallbackResult:
    metric: str | None
    confidence: str | None = None
    note: str | None = None


def _extract_json_object(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_metric_from_text(text: str) -> str | None:
    stripped = text.strip()
    direct = normalize_numeric_string(stripped)
    if direct:
        return direct
    patterns = (
        r'"metric"\s*:\s*"([^"]+)"',
        r'"metric"\s*:\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = normalize_numeric_string(match.group(1))
            if value:
                return value
    return None


def _parse_response(content: str) -> GroqFallbackResult:
    parsed = _extract_json_object(content)
    if parsed:
        metric = parsed.get("metric")
        value = normalize_numeric_string(str(metric)) if metric is not None else None
        return GroqFallbackResult(
            metric=value,
            confidence=str(parsed.get("confidence")) if parsed.get("confidence") else None,
            note=str(parsed.get("note")) if parsed.get("note") else None,
        )

    return GroqFallbackResult(metric=_extract_metric_from_text(content))


def _build_snippet_prompt(
    ticker: str,
    snippets: list[str] | None = None,
    metric_label: str | None = None,
) -> str:
    snippet_text = "\n".join(f"- {snippet[:700]}" for snippet in (snippets or [])[:10])
    target = metric_label or "GuruFocus DCF Fair Value or intrinsic value per share"
    return (
        f"Extract the {target} for ticker {ticker} only from the snippets below.\n"
        "Do not use model memory, training data, estimates, or guesses.\n"
        "Return only the numeric value with no currency symbol, no JSON, no words, and no explanation.\n"
        "If the requested value is not explicitly present in the snippets, return an empty response.\n"
        f"Snippets:\n{snippet_text}"
    )


def _build_direct_prompt(ticker: str, metric_label: str | None = None) -> str:
    target = metric_label or "GuruFocus Intrinsic Value: DCF (Earnings Based)"
    return (
        f"What is the {target} for ticker {ticker}?\n"
        "Return only one numeric value with no currency symbol, no JSON, no words, and no explanation.\n"
        "If you are not confident, or may be confusing it with GF Value, DCF FCF Based, "
        "current price, margin of safety, or any other metric, return an empty response."
    )


def groq_direct_fallback_enabled() -> bool:
    raw = os.environ.get("RESILIENT_GROQ_DIRECT_FALLBACK", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _api_keys() -> list[str]:
    env_keys = [
        os.environ.get("GROQ_KEY_1", "").strip(),
        os.environ.get("GROQ_KEY_2", "").strip(),
    ]
    raw_keys = os.environ.get("GROQ_API_KEYS", "").strip()
    keys = [
        key.strip()
        for key in re.split(r"[\s,;]+", raw_keys)
        if key.strip()
    ]
    single_key = os.environ.get("GROQ_API_KEY", "").strip()
    if single_key:
        keys.append(single_key)
    return list(dict.fromkeys([key for key in env_keys + keys if key]))


async def _request_groq(api_key: str, payload: dict) -> GroqFallbackResult:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=GROQ_TIMEOUT_SECONDS) as client:
        response = await client.post(GROQ_ENDPOINT, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_response(content)


async def _call_groq(user_prompt: str, *, ticker: str, log_label: str) -> GroqFallbackResult:
    api_keys = _api_keys()
    if not api_keys:
        logger.warning("[%s] %s skipped: GROQ_API_KEY or GROQ_API_KEYS is not set", ticker, log_label)
        return GroqFallbackResult(metric=None, note="GROQ_API_KEY or GROQ_API_KEYS is not set")

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You return only one numeric financial metric value. "
                    "No prose, no JSON, no currency symbol, no explanation."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }

    last_error: str | None = None
    for index, api_key in enumerate(api_keys, start=1):
        try:
            result = await _request_groq(api_key, payload)
            if result.metric:
                logger.info(
                    "[%s] %s extracted metric=%s confidence=%s key_index=%d",
                    ticker,
                    log_label,
                    result.metric,
                    result.confidence,
                    index,
                )
            else:
                logger.warning("[%s] %s returned no metric key_index=%d", ticker, log_label, index)
            return result
        except Exception as exc:
            last_error = str(exc)
            logger.warning("[%s] %s key_index=%d failed: %s", ticker, log_label, index, exc)

    return GroqFallbackResult(metric=None, note=last_error)


async def extract_groq_fallback_metric(
    ticker: str,
    snippets: list[str] | None = None,
    metric_label: str | None = None,
) -> GroqFallbackResult:
    if not snippets:
        logger.warning("[%s] Groq snippet fallback skipped: no search snippets to parse", ticker)
        return GroqFallbackResult(metric=None, note="No search snippets to parse")

    return await _call_groq(
        _build_snippet_prompt(ticker, snippets, metric_label),
        ticker=ticker,
        log_label="Groq snippet fallback",
    )


async def extract_groq_direct_fallback_metric(
    ticker: str,
    metric_label: str | None = None,
) -> GroqFallbackResult:
    if not groq_direct_fallback_enabled():
        logger.info("[%s] Groq direct fallback disabled", ticker)
        return GroqFallbackResult(metric=None, note="Groq direct fallback disabled")

    logger.warning(
        "[%s] Using Groq direct fallback (model memory; may be stale) for %s",
        ticker,
        metric_label or "requested metric",
    )
    return await _call_groq(
        _build_direct_prompt(ticker, metric_label),
        ticker=ticker,
        log_label="Groq direct fallback",
    )
