#!/usr/bin/env python3
"""
Raffay cron 62 — fill missing DCF / price-target values on Missing Value tab.

Test JSON only (no Google Sheet):
  python scripts/capture_json.py --source gurufocus --ticker AAPL --try-extract

Production (server — use virtual display for GuruFocus):
  scripts/run_production.sh --dry-run
  scripts/run_production.sh --source gurufocus --limit 5
  python run.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as script from package directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import SourceKey
from logging_setup import setup_logging
from runner import run


def _parse_sources(raw: list[str] | None) -> set[SourceKey] | None:
    if not raw:
        return None
    allowed = {
        "alphaspread",
        "valueinvesting",
        "gurufocus",
        "price_target_low",
        "price_target_avg",
        "price_target_high",
    }
    out: set[SourceKey] = set()
    for s in raw:
        if s not in allowed:
            raise argparse.ArgumentTypeError(
                f"Unknown source {s!r}. Choose from: {', '.join(sorted(allowed))}"
            )
        out.add(s)  # type: ignore[arg-type]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Raffay cron 62 — DCF gap fill")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List missing cells without scraping or writing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of cells to process",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="SOURCE",
        help="Only run given source(s); repeatable",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Force visible browser (all sources in each batch)",
    )
    args = parser.parse_args()

    setup_logging()
    try:
        run(
            dry_run=args.dry_run,
            limit=args.limit,
            sources=_parse_sources(args.sources),
            force_headed=args.headed,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except PermissionError:
        print(
            "Cannot access the spreadsheet (403). Share the sheet with the "
            "service account email in keys.json (Editor).",
            file=sys.stderr,
        )
        return 1
    except Exception:
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
