"""Graceful shutdown, progress tracking, and single-instance locking for the worker."""

from __future__ import annotations

import asyncio
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import SourceKey

HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("RESILIENT_HEARTBEAT_SECONDS", "300"))


@dataclass
class WorkerProgress:
    total_jobs: int = 0
    jobs_done: int = 0
    batch: str = ""
    batch_index: int = 0
    batch_total: int = 0
    last_row: int | None = None
    last_ticker: str | None = None
    last_source: str | None = None
    extra: str = ""

    def snapshot(self) -> str:
        last = (
            f"{self.last_ticker} row={self.last_row} source={self.last_source}"
            if self.last_ticker
            else "none"
        )
        batch_part = ""
        if self.batch:
            batch_part = f" | batch={self.batch} {self.batch_index}/{self.batch_total}"
        extra_part = f" | {self.extra}" if self.extra else ""
        return (
            f"jobs_done={self.jobs_done}/{self.total_jobs}{batch_part} | last={last}{extra_part}"
        )


class ShutdownController:
    """Graceful shutdown controller with lazy, loop-aware asyncio.Event binding.

    In Python <= 3.9, creating an asyncio.Event outside an active loop binds it to
    the thread's default loop. When asyncio.run() later starts a new event loop,
    awaiting that event raises:
        RuntimeError: Task <...> got Future <...> attached to a different loop.
    This controller dynamically binds or rebinds the event to the active running loop.
    """

    def __init__(
        self,
        event: asyncio.Event | None = None,
        reason: str | None = None,
        progress: WorkerProgress | None = None,
    ) -> None:
        self._event = event
        self._loop: asyncio.AbstractEventLoop | None = (
            getattr(event, "_loop", None) if event is not None else None
        )
        self.reason = reason
        self.progress = progress if progress is not None else WorkerProgress()
        self._requested: bool = False

    @property
    def event(self) -> asyncio.Event:
        """Return an asyncio.Event bound to the currently running event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._event is None:
            self._event = asyncio.Event()
            self._loop = current_loop
            if self._requested:
                self._event.set()
        elif current_loop is not None:
            event_loop = getattr(self._event, "_loop", None) or self._loop
            if event_loop is not current_loop:
                was_set = self._requested or self._event.is_set()
                self._event = asyncio.Event()
                self._loop = current_loop
                if was_set:
                    self._event.set()
        return self._event

    @event.setter
    def event(self, value: asyncio.Event) -> None:
        self._event = value
        self._loop = getattr(value, "_loop", None)
        if self._requested:
            self._event.set()

    def request_shutdown(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self._requested = True
        if self._event is not None:
            target_loop = getattr(self._event, "_loop", None) or self._loop
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if (
                target_loop is not None
                and target_loop.is_running()
                and running_loop is not target_loop
            ):
                target_loop.call_soon_threadsafe(self._event.set)
            else:
                self._event.set()

    def is_requested(self) -> bool:
        return self._requested or (self._event is not None and self._event.is_set())

    def reset(self) -> None:
        """Reset state if reusing the controller across separate runs."""
        self._requested = False
        self.reason = None
        self._event = None
        self._loop = None

    def note_job(
        self,
        *,
        row: int,
        ticker: str,
        source: str,
        batch: str = "",
        batch_index: int = 0,
        batch_total: int = 0,
    ) -> None:
        self.progress.last_row = row
        self.progress.last_ticker = ticker
        self.progress.last_source = source
        if batch:
            self.progress.batch = batch
            self.progress.batch_index = batch_index
            self.progress.batch_total = batch_total

    def note_job_completed(self) -> None:
        self.progress.jobs_done += 1


def flush_log_handlers() -> None:
    for handler in logging.getLogger().handlers:
        flush = getattr(handler, "flush", None)
        if callable(flush):
            flush()


def log_run_summary(
    logger: logging.Logger,
    *,
    status: str,
    reason: str | None = None,
    elapsed_sec: float | None = None,
    progress: WorkerProgress | None = None,
    exit_code: int | None = None,
) -> None:
    parts = [f"status={status}"]
    if reason:
        parts.append(f"reason={reason}")
    if elapsed_sec is not None:
        parts.append(f"elapsed_sec={elapsed_sec:.0f}")
    if progress is not None:
        parts.append(progress.snapshot())
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    logger.info("RUN_SUMMARY | %s", " | ".join(parts))
    flush_log_handlers()


def install_signal_handlers(shutdown: ShutdownController, logger: logging.Logger) -> None:
    loop = asyncio.get_running_loop()

    def _handle(signame: str) -> None:
        shutdown.request_shutdown(f"signal:{signame}")
        logger.warning("RUN_SHUTDOWN | reason=signal:%s | requesting graceful stop", signame)

    for sig, signame in (
        (signal.SIGTERM, "SIGTERM"),
        (signal.SIGINT, "SIGINT"),
    ):
        try:
            loop.add_signal_handler(sig, _handle, signame)
        except (NotImplementedError, RuntimeError):
            # Windows / limited platforms or non-main thread execution
            try:
                signal.signal(sig, lambda _num, _frame, name=signame: _handle(name))
            except (ValueError, OSError):
                pass


async def heartbeat_loop(logger: logging.Logger, shutdown: ShutdownController) -> None:
    while not shutdown.is_requested():
        interval = float(os.environ.get("RESILIENT_HEARTBEAT_SECONDS", str(HEARTBEAT_INTERVAL_SECONDS)))
        try:
            await asyncio.wait_for(shutdown.event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            logger.info("RUN_HEARTBEAT | %s", shutdown.progress.snapshot())


async def interruptible_sleep(
    seconds: float | int,
    shutdown: ShutdownController | None,
    *,
    label: str = "pause",
) -> bool:
    """Sleep up to *seconds*. Returns False if shutdown was requested during the wait."""
    if shutdown is None:
        await asyncio.sleep(seconds)
        return True
    if shutdown.is_requested():
        return False
    try:
        await asyncio.wait_for(shutdown.event.wait(), timeout=seconds)
        logging.getLogger(__name__).info(
            "[schedule] %s interrupted after shutdown: %s",
            label,
            shutdown.reason,
        )
        return False
    except asyncio.TimeoutError:
        return True


def read_pid_file(pid_file: os.PathLike[str]) -> int | None:
    path = os.fspath(pid_file)
    if not os.path.isfile(path):
        return None
    try:
        return int(open(path, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return None


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # On Linux, inspect /proc/{pid}/cmdline to guard against stale PIDs recycled by unrelated processes
    cmdline_path = f"/proc/{pid}/cmdline"
    if os.path.exists(cmdline_path):
        try:
            with open(cmdline_path, "rb") as f:
                cmd = f.read().decode("utf-8", errors="ignore")
                if cmd and not any(sig in cmd for sig in ("python", "resilient", "xvfb")):
                    return False
        except OSError:
            pass
    return True


def existing_worker_pid(pid_file: os.PathLike[str]) -> int | None:
    pid = read_pid_file(pid_file)
    if pid is None:
        return None
    if is_process_alive(pid):
        return pid
    return None


def write_pid_file(pid_file: os.PathLike[str]) -> None:
    path = os.fspath(pid_file)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")


def remove_pid_file(pid_file: os.PathLike[str]) -> None:
    try:
        os.remove(os.fspath(pid_file))
    except FileNotFoundError:
        pass


def acquire_instance_lock(lock_file: os.PathLike[str]) -> int | None:
    path = os.fspath(lock_file)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    if fcntl is None:
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def release_instance_lock(lock_fd: int | None) -> None:
    if lock_fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
