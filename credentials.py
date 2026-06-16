"""Google service account credentials (cron 57 pattern)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.oauth2.service_account import Credentials

from config import CREDENTIALS_PRIMARY, GOOGLE_SCOPES

logger = logging.getLogger(__name__)
_PACKAGE_DIR = Path(__file__).resolve().parent


def _resolve_credentials_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (_PACKAGE_DIR / path).resolve()
    return path


def _credentials_candidates() -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    env_path = os.environ.get("GOOGLE_CREDENTIALS_PATH", "").strip()
    if env_path:
        add(_resolve_credentials_path(env_path))

    add(_PACKAGE_DIR / "keys.json")
    add(_resolve_credentials_path(CREDENTIALS_PRIMARY))
    return candidates


def get_credentials() -> Credentials:
    checked: list[str] = []
    for path in _credentials_candidates():
        checked.append(str(path))
        if path.is_file():
            logger.info("Using credentials: %s", path)
            return Credentials.from_service_account_file(str(path), scopes=GOOGLE_SCOPES)

    raise FileNotFoundError(
        "No Google credentials found. Checked:\n"
        + "".join(f"  - {path}\n" for path in checked)
        + "Set GOOGLE_CREDENTIALS_PATH in .env or place keys.json in the project root.\n"
        + "Share the sheet with the service account email from keys.json."
    )
