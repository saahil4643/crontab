"""Playwright scrapers per data source."""

from scrapers.capture import NetworkCapture, PageCapture
from scrapers.extract import ExtractionResult, format_for_sheet, run_extraction_pipeline
from scrapers.alpha_spread import AlphaSpreadScraper
from scrapers.gurufocus import (
    GuruFocusScraper,
    extract_fair_value_from_dcf_calculator,
    prepare_dcf_calculator_page,
    wait_for_fair_value_on_calculator,
)
from scrapers.price_target import PriceTargetScraper
from scrapers.value_investing import ValueInvestingScraper

SCRAPERS_BY_SOURCE = {
    "alphaspread": AlphaSpreadScraper(),
    "valueinvesting": ValueInvestingScraper(),
    "gurufocus": GuruFocusScraper(),
    "price_target_low": PriceTargetScraper(kind="low"),
    "price_target_avg": PriceTargetScraper(kind="avg"),
    "price_target_high": PriceTargetScraper(kind="high"),
}

__all__ = [
    "SCRAPERS_BY_SOURCE",
    "NetworkCapture",
    "PageCapture",
    "ExtractionResult",
    "format_for_sheet",
    "run_extraction_pipeline",
    "AlphaSpreadScraper",
    "ValueInvestingScraper",
    "GuruFocusScraper",
    "PriceTargetScraper",
]
