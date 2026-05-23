from __future__ import annotations

import argparse
import asyncio
import os
import shutil
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
XVFB_MARKER = "RESILIENT_XVFB_ACTIVE"


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    parser.add_argument("--no-xvfb", action="store_true", help="Do not auto-wrap the run in xvfb-run")
    parser.add_argument("--dry-run", action="store_true", help="List sheet jobs without scraping or writing")
    parser.add_argument("--limit", type=int, default=None, help="Max sheet jobs to process")
    parser.add_argument("--source", action="append", dest="sources", metavar="SOURCE", help="Sheet source to process; repeatable")
    return parser


def should_use_xvfb(args: argparse.Namespace) -> bool:
    return (
        not args.no_xvfb
        and env_bool("RESILIENT_USE_XVFB", True)
        and not os.environ.get("DISPLAY")
        and os.environ.get(XVFB_MARKER) != "1"
    )


def with_xvfb(command: list[str]) -> tuple[list[str], dict[str, str]]:
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        raise RuntimeError("xvfb-run is not installed. Install xvfb or run with --no-xvfb.")
    env = os.environ.copy()
    env[XVFB_MARKER] = "1"
    env.setdefault("RESILIENT_HEADLESS", "0")
    return [xvfb, "-a", *command], env


def maybe_reexec_with_xvfb(args: argparse.Namespace) -> None:
    if not should_use_xvfb(args):
        return
    command, env = with_xvfb([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    os.execvpe(command[0], command, env)


def start_background(argv: list[str]) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    child_args = [arg for arg in argv if arg != "--background"]
    command = [sys.executable, str(Path(__file__).resolve()), *child_args]
    env = os.environ.copy()
    parser = build_parser()
    args = parser.parse_args(argv)
    if should_use_xvfb(args):
        command, env = with_xvfb(command)
    with OUT_FILE.open("ab", buffering=0) as output:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
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
    )


def main() -> int:
    os.chdir(ROOT)
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()
    if args.background:
        return start_background(sys.argv[1:])
    maybe_reexec_with_xvfb(args)
    setup_logging()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
