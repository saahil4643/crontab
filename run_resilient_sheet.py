from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from contextlib import suppress
from pathlib import Path

from config import SourceKey, validate_config
from logging_setup import setup_logging
from resilient_collector.env_loader import load_env_file
from resilient_collector.orchestrator import SHEET_SOURCES, run_sheet_collector
from worker_shutdown import (
    ShutdownController,
    acquire_instance_lock,
    existing_worker_pid,
    flush_log_handlers,
    heartbeat_loop,
    install_signal_handlers,
    log_run_summary,
    release_instance_lock,
    remove_pid_file,
    write_pid_file,
)

ROOT = Path(__file__).resolve().parent
JOB_NAME = ROOT.name
LOG_DIR = ROOT / "logs"
PID_FILE = LOG_DIR / "resilient_sheet_run.pid"
LOCK_FILE = LOG_DIR / "resilient_sheet_run.lock"
PHASE_RESULT_FILE = LOG_DIR / "worker_phase_result.json"
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


def write_phase_result(*, phase: str, status: str, reason: str | None, exit_code: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase,
        "status": status,
        "reason": reason,
        "exit_code": exit_code,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    tmp_path = PHASE_RESULT_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(PHASE_RESULT_FILE)


def start_background(argv: list[str]) -> int:
    running_pid = existing_worker_pid(PID_FILE)
    if running_pid is not None:
        print(f"already running pid {running_pid}")
        return 1

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


async def _run_worker(args: argparse.Namespace, shutdown: ShutdownController) -> list:
    install_signal_handlers(shutdown, worker_logger)
    heartbeat_task = asyncio.create_task(heartbeat_loop(worker_logger, shutdown))
    try:
        return await run_sheet_collector(
            dry_run=args.dry_run,
            limit=args.limit,
            sources=parse_sources(args.sources),
            shutdown=shutdown,
        )
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


def run_worker(args: argparse.Namespace, shutdown: ShutdownController) -> int:
    started = time.time()
    worker_logger.info(
        "RUN_START | dry_run=%s | limit=%s | sources=%s | pid=%s",
        args.dry_run,
        args.limit,
        args.sources,
        os.getpid(),
    )
    asyncio.run(_run_worker(args, shutdown))
    elapsed = time.time() - started
    if shutdown.is_requested():
        log_run_summary(
            worker_logger,
            status="interrupted",
            reason=shutdown.reason,
            elapsed_sec=elapsed,
            progress=shutdown.progress,
        )
        return 130
    log_run_summary(
        worker_logger,
        status="completed",
        elapsed_sec=elapsed,
        progress=shutdown.progress,
    )
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

    shutdown = ShutdownController()
    lock_fd: int | None = None
    started = time.time()
    status = "completed"
    exit_code = 0
    failure_reason: str | None = None

    try:
        running_pid = existing_worker_pid(PID_FILE)
        # Background + xvfb writes the launcher PID first; the real worker is a child
        # of that process, so treat parent PID as "us", not a competing instance.
        if (
            running_pid is not None
            and running_pid != os.getpid()
            and running_pid != os.getppid()
        ):
            status = "refused"
            failure_reason = f"already_running pid={running_pid}"
            exit_code = 1
            log_run_summary(
                worker_logger,
                status=status,
                reason=failure_reason,
                elapsed_sec=0,
                exit_code=exit_code,
            )
            raise SystemExit(exit_code)

        lock_fd = acquire_instance_lock(LOCK_FILE)
        if lock_fd is None:
            status = "refused"
            failure_reason = "instance_lock_busy"
            exit_code = 1
            log_run_summary(
                worker_logger,
                status=status,
                reason=failure_reason,
                elapsed_sec=0,
                exit_code=exit_code,
            )
            raise SystemExit(exit_code)

        write_pid_file(PID_FILE)
        exit_code = run_worker(args, shutdown)
        if shutdown.is_requested():
            status = "interrupted"
            failure_reason = shutdown.reason
        else:
            status = "completed"
    except SystemExit as exc:
        if exc.code not in (None, 0):
            if status == "completed":
                status = "failed"
                failure_reason = f"exit_code={exc.code}"
            exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        raise
    except KeyboardInterrupt:
        shutdown.request_shutdown("signal:KeyboardInterrupt")
        status = "interrupted"
        failure_reason = shutdown.reason
        exit_code = 130
        log_run_summary(
            worker_logger,
            status=status,
            reason=failure_reason,
            elapsed_sec=time.time() - started,
            progress=shutdown.progress,
            exit_code=exit_code,
        )
        raise SystemExit(exit_code)
    except BaseException as exc:
        status = "failed"
        failure_reason = str(exc)
        exit_code = 1
        worker_logger.exception("RUN_SUMMARY | status=failed | error=%s", exc)
        log_run_summary(
            worker_logger,
            status=status,
            reason=failure_reason,
            elapsed_sec=time.time() - started,
            progress=shutdown.progress,
            exit_code=exit_code,
        )
        print(f"Fatal error: {exc}")
        raise
    finally:
        worker_logger.info(
            "[LOG] Execution end: run_resilient_sheet.py | status=%s | reason=%s",
            status,
            failure_reason or shutdown.reason or "none",
        )
        cycle_phase = os.environ.get("RESILIENT_CYCLE_PHASE", "").strip()
        if cycle_phase:
            write_phase_result(
                phase=cycle_phase,
                status=status,
                reason=failure_reason or shutdown.reason,
                exit_code=exit_code,
            )
        remove_pid_file(PID_FILE)
        release_instance_lock(lock_fd)
        flush_log_handlers()
        disable_stdout_tee(tee_handle)

    raise SystemExit(exit_code)
