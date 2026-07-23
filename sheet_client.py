"""Google Sheets read/write for Missing Value tab."""

from __future__ import annotations

import logging
import os

import gspread
from gspread import Worksheet

from config import (
    MISSING_VALUE_BLOCKS,
    MISSING_VALUE_START_ROW,
    SHEET_ID,
    SHEET_URL,
    WORKSHEET_MISSING_VALUE,
)
from credentials import get_credentials
from models import ScrapeJob
from resilient_collector.progress_tracker import get_completed_rows
from sheet_utils import (
    col_letter_to_index,
    has_stored_cell_value,
    is_missing_cell_value,
    is_us_market_index,
    normalize_security_type,
)

logger = logging.getLogger(__name__)

_HEADER_TICKERS = frozenset({"STOCK NAME", "TICKER", "SYMBOL"})


def _refresh_all_cells() -> bool:
    raw = os.environ.get("RESILIENT_REFRESH_MODE", "all").strip().lower()
    return raw not in {"missing_only", "missing", "empty_only"}


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

    def collect_jobs(self, start_row: int = MISSING_VALUE_START_ROW) -> list[ScrapeJob]:
        """
        Queue sheet cells for refresh.

        US (Nasdaq/NYSE): scrape every value column (missing or existing).
        Non-US: skip scraping; queue clear-only jobs when a stale value exists.
        """
        jobs: list[ScrapeJob] = []
        refresh_all = _refresh_all_cells()
        skipped_existing = 0
        skipped_non_us_empty = 0
        clear_non_us = 0
        skipped_completed = 0
        
        completed_cache = {}
        for block in MISSING_VALUE_BLOCKS:
            completed_cache[block.source] = get_completed_rows(block.source)
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

                current_value = ""
                if value_idx < len(row):
                    current_value = str(row[value_idx]).strip()

                index_cell = ""
                if index_idx < len(row):
                    index_cell = str(row[index_idx]).strip()
                security_type = normalize_security_type(index_cell)

                if not is_us_market_index(index_cell):
                    if has_stored_cell_value(current_value):
                        jobs.append(
                            ScrapeJob(
                                row=row_num,
                                ticker=ticker,
                                source=block.source,
                                value_col=block.value_col,
                                value_col_index=col_letter_to_index(block.value_col),
                                label=block.label,
                                current_value=current_value,
                                security_type=security_type,
                                index_label=index_cell,
                                clear_only=True,
                            )
                        )
                        clear_non_us += 1
                    else:
                        skipped_non_us_empty += 1
                    continue

                if not refresh_all and not is_missing_cell_value(current_value):
                    skipped_existing += 1
                    continue

                if row_num in completed_cache[block.source]:
                    skipped_completed += 1
                    continue

                jobs.append(
                    ScrapeJob(
                        row=row_num,
                        ticker=ticker,
                        source=block.source,
                        value_col=block.value_col,
                        value_col_index=col_letter_to_index(block.value_col),
                        label=block.label,
                        current_value=current_value,
                        security_type=security_type,
                        index_label=index_cell,
                    )
                )

        logger.info(
            "Queued %d jobs (%d scrape, %d non-US clear, %d skipped existing, "
            "%d skipped completed, %d non-US empty skipped) refresh_mode=%s",
            len(jobs),
            len(jobs) - clear_non_us,
            clear_non_us,
            skipped_existing,
            skipped_completed,
            skipped_non_us_empty,
            os.environ.get("RESILIENT_REFRESH_MODE", "all"),
        )
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

    def clear_value(self, job: ScrapeJob) -> None:
        self._worksheet.update_cell(job.row, job.value_col_index, "")
        logger.info(
            "Cleared row %d col %s (%s) %s old=%r",
            job.row,
            job.value_col,
            job.label,
            job.ticker,
            job.current_value,
        )

    def apply_cell_updates(self, updates: list) -> None:
        """Apply cell updates in one batch_update call (items need .job and .value)."""
        if not updates:
            return
        payload = [
            {
                "range": f"{update.job.value_col}{update.job.row}",
                "values": [[update.value]],
            }
            for update in updates
        ]
        self._worksheet.batch_update(payload, value_input_option="USER_ENTERED")
