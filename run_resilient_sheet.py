from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import SourceKey, validate_config
from logging_setup import setup_logging
from resilient_collector.env_loader import load_env_file
from resilient_collector.orchestrator import SHEET_SOURCES, run_sheet_collector

ROOT = Path(__file__).resolve().parent
JOB_NAME = ROOT.name
LOG_DIR = ROOT / "logs"
PID_FILE = LOG_DIR / "resilient_sheet_run.pid"
XVFB_MARKER = "RESILIENT_XVFB_ACTIVE"

from job_logging_import import get_job_logging

_jl = get_job_logging()
daily_log_path = _jl.daily_log_path
disable_stdout_tee = _jl.disable_stdout_tee
enable_stdout_tee = _jl.enable_stdout_tee

worker_logger = logging.getLogger("cron62.worker")


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


def worker_log_path() -> str:
    return daily_log_path(JOB_NAME, str(LOG_DIR), role="worker")


def start_background(argv: list[str]) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    child_args = [arg for arg in argv if arg != "--background"]
    command = [sys.executable, str(Path(__file__).resolve()), *child_args]
    env = os.environ.copy()
    parser = build_parser()
    args = parser.parse_args(argv)
    if should_use_xvfb(args):
        command, env = with_xvfb(command)
    log_path = worker_log_path()
    with open(log_path, "ab", buffering=0) as output:
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
    print(f"log {Path(log_path).relative_to(ROOT)}")
    return 0


async def run(args: argparse.Namespace) -> None:
    await run_sheet_collector(
        dry_run=args.dry_run,
        limit=args.limit,
        sources=parse_sources(args.sources),
    )


def main(args: argparse.Namespace) -> int:
    started = time.time()
    worker_logger.info(
        "RUN_START | dry_run=%s | limit=%s | sources=%s",
        args.dry_run,
        args.limit,
        args.sources,
    )
    asyncio.run(run(args))
    elapsed = time.time() - started
    worker_logger.info("RUN_SUMMARY | status=completed | elapsed_sec=%.0f", elapsed)
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    load_env_file()
    validate_config()
    parser = build_parser()
    args = parser.parse_args()

    if args.background:
        raise SystemExit(start_background(sys.argv[1:]))

    maybe_reexec_with_xvfb(args)

    global_worker_logger, log_path = setup_logging()
    worker_logger = global_worker_logger
    tee_handle = enable_stdout_tee(log_path)

    status = "completed"
    exit_code = 0
    try:
        exit_code = main(args)
    except Exception as exc:
        status = "failed"
        exit_code = 1
        worker_logger.exception("RUN_SUMMARY | status=failed | error=%s", exc)
        print(f"Fatal error: {exc}")
        raise
    finally:
        worker_logger.info(
            "[LOG] Execution end: run_resilient_sheet.py | status=%s",
            status,
        )
        disable_stdout_tee(tee_handle)
    raise SystemExit(exit_code)
