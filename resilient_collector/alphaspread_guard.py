"""Same-run Alpha Spread retry queue and single-IP circuit breaker."""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from models import ScrapeJob

logger = logging.getLogger(__name__)

_JITTER_FRACTION = 0.25


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def apply_jitter(seconds: float, fraction: float = _JITTER_FRACTION) -> float:
    """Return seconds jittered by ±fraction (clamped to >= 1 when input >= 1)."""
    if seconds <= 0:
        return 0.0
    low = seconds * (1.0 - fraction)
    high = seconds * (1.0 + fraction)
    value = random.uniform(low, high)
    if seconds >= 1:
        return max(1.0, value)
    return max(0.0, value)


def _job_key(job: ScrapeJob) -> tuple[int, str, str]:
    return (job.row, job.ticker.strip().upper(), job.source)


@dataclass
class RetryItem:
    job: ScrapeJob
    attempt: int
    next_retry_at: float
    reason: str


@dataclass
class AlphaSpreadGuard:
    retry_base_seconds: int = field(
        default_factory=lambda: _env_int("RESILIENT_ALPHASPREAD_RETRY_BASE_SECONDS", 480)
    )
    retry_cap_seconds: int = field(
        default_factory=lambda: _env_int("RESILIENT_ALPHASPREAD_RETRY_CAP_SECONDS", 4500)
    )
    retry_max_attempts: int = field(
        default_factory=lambda: _env_int("RESILIENT_ALPHASPREAD_RETRY_MAX_ATTEMPTS", 5)
    )
    breaker_threshold: int = field(
        default_factory=lambda: _env_int("RESILIENT_ALPHASPREAD_BREAKER_THRESHOLD", 3)
    )
    breaker_window_seconds: int = field(
        default_factory=lambda: _env_int("RESILIENT_ALPHASPREAD_BREAKER_WINDOW_SECONDS", 600)
    )
    breaker_cooldown_seconds: int = field(
        default_factory=lambda: _env_int("RESILIENT_ALPHASPREAD_BREAKER_COOLDOWN_SECONDS", 2700)
    )
    _retry: list[RetryItem] = field(default_factory=list)
    _attempts: dict[tuple[int, str, str], int] = field(default_factory=dict)
    _hits: list[float] = field(default_factory=list)
    _breaker_reset_at: float = 0.0
    _breaker_was_open: bool = False

    def pending_retry_count(self) -> int:
        return len(self._retry)

    def is_open(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self._breaker_reset_at <= 0:
            if self._breaker_was_open:
                logger.warning("CIRCUIT BREAKER RESET: alpha_spread — resuming")
                self._breaker_was_open = False
            return False
        if now >= self._breaker_reset_at:
            logger.warning("CIRCUIT BREAKER RESET: alpha_spread — resuming")
            self._breaker_reset_at = 0.0
            self._breaker_was_open = False
            return False
        return True

    def remaining_breaker_seconds(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if not self.is_open(now):
            return 0.0
        return max(0.0, self._breaker_reset_at - now)

    def seconds_until_ready(self, now: float | None = None) -> float:
        """Soonest wait until a retry can run (breaker must be closed)."""
        now = time.time() if now is None else now
        if self.is_open(now):
            return max(0.0, self._breaker_reset_at - now)
        if self._retry:
            return max(0.0, min(item.next_retry_at for item in self._retry) - now)
        return 0.0

    def status_line(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        open_now = self.is_open(now)
        state = "open" if open_now else "closed"
        remaining = int(self.remaining_breaker_seconds(now)) if open_now else 0
        return f"as_retry={self.pending_retry_count()} as_breaker={state} remaining_s={remaining}"

    def clear_attempts(self, job: ScrapeJob) -> None:
        self._attempts.pop(_job_key(job), None)
        self._retry = [item for item in self._retry if _job_key(item.job) != _job_key(job)]

    def record_403(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._hits.append(now)
        cutoff = now - self.breaker_window_seconds
        self._hits = [stamp for stamp in self._hits if stamp > cutoff]
        if self.is_open(now):
            return
        if len(self._hits) >= self.breaker_threshold:
            cooldown = apply_jitter(self.breaker_cooldown_seconds)
            self._breaker_reset_at = now + cooldown
            self._breaker_was_open = True
            minutes = max(1, int(round(cooldown / 60.0)))
            logger.warning(
                "CIRCUIT BREAKER TRIPPED: alpha_spread — pausing %dmin",
                minutes,
            )

    def enqueue_retry(
        self,
        job: ScrapeJob,
        reason: str,
        now: float | None = None,
    ) -> bool:
        """Queue *job* for a later same-run retry. False if attempts are exhausted."""
        now = time.time() if now is None else now
        key = _job_key(job)
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt

        if reason in {"403", "429"}:
            self.record_403(now)

        if attempt > self.retry_max_attempts:
            logger.warning("retry exhausted: %s — leaving row empty", job.ticker)
            self._retry = [item for item in self._retry if _job_key(item.job) != key]
            return False

        delay = min(
            float(self.retry_base_seconds) * (2 ** (attempt - 1)),
            float(self.retry_cap_seconds),
        )
        delay = apply_jitter(delay)
        next_at = now + delay
        if self.is_open(now):
            next_at = max(next_at, self._breaker_reset_at)

        self._retry = [item for item in self._retry if _job_key(item.job) != key]
        self._retry.append(
            RetryItem(job=job, attempt=attempt, next_retry_at=next_at, reason=reason)
        )
        next_iso = datetime.fromtimestamp(next_at, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        logger.info(
            "retry queued: %s attempt=%d next_retry_at=%s reason=%s",
            job.ticker,
            attempt,
            next_iso,
            reason,
        )
        return True

    def due_jobs(self, now: float | None = None) -> list[ScrapeJob]:
        now = time.time() if now is None else now
        if self.is_open(now):
            return []
        due: list[ScrapeJob] = []
        remaining: list[RetryItem] = []
        for item in self._retry:
            if item.next_retry_at <= now:
                due.append(item.job)
                logger.info(
                    "retry due: %s attempt=%d reason=%s",
                    item.job.ticker,
                    item.attempt,
                    item.reason,
                )
            else:
                remaining.append(item)
        self._retry = remaining
        return due
