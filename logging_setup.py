"""Option B worker logging — daily file under project logs/ (same pattern as cron-55)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

CRON62_ROOT = Path(__file__).resolve().parent
JOB_NAME = CRON62_ROOT.name
LOG_DIR = CRON62_ROOT / "logs"

from job_logging_import import get_job_logging

_jl = get_job_logging()
configure_job_logger = _jl.configure_job_logger
log_run_banner = _jl.log_run_banner

_worker_logger = logging.getLogger("cron62.worker")


def setup_logging(*, also_stdout: bool = False) -> tuple[logging.Logger, str]:
    """
    Configure cron-62 worker logging.

    Writes to {JOB_NAME}-worker-{YYYY-MM-DD}.log; module loggers propagate to root.
    Returns (worker_logger, log_path).
    """
    global _worker_logger

    _worker_logger, log_path, run_id = configure_job_logger(
        "cron62.worker",
        JOB_NAME,
        str(LOG_DIR),
        role="worker",
        also_stdout=False,
    )
    log_run_banner(_worker_logger, JOB_NAME, "worker")
    _worker_logger.info("worker log_file=%s", log_path)

    fmt = logging.Formatter(
        f"%(asctime)s | {JOB_NAME} | worker | run={run_id} | %(levelname)s | %(message)s"
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.propagate = False

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if also_stdout:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

    return _worker_logger, log_path
