"""Data models for scrape jobs."""

from __future__ import annotations

from dataclasses import dataclass

from config import SourceKey


@dataclass(frozen=True)
class ScrapeJob:
    """One sheet cell to refresh."""

    row: int  # 1-based sheet row
    ticker: str
    source: SourceKey
    value_col: str
    value_col_index: int
    label: str
    current_value: str = ""
    security_type: str = "nasdaq"  # Alpha Spread URL segment from the source block's index column
    index_label: str = ""  # raw Index column (column G) for logging
    clear_only: bool = False  # non-US row: clear stale value without scraping
