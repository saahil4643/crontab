"""Unit tests for Alpha Spread retry queue and circuit breaker (no Playwright)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from models import ScrapeJob
from resilient_collector.alphaspread_guard import AlphaSpreadGuard, apply_jitter


def _job(ticker: str = "TSM", row: int = 4) -> ScrapeJob:
    return ScrapeJob(
        row=row,
        ticker=ticker,
        source="alphaspread",
        value_col="D",
        value_col_index=4,
        label="Alpha Spread",
    )


class ApplyJitterTests(unittest.TestCase):
    def test_jitter_stays_within_30_percent(self) -> None:
        for _ in range(200):
            value = apply_jitter(100.0, 0.25)
            self.assertGreaterEqual(value, 70.0)
            self.assertLessEqual(value, 130.0)


class RetryQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jitter = patch(
            "resilient_collector.alphaspread_guard.apply_jitter",
            side_effect=lambda seconds, fraction=0.25: seconds,
        )
        self.jitter.start()
        self.guard = AlphaSpreadGuard(
            retry_base_seconds=480,
            retry_cap_seconds=4500,
            retry_max_attempts=5,
            breaker_threshold=99,
            breaker_window_seconds=600,
            breaker_cooldown_seconds=2700,
        )

    def tearDown(self) -> None:
        self.jitter.stop()

    def test_403_not_due_immediately_then_due_after_backoff(self) -> None:
        now = 1_000_000.0
        queued = self.guard.enqueue_retry(_job(), "403", now=now)
        self.assertTrue(queued)
        self.assertEqual(self.guard.due_jobs(now=now), [])
        self.assertEqual(self.guard.pending_retry_count(), 1)
        due = self.guard.due_jobs(now=now + 480)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].ticker, "TSM")
        self.assertEqual(self.guard.pending_retry_count(), 0)

    def test_fifth_failure_exhausted(self) -> None:
        now = 1_000_000.0
        job = _job()
        for attempt in range(5):
            self.assertTrue(
                self.guard.enqueue_retry(job, "403", now=now + attempt * 10_000)
            )
        self.assertFalse(self.guard.enqueue_retry(job, "403", now=now + 60_000))
        self.assertEqual(self.guard.pending_retry_count(), 0)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jitter = patch(
            "resilient_collector.alphaspread_guard.apply_jitter",
            side_effect=lambda seconds, fraction=0.25: seconds,
        )
        self.jitter.start()
        self.guard = AlphaSpreadGuard(
            retry_base_seconds=60,
            retry_cap_seconds=60,
            retry_max_attempts=5,
            breaker_threshold=3,
            breaker_window_seconds=600,
            breaker_cooldown_seconds=2700,
        )

    def tearDown(self) -> None:
        self.jitter.stop()

    def test_three_403s_in_window_trip_then_reset(self) -> None:
        now = 2_000_000.0
        self.assertFalse(self.guard.is_open(now=now))
        for offset in (0, 30, 60):
            self.guard.enqueue_retry(_job(row=offset + 1), "403", now=now + offset)
        self.assertTrue(self.guard.is_open(now=now + 60))
        self.assertIn("as_breaker=open", self.guard.status_line(now=now + 60))
        self.assertEqual(self.guard.due_jobs(now=now + 60), [])
        self.assertFalse(self.guard.is_open(now=now + 60 + 2700))
        self.assertIn("as_breaker=closed", self.guard.status_line(now=now + 60 + 2700))


if __name__ == "__main__":
    unittest.main()
