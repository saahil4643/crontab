from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from config import SourceKey
from logging_setup import setup_logging
from resilient_collector.env_loader import load_env_file
from resilient_collector.orchestrator import SHEET_SOURCES, run_sheet_collector


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
OUT_FILE = LOG_DIR / "resilient_sheet_run.out"
PID_FILE = LOG_DIR / "resilient_sheet_run.pid"


def parse_sources(raw: list[str] | None) -> set[SourceKey] | None:
    if not raw:
        return None
    selected: set[SourceKey] = set()
    for source in raw:
        if source not in SHEET_SOURCES:
            allowed = ", ".join(sorted(SHEET_SOURCES))
            raise argparse.ArgumentTypeError(f"Unknown source {source!r}. Choose from: {allowed}")
        selected.add(source)
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the resilient sheet collector")
    parser.add_argument("--background", action="store_true", help="Run detached and write PID/log files")
    parser.add_argument("--dry-run", action="store_true", help="List sheet jobs without scraping or writing")
    parser.add_argument("--limit", type=int, default=None, help="Max sheet jobs to process")
    parser.add_argument("--source", action="append", dest="sources", metavar="SOURCE", help="Sheet source to process; repeatable")
    parser.add_argument("--write-failures", action="store_true", help="Write N/A on failed sheet jobs instead of leaving cells unchanged")
    return parser


def start_background(argv: list[str]) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    child_args = [arg for arg in argv if arg != "--background"]
    command = [sys.executable, str(Path(__file__).resolve()), *child_args]
    with OUT_FILE.open("ab", buffering=0) as output:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
    print(f"started pid {process.pid}")
    print(f"log {OUT_FILE.relative_to(ROOT)}")
    return 0


async def run(args: argparse.Namespace) -> None:
    await run_sheet_collector(
        dry_run=args.dry_run,
        limit=args.limit,
        sources=parse_sources(args.sources),
        write_failures=args.write_failures,
    )


def main() -> int:
    os.chdir(ROOT)
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()
    if args.background:
        return start_background(sys.argv[1:])
    setup_logging()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
