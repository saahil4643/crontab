#!/usr/bin/env python3
"""
Test script to verify Grok API valuation extraction for GuruFocus and Alpha Spread.

Usage:
    python test_grok.py --ticker AAPL --source gurufocus
    python test_grok.py --ticker MSFT --source alphaspread
    python test_grok.py --ticker NVDA --api-key YOUR_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from resilient_collector.env_loader import load_env_file
from resilient_collector.grok_collector import (
    DEFAULT_GROK_MODEL,
    fetch_metric_from_grok,
    get_grok_api_key,
    grok_is_configured,
)


async def main_async() -> int:
    load_env_file()

    parser = argparse.ArgumentParser(description="Test Grok (xAI) valuation extraction.")
    parser.add_argument("--ticker", default="AAPL", help="Stock ticker to query (default: AAPL)")
    parser.add_argument(
        "--source",
        default="gurufocus",
        choices=["gurufocus", "alphaspread", "valueinvesting"],
        help="Valuation source to look up (default: gurufocus)",
    )
    parser.add_argument("--api-key", default="", help="Explicit xAI API key (or set XAI_API_KEY in .env)")
    parser.add_argument("--model", default="", help=f"Grok model to use (default: {DEFAULT_GROK_MODEL})")
    args = parser.parse_args()

    if args.api_key:
        os.environ["XAI_API_KEY"] = args.api_key
    if args.model:
        os.environ["GROK_MODEL"] = args.model

    ticker = args.ticker.strip().upper()
    source = args.source.strip().lower()

    key = get_grok_api_key()
    preview = f"{key[:8]}...{key[-4:]} (len: {len(key)})" if len(key) > 12 else f"'{key}' (len: {len(key)})"

    print("=" * 65)
    print(f"[*] Testing Grok API Valuation Extractor")
    print(f"[*] Target Ticker: {ticker}")
    print(f"[*] Valuation Source: {source}")
    print(f"[*] Model: {os.environ.get('GROK_MODEL', DEFAULT_GROK_MODEL)}")
    print(f"[*] API Key: {preview if key else 'NOT FOUND'}")
    print("=" * 65)

    if not grok_is_configured():
        print("[!] ERROR: No xAI API key found!")
        print("    Add XAI_API_KEY=your_key_here to your .env file or pass --api-key <key>.")
        return 1

    print(f"[*] Asking Grok to search the web for {ticker} ({source})...")
    start = asyncio.get_event_loop().time()
    metric = await fetch_metric_from_grok(ticker, source)
    elapsed = asyncio.get_event_loop().time() - start

    print("\n" + "=" * 65)
    if metric:
        print(f"[✓ SUCCESS] {ticker} {source} DCF Fair Value -> ${metric}")
        print(f"[*] Time taken: {elapsed:.2f} seconds")
        print("=" * 65)
        return 0
    else:
        print(f"[X FAILED] Could not extract {source} metric for {ticker} using Grok API.")
        print("    Check API key permissions, rate limits, or try another model.")
        print("=" * 65)
        return 1


def main() -> None:
    code = asyncio.run(main_async())
    sys.exit(code)


if __name__ == "__main__":
    main()
