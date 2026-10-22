"""Google Sheets read/write for Missing Value tab."""

from __future__ import annotations

import logging
import os
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module=r"google.*")
warnings.filterwarnings("ignore", message=r".*Python version 3\.9.*", category=FutureWarning)

import gspread
from gspread import Worksheet

from config import (
    MISSING_VALUE_BLOCKS,
    MISSING_VALUE_START_ROW,
    SHEET_ID,
    SHEET_URL,
    SheetColumnBlock,
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


def _cell_text(row: list, col_letter: str, *, upper: bool = False) -> str:
    idx = col_letter_to_index(col_letter) - 1
    if idx < 0 or idx >= len(row):
        return ""
    text = str(row[idx]).strip()
    return text.upper() if upper else text


def resolve_row_ticker(
    row: list,
    block: SheetColumnBlock,
    blocks: tuple[SheetColumnBlock, ...] = MISSING_VALUE_BLOCKS,
) -> str:
    """Use this source's ticker column, else another filled ticker on the same row (C/N)."""
    ticker = _cell_text(row, block.ticker_col, upper=True)
    if ticker and ticker not in _HEADER_TICKERS:
        return ticker
    for other in blocks:
        candidate = _cell_text(row, other.ticker_col, upper=True)
        if candidate and candidate not in _HEADER_TICKERS:
            return candidate
    return ""


def resolve_row_index(
    row: list,
    block: SheetColumnBlock,
    blocks: tuple[SheetColumnBlock, ...] = MISSING_VALUE_BLOCKS,
) -> str:
    """Use this source's index column, else another filled index on the same row (B/M)."""
    index_cell = _cell_text(row, block.index_col)
    if index_cell:
        return index_cell
    for other in blocks:
        candidate = _cell_text(row, other.index_col)
        if candidate:
            return candidate
    return ""


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
        If a source ticker/index cell is empty, reuse another filled ticker/index
        on the same row (typically Alpha Spread C/B or GuruFocus N/M).
        """
        jobs: list[ScrapeJob] = []
        refresh_all = _refresh_all_cells()
        skipped_existing = 0
        skipped_non_us_empty = 0
        clear_non_us = 0
        skipped_completed = 0
        ticker_fallbacks = 0
        index_fallbacks = 0

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
                value_idx = col_letter_to_index(block.value_col) - 1

                own_ticker = _cell_text(row, block.ticker_col, upper=True)
                ticker = resolve_row_ticker(row, block)
                if not ticker or ticker in _HEADER_TICKERS:
                    continue
                if own_ticker != ticker:
                    ticker_fallbacks += 1

                current_value = ""
                if value_idx < len(row):
                    current_value = str(row[value_idx]).strip()

                own_index = _cell_text(row, block.index_col)
                index_cell = resolve_row_index(row, block)
                if own_index != index_cell:
                    index_fallbacks += 1
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
            "%d skipped completed, %d non-US empty skipped, "
            "%d ticker fallbacks, %d index fallbacks) refresh_mode=%s",
            len(jobs),
            len(jobs) - clear_non_us,
            clear_non_us,
            skipped_existing,
            skipped_completed,
            skipped_non_us_empty,
            ticker_fallbacks,
            index_fallbacks,
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
