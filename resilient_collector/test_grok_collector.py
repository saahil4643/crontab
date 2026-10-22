"""Unit tests for grok_collector module."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from resilient_collector.grok_collector import (
    _build_valuation_prompt,
    _parse_grok_metric,
    fetch_metric_from_grok,
    get_grok_api_key,
    grok_is_configured,
)


class GrokCollectorTests(unittest.TestCase):
    def test_get_grok_api_key(self) -> None:
        with patch.dict(os.environ, {"XAI_API_KEY": "xai-test-123"}):
            self.assertEqual(get_grok_api_key(), "xai-test-123")
            self.assertTrue(grok_is_configured())

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_grok_api_key(), "")
            self.assertFalse(grok_is_configured())

    def test_parse_grok_metric_clean_json(self) -> None:
        content = '{"ticker": "AAPL", "metric": 177.31, "source": "gurufocus"}'
        self.assertEqual(_parse_grok_metric(content), "177.31")

    def test_parse_grok_metric_markdown_block(self) -> None:
        content = '```json\n{"ticker": "MSFT", "metric": "$481.04", "source": "alphaspread"}\n```'
        self.assertEqual(_parse_grok_metric(content), "481.04")

    def test_parse_grok_metric_regex_fallback(self) -> None:
        content = 'The Alpha Spread DCF intrinsic value for AAPL is "metric": 225.25 in the model.'
        self.assertEqual(_parse_grok_metric(content), "225.25")

    def test_build_valuation_prompt(self) -> None:
        prompt_gf = _build_valuation_prompt("aapl", "gurufocus")
        self.assertIn("GuruFocus", prompt_gf)
        self.assertIn("AAPL", prompt_gf)

        prompt_as = _build_valuation_prompt("tsla", "alphaspread")
        self.assertIn("Alpha Spread", prompt_as)
        self.assertIn("TSLA", prompt_as)


class GrokAsyncApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_metric_from_grok_no_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            metric = await fetch_metric_from_grok("AAPL", "gurufocus")
            self.assertIsNone(metric)

    async def test_fetch_metric_from_grok_mocked_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {"message": {"content": '{"ticker": "NVDA", "metric": 185.50, "source": "gurufocus"}'}}
            ]
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch.dict(os.environ, {"XAI_API_KEY": "test-key"}), patch("httpx.AsyncClient", return_value=mock_client):
            metric = await fetch_metric_from_grok("NVDA", "gurufocus")
            self.assertEqual(metric, "185.50")


if __name__ == "__main__":
    unittest.main()
