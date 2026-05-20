"""Google Sheets read/write for Missing Value tab."""

from __future__ import annotations

import logging
import re
from typing import Optional

import gspread
from gspread import Worksheet

from config import (
    MISSING_PLACEHOLDERS,
    MISSING_VALUE_BLOCKS,
    MISSING_VALUE_START_ROW,
    SHEET_ID,
    SHEET_URL,
    WORKSHEET_MISSING_VALUE,
)
from credentials import get_credentials
from models import ScrapeJob
from sheet_utils import col_letter_to_index, normalize_security_type

logger = logging.getLogger(__name__)

_ZERO_PERCENT = re.compile(r"^0+(\.0+)?\s*%$", re.IGNORECASE)
_HEADER_TICKERS = frozenset({"STOCK NAME", "TICKER", "SYMBOL"})


def is_missing(value: Optional[str]) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if text in MISSING_PLACEHOLDERS:
        return True
    if _ZERO_PERCENT.match(text):
        return True
    return False


class DcfSheetClient:
    """Reads/writes the Missing Value worksheet."""

    def __init__(self) -> None:
        creds = get_credentials()
        gc = gspread.authorize(creds)
        self._spreadsheet = gc.open_by_url(SHEET_URL)
        self._worksheet: Worksheet = self._spreadsheet.worksheet(WORKSHEET_MISSING_VALUE)
        logger.info("Opened spreadsheet %s tab %r", SHEET_ID, WORKSHEET_MISSING_VALUE)

    @property
    def worksheet(self) -> Worksheet:
        return self._worksheet

    def list_worksheets(self) -> list[str]:
        return [ws.title for ws in self._spreadsheet.worksheets()]

    def collect_missing_jobs(self, start_row: int = MISSING_VALUE_START_ROW) -> list[ScrapeJob]:
        """
        Scan Missing Value source blocks and fill empty value columns.
        """
        jobs: list[ScrapeJob] = []
        all_values = self._worksheet.get_all_values()
        if not all_values:
            logger.warning("Sheet is empty")
            return jobs

        max_row = len(all_values)
        for row_num in range(start_row, max_row + 1):
            row = all_values[row_num - 1]
            for block in MISSING_VALUE_BLOCKS:
                index_idx = col_letter_to_index(block.index_col) - 1
                ticker_idx = col_letter_to_index(block.ticker_col) - 1
                value_idx = col_letter_to_index(block.value_col) - 1

                ticker = ""
                if ticker_idx < len(row):
                    ticker = str(row[ticker_idx]).strip().upper()
                if not ticker or ticker in _HEADER_TICKERS:
                    continue

                current = ""
                if value_idx < len(row):
                    current = str(row[value_idx]).strip()

                if not is_missing(current):
                    continue

                index_cell = ""
                if index_idx < len(row):
                    index_cell = str(row[index_idx]).strip()
                security_type = normalize_security_type(index_cell)

                jobs.append(
                    ScrapeJob(
                        row=row_num,
                        ticker=ticker,
                        source=block.source,
                        value_col=block.value_col,
                        value_col_index=value_idx + 1,
                        label=block.label,
                        security_type=security_type,
                    )
                )

        logger.info("Found %d missing value cells to fill", len(jobs))
        return jobs

    def write_value(self, job: ScrapeJob, value: str) -> None:
        self._worksheet.update_cell(job.row, job.value_col_index, value)
        logger.info(
            "Updated row %d col %s (%s) %s -> %r",
            job.row,
            job.value_col,
            job.label,
            job.ticker,
            value,
        )
