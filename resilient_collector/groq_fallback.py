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
GROQ_LENIENT_FALLBACK = os.environ.get("GROQ_LENIENT_FALLBACK", "1").strip().lower() in {"1", "true", "yes", "on"}


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


def _build_prompt(
    ticker: str,
    snippets: list[str] | None = None,
    metric_label: str | None = None,
) -> str:
    snippet_text = "\n".join(f"- {snippet[:700]}" for snippet in (snippets or [])[:10])
    target = metric_label or "GuruFocus DCF Fair Value or intrinsic value per share"
    if GROQ_LENIENT_FALLBACK:
        source_text = snippet_text or "No useful search snippets were available."
        return (
            f"Find the best available numeric estimate for the {target} for ticker {ticker}.\n"
            "Prefer the provided snippets when they contain the value. If snippets are missing or incomplete, "
            "use your best financial knowledge or reasonable estimate.\n"
            "Return only one numeric value with no currency symbol, no JSON, no words, and no explanation.\n"
            f"Snippets:\n{source_text}"
        )
    return (
        f"Extract the {target} for ticker {ticker} only from the snippets below.\n"
        "Return only the numeric value with no currency symbol, no JSON, no words, and no explanation.\n"
        "If the requested value is not explicitly present in the snippets, return an empty response.\n"
        f"Snippets:\n{snippet_text}"
    )


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


async def extract_groq_fallback_metric(
    ticker: str,
    snippets: list[str] | None = None,
    metric_label: str | None = None,
) -> GroqFallbackResult:
    if not snippets and not GROQ_LENIENT_FALLBACK:
        logger.warning("[%s] Groq fallback skipped: no search snippets to parse", ticker)
        return GroqFallbackResult(metric=None, note="No search snippets to parse")

    api_keys = _api_keys()
    if not api_keys:
        logger.warning("[%s] Groq fallback skipped: GROQ_API_KEY or GROQ_API_KEYS is not set", ticker)
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
            {
                "role": "user",
                "content": _build_prompt(ticker, snippets, metric_label),
            },
        ],
    }

    last_error: str | None = None
    for index, api_key in enumerate(api_keys, start=1):
        try:
            result = await _request_groq(api_key, payload)
            if result.metric:
                logger.info(
                    "[%s] Groq fallback extracted metric=%s confidence=%s key_index=%d",
                    ticker,
                    result.metric,
                    result.confidence,
                    index,
                )
            else:
                logger.warning("[%s] Groq fallback returned no metric key_index=%d", ticker, index)
            return result
        except Exception as exc:
            last_error = str(exc)
            logger.warning("[%s] Groq fallback key_index=%d failed: %s", ticker, index, exc)

    return GroqFallbackResult(metric=None, note=last_error)
