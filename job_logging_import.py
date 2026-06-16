"""Production cron_scheduler job_logging, with inline fallback for local runs."""

from __future__ import annotations

import importlib
import logging
import sys
import uuid
from datetime import date
from pathlib import Path
from types import ModuleType

_SCHEDULER_ROOT = Path("/home/raffay/cron_scheduler")


def daily_log_path(job_name: str, log_dir: str, *, role: str = "worker") -> str:
    day = date.today().isoformat()
    path = Path(log_dir) / f"{job_name}-{role}-{day}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def configure_job_logger(
    logger_name: str,
    job_name: str,
    log_dir: str,
    *,
    role: str = "worker",
    also_stdout: bool = False,
) -> tuple[logging.Logger, str, str]:
    del also_stdout
    log_path = daily_log_path(job_name, log_dir, role=role)
    run_id = uuid.uuid4().hex[:8]
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    return logger, log_path, run_id


def log_run_banner(logger: logging.Logger, job_name: str, role: str) -> None:
    logger.info("=== %s | %s run start ===", job_name, role)


class _StdoutTee:
    def __init__(self, terminal, log_file) -> None:
        self.terminal = terminal
        self.log_file = log_file

    def write(self, message: str) -> None:
        self.terminal.write(message)
        self.log_file.write(message)

    def flush(self) -> None:
        self.terminal.flush()
        self.log_file.flush()


def enable_stdout_tee(log_path: str):
    log_file = open(log_path, "a", encoding="utf-8")
    tee = _StdoutTee(sys.stdout, log_file)
    sys.stdout = tee
    return tee


def disable_stdout_tee(handle) -> None:
    if handle is None:
        return
    sys.stdout = handle.terminal
    handle.log_file.close()


def get_job_logging() -> ModuleType:
    if _SCHEDULER_ROOT.is_dir():
        root = str(_SCHEDULER_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            return importlib.import_module("job_logging")
        except ImportError:
            pass
    return sys.modules[__name__]
