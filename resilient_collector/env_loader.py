from __future__ import annotations

import os
from pathlib import Path


def _clean_value(value: str) -> str:
    value = value.strip()
    if value and value[0] not in {"'", '"'}:
        hash_at = value.find(" #")
        if hash_at >= 0:
            value = value[:hash_at].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path | None = None) -> None:
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _clean_value(value)
