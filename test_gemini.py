#!/usr/bin/env python3
"""
Test script to verify Google Gemini API valuation extraction for GuruFocus and Alpha Spread.

Usage:
    python test_gemini.py --ticker AAPL --source gurufocus
    python test_gemini.py --ticker MSFT --source alphaspread
    python test_gemini.py --ticker NVDA --api-key YOUR_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from resilient_collector.env_loader import load_env_file
from resilient_collector.gemini_collector import (
    DEFAULT_GEMINI_MODEL,
    fetch_metric_from_gemini,
    gemini_is_configured,
    get_gemini_api_key,
)


async def main_async() -> int:
    load_env_file()

    parser = argparse.ArgumentParser(description="Test Google Gemini API valuation extraction with Google Search.")
    parser.add_argument("--ticker", default="AAPL", help="Stock ticker to query (default: AAPL)")
    parser.add_argument(
        "--source",
        default="gurufocus",
        choices=["gurufocus", "alphaspread", "valueinvesting"],
        help="Valuation source to look up (default: gurufocus)",
    )
    parser.add_argument("--api-key", default="", help="Explicit Gemini API key (or set GEMINI_API_KEY in .env)")
    parser.add_argument("--model", default="", help=f"Gemini model to use (default: {DEFAULT_GEMINI_MODEL})")
    args = parser.parse_args()

    if args.api_key:
        os.environ["GEMINI_API_KEY"] = args.api_key
    if args.model:
        os.environ["GEMINI_MODEL"] = args.model

    ticker = args.ticker.strip().upper()
    source = args.source.strip().lower()

    key = get_gemini_api_key()
    preview = f"{key[:8]}...{key[-4:]} (len: {len(key)})" if len(key) > 12 else f"'{key}' (len: {len(key)})"

    print("=" * 65)
    print(f"[*] Testing Google Gemini API Valuation Extractor")
    print(f"[*] Target Ticker: {ticker}")
    print(f"[*] Valuation Source: {source}")
    print(f"[*] Model: {os.environ.get('GEMINI_MODEL', DEFAULT_GEMINI_MODEL)}")
    print(f"[*] API Key: {preview if key else 'NOT FOUND'}")
    print("=" * 65)

    if not gemini_is_configured():
        print("[!] ERROR: No Gemini API key found!")
        print("    1. Go to https://aistudio.google.com/app/apikey to get a 100% FREE key.")
        print("    2. Add GEMINI_API_KEY=AIzaSy... to your .env file or pass --api-key <key>.")
        return 1

    print(f"[*] Asking Gemini (with Google Search grounding) for {ticker} ({source})...")
    start = asyncio.get_event_loop().time()
    metric = await fetch_metric_from_gemini(ticker, source)
    elapsed = asyncio.get_event_loop().time() - start

    print("\n" + "=" * 65)
    if metric:
        print(f"[SUCCESS] {ticker} {source} DCF Fair Value -> ${metric}")
        print(f"[*] Time taken: {elapsed:.2f} seconds")
        print("=" * 65)
        return 0
    else:
        print(f"[X FAILED] Could not extract {source} metric for {ticker} using Gemini API.")
        print("    Check API key permissions, rate limits, or try another model.")
        print("=" * 65)
        return 1


def main() -> None:
    code = asyncio.run(main_async())
    sys.exit(code)


if __name__ == "__main__":
    main()
