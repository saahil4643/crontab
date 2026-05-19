"""Data models for scrape jobs."""

from __future__ import annotations

from dataclasses import dataclass

from config import SourceKey


@dataclass(frozen=True)
class ScrapeJob:
    """One cell to fill on the Missing Value tab."""

    row: int  # 1-based sheet row
    ticker: str
    source: SourceKey
    value_col: str
    value_col_index: int
    label: str
    security_type: str = "nasdaq"  # Alpha Spread URL segment from column AB
