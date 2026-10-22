"""Unit tests for gemini_collector and ai_fallback modules."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from resilient_collector.ai_fallback import (
    ai_is_configured,
    ai_primary_sources,
    fetch_ai_valuation_metric,
)
from resilient_collector.gemini_collector import (
    _build_gemini_prompt,
    _parse_gemini_metric,
    fetch_metric_from_gemini,
    gemini_is_configured,
    get_gemini_api_key,
)


class GeminiCollectorTests(unittest.TestCase):
    def test_get_gemini_api_key(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyTest123"}):
            self.assertEqual(get_gemini_api_key(), "AIzaSyTest123")
            self.assertTrue(gemini_is_configured())

        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": '"AIzaSyWithQuotes"'}):
            self.assertEqual(get_gemini_api_key(), "AIzaSyWithQuotes")
            self.assertTrue(gemini_is_configured())

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_gemini_api_key(), "")
            self.assertFalse(gemini_is_configured())

    def test_parse_gemini_metric_clean_json(self) -> None:
        content = '{"ticker": "AAPL", "metric": 182.40, "source": "gurufocus"}'
        self.assertEqual(_parse_gemini_metric(content), "182.40")

    def test_parse_gemini_metric_dollar_string(self) -> None:
        content = '{"ticker": "MSFT", "metric": "$435.50", "source": "alphaspread"}'
        self.assertEqual(_parse_gemini_metric(content), "435.50")

    def test_parse_gemini_metric_markdown_block(self) -> None:
        content = '```json\n{"ticker": "GOOGL", "metric": 195.25, "source": "gurufocus"}\n```'
        self.assertEqual(_parse_gemini_metric(content), "195.25")

    def test_parse_gemini_metric_natural_language(self) -> None:
        content = "Based on GuruFocus DCF model, the intrinsic value is $175.80 per share."
        self.assertEqual(_parse_gemini_metric(content), "175.80")

    def test_build_gemini_prompt(self) -> None:
        prompt_gf = _build_gemini_prompt("aapl", "gurufocus")
        self.assertIn("GuruFocus", prompt_gf)
        self.assertIn("AAPL", prompt_gf)

        prompt_as = _build_gemini_prompt("nvda", "alphaspread")
        self.assertIn("Alpha Spread", prompt_as)
        self.assertIn("NVDA", prompt_as)


class GeminiAsyncApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_metric_from_gemini_no_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            metric = await fetch_metric_from_gemini("AAPL", "gurufocus")
            self.assertIsNone(metric)

    async def test_fetch_metric_from_gemini_mocked_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"ticker": "AAPL", "metric": 178.50, "source": "gurufocus"}'}
                        ]
                    }
                }
            ]
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyTest"}), patch("httpx.AsyncClient", return_value=mock_client):
            metric = await fetch_metric_from_gemini("AAPL", "gurufocus")
            self.assertEqual(metric, "178.50")


class AiFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_ai_primary_sources(self) -> None:
        with patch.dict(os.environ, {"AI_PRIMARY_FOR": "gurufocus, alphaspread"}):
            sources = ai_primary_sources()
            self.assertIn("gurufocus", sources)
            self.assertIn("alphaspread", sources)

    async def test_fetch_ai_valuation_metric_gemini_first(self) -> None:
        with patch("resilient_collector.ai_fallback.gemini_is_configured", return_value=True), \
             patch("resilient_collector.ai_fallback.fetch_metric_from_gemini", return_value="190.20") as mock_gemini:
            res = await fetch_ai_valuation_metric("AAPL", "gurufocus")
            self.assertEqual(res, "190.20")
            mock_gemini.assert_called_once_with("AAPL", "gurufocus")


if __name__ == "__main__":
    unittest.main()
