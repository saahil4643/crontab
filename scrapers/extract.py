from __future__ import annotations

import re
from typing import Any, Optional, Sequence


def normalize_numeric_string(raw: str) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text or text in (".", "-", "-."):
        return None

    try:
        number = float(text)
    except ValueError:
        return None

    if negative:
        number = -abs(number)

    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.2f}"


def format_for_sheet(value: str) -> str:
    normalized = normalize_numeric_string(value)
    return normalized if normalized is not None else value.strip()


def _key_matches(key: str, hints: Sequence[str]) -> bool:
    key_lower = key.lower().replace("_", "").replace("-", "")
    return any(h in key_lower for h in hints)


def _coerce_number(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return normalize_numeric_string(str(value))
    if isinstance(value, str):
        return normalize_numeric_string(value)
    if isinstance(value, dict):
        for candidate_key in ("raw", "fmt", "value"):
            if candidate_key in value:
                coerced = _coerce_number(value[candidate_key])
                if coerced is not None:
                    return coerced
    return None


def walk_json_for_value(
    obj: Any,
    key_hints: Sequence[str],
    *,
    depth: int = 0,
) -> Optional[str]:
    if depth > 14:
        return None

    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(key, str) and _key_matches(key, key_hints):
                found = _coerce_number(val)
                if found is not None:
                    return found
            nested = walk_json_for_value(val, key_hints, depth=depth + 1)
            if nested is not None:
                return nested
        for val in obj.values():
            nested = walk_json_for_value(val, key_hints, depth=depth + 1)
            if nested is not None:
                return nested

    if isinstance(obj, list):
        for item in obj:
            nested = walk_json_for_value(item, key_hints, depth=depth + 1)
            if nested is not None:
                return nested

    return None


def extract_from_json_payloads(
    payloads: Sequence[Any],
    key_hints: Sequence[str],
) -> Optional[str]:
    for payload in payloads:
        body = payload.body if hasattr(payload, "body") else payload
        found = walk_json_for_value(body, key_hints)
        if found is not None:
            return found
    return None


def extract_from_text(text: str, patterns: Sequence[str]) -> Optional[str]:
    if not text:
        return None
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            group = match.group(1) if match.lastindex else match.group(0)
            normalized = normalize_numeric_string(group)
            if normalized is not None:
                return normalized
    return None
