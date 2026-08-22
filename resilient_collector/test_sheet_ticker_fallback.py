"""Unit tests for same-row ticker/index fallback (no Sheets API)."""

from __future__ import annotations

import unittest

from config import SheetColumnBlock
from sheet_client import resolve_row_index, resolve_row_ticker

_BLOCKS = (
    SheetColumnBlock("alphaspread", "B", "C", "D", "Alpha Spread"),
    SheetColumnBlock("valueinvesting", "G", "H", "I", "ValueIo"),
    SheetColumnBlock("gurufocus", "M", "N", "O", "GuruFocus"),
    SheetColumnBlock("price_target_low", "R", "S", "T", "Price Target Low"),
)


def _row_from_letters(**cells: str) -> list[str]:
    """Build a 0-based row list from column-letter keys (A=0)."""
    width = 22
    row = [""] * width
    for letter, value in cells.items():
        idx = ord(letter.upper()) - ord("A")
        row[idx] = value
    return row


class TickerFallbackTests(unittest.TestCase):
    def test_uses_own_ticker_when_present(self) -> None:
        row = _row_from_letters(C="TSM", H="MSFT", N="AAPL")
        vi = _BLOCKS[1]
        self.assertEqual(resolve_row_ticker(row, vi, _BLOCKS), "MSFT")

    def test_vi_uses_alphaspread_ticker_when_h_empty(self) -> None:
        row = _row_from_letters(C="HQI", N="HQI")
        vi = _BLOCKS[1]
        self.assertEqual(resolve_row_ticker(row, vi, _BLOCKS), "HQI")

    def test_yahoo_uses_alphaspread_ticker_when_s_empty(self) -> None:
        row = _row_from_letters(C="HQI")
        yahoo = _BLOCKS[3]
        self.assertEqual(resolve_row_ticker(row, yahoo, _BLOCKS), "HQI")

    def test_empty_when_no_tickers(self) -> None:
        row = _row_from_letters(B="NYSE")
        self.assertEqual(resolve_row_ticker(row, _BLOCKS[1], _BLOCKS), "")


class IndexFallbackTests(unittest.TestCase):
    def test_vi_uses_alphaspread_index_when_g_empty(self) -> None:
        row = _row_from_letters(B="Nasdaq", C="HQI")
        vi = _BLOCKS[1]
        self.assertEqual(resolve_row_index(row, vi, _BLOCKS), "Nasdaq")

    def test_uses_own_index_when_present(self) -> None:
        row = _row_from_letters(B="NYSE", G="Nasdaq")
        vi = _BLOCKS[1]
        self.assertEqual(resolve_row_index(row, vi, _BLOCKS), "Nasdaq")


if __name__ == "__main__":
    unittest.main()
