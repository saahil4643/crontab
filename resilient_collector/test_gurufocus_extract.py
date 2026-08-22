"""Unit tests for GuruFocus trusted Fair Value / earnings parsers (no Playwright)."""

from __future__ import annotations

import unittest

from resilient_collector.extractors import (
    _extract_gurufocus_earnings_value,
    _is_gurufocus_fair_value_label,
    _parse_gurufocus_dollar_cell,
)


class ParseDollarCellTests(unittest.TestCase):
    def test_parses_spaced_dollar_cell(self) -> None:
        self.assertEqual(_parse_gurufocus_dollar_cell("$ 8.48"), "8.48")

    def test_parses_compact_dollar_cell(self) -> None:
        self.assertEqual(_parse_gurufocus_dollar_cell("$8.48"), "8.48")

    def test_parses_negative(self) -> None:
        self.assertEqual(_parse_gurufocus_dollar_cell("$-18.44"), "-18.44")

    def test_rejects_large_body_blob(self) -> None:
        blob = (
            "Fair Value Margin of Safety Stock Price $16.86 Dec25 10.77 "
            "TTM 12.56 and lots of other page noise that must not be scraped"
        )
        self.assertIsNone(_parse_gurufocus_dollar_cell(blob))

    def test_rejects_ambiguous_multi_dollar(self) -> None:
        self.assertIsNone(_parse_gurufocus_dollar_cell("$8.48 and also $10.77"))

    def test_rejects_hqi_style_wrong_match_blob(self) -> None:
        self.assertIsNone(
            _parse_gurufocus_dollar_cell("Fair Value 10.77 somewhere near Stock Price $16.86")
        )


class FairValueLabelTests(unittest.TestCase):
    def test_accepts_fair_value_tips(self) -> None:
        self.assertTrue(_is_gurufocus_fair_value_label("Fair Value"))
        self.assertTrue(_is_gurufocus_fair_value_label("  Fair Value  \n"))

    def test_rejects_saved_fair_values(self) -> None:
        self.assertFalse(_is_gurufocus_fair_value_label("Saved Fair Values"))
        self.assertFalse(_is_gurufocus_fair_value_label("Fair Values"))


class EarningsH1Tests(unittest.TestCase):
    def test_parses_term_h1(self) -> None:
        h1 = (
            "HQI (HireQuest) Intrinsic Value: DCF (Earnings Based)"
            ": $8.48 (As of Aug. 21, 2026)"
        )
        self.assertEqual(_extract_gurufocus_earnings_value(h1), "8.48")


if __name__ == "__main__":
    unittest.main()
