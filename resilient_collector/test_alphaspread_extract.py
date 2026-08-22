"""Regression: Alpha Spread CSS stays on the July summary selectors."""

from __future__ import annotations

import inspect
import unittest

from resilient_collector import orchestrator
from resilient_collector.extractors import _alpha_css


class JulyAlphaSpreadExtractTests(unittest.TestCase):
    def test_css_uses_july_summary_selector_not_livewire(self) -> None:
        src = inspect.getsource(_alpha_css)
        self.assertIn("a.intrinsic-value-dcf-link div.detail", src)
        self.assertIn("valuation-scenario-value", src)
        locators = [line for line in src.splitlines() if "locator(" in line]
        joined = "\n".join(locators)
        self.assertNotIn("wire:key", joined)
        self.assertNotIn("dcf-base-case", joined)

    def test_pacing_stays_isolated_browser_defaults(self) -> None:
        src = inspect.getsource(orchestrator)
        self.assertIn('RESILIENT_ALPHASPREAD_CHUNK_SIZE", 1)', src)
        self.assertIn('RESILIENT_ALPHASPREAD_SLEEP_SECONDS", 8)', src)
        self.assertIn("isolated-browser", src)
        self.assertIn("run_alphaspread_job_with_watchdog", inspect.getsource(orchestrator._process_alphaspread_chunk))


if __name__ == "__main__":
    unittest.main()
