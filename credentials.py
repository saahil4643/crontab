"""Google service account credentials (cron 57 pattern)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.oauth2.service_account import Credentials

from config import CREDENTIALS_PRIMARY, GOOGLE_SCOPES

logger = logging.getLogger(__name__)
_PACKAGE_DIR = Path(__file__).resolve().parent


def get_credentials() -> Credentials:
    primary = Path(CREDENTIALS_PRIMARY)
    fallback = _PACKAGE_DIR / "keys.json"

    if primary.is_file():
        logger.info("Using credentials: %s", primary)
        return Credentials.from_service_account_file(str(primary), scopes=GOOGLE_SCOPES)

    if fallback.is_file():
        logger.warning("Primary key missing; using fallback: %s", fallback)
        return Credentials.from_service_account_file(str(fallback), scopes=GOOGLE_SCOPES)

    raise FileNotFoundError(
        "No Google credentials found. Checked:\n"
        f"  - {primary}\n"
        f"  - {fallback}\n"
        "Share the sheet with the service account email from keys.json."
    )
