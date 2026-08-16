"""Buffered Google Sheets writes with retries, rate-limit backoff, and a pending queue."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from config import _env_float, _env_int
from models import ScrapeJob
from sheet_client import DcfSheetClient

logger = logging.getLogger(__name__)

SHEET_WRITE_BATCH_SIZE = _env_int("SHEET_WRITE_BATCH_SIZE", 15)
SHEET_RETRY_ATTEMPTS = _env_int("SHEET_RETRY_ATTEMPTS", 6)
SHEET_RETRY_BASE_SECONDS = _env_float("SHEET_RETRY_BASE_SECONDS", 2)
SHEET_RATE_LIMIT_BACKOFF_SECONDS = _env_float("SHEET_RATE_LIMIT_BACKOFF_SECONDS", 90)


@dataclass(frozen=True)
class CellUpdate:
    job: ScrapeJob
    value: str
    action: str  # "write" | "clear"


def _is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in ("429", "quota", "rate limit", "ratelimit", "too many requests")
    )


def _retry_delay(exc: BaseException, attempt: int) -> float:
    if _is_rate_limit_error(exc):
        return SHEET_RATE_LIMIT_BACKOFF_SECONDS
    return SHEET_RETRY_BASE_SECONDS * attempt


class SheetWriteBuffer:
    """Queue cell updates; flush in batches without aborting the worker on API errors."""

    def __init__(
        self,
        client: DcfSheetClient,
        *,
        pending_path: Path,
        batch_size: int | None = None,
    ) -> None:
        self._client = client
        self._pending_path = pending_path
        # Re-read env at construct time so .env loaded after import still applies.
        configured = _env_int("SHEET_WRITE_BATCH_SIZE", SHEET_WRITE_BATCH_SIZE)
        self._batch_size = max(1, batch_size if batch_size is not None else configured)
        self._queue: list[CellUpdate] = []
        self._last_flush_at = time.monotonic()
        self._queued_total = 0
        self._flushed_total = 0
        self._failed_total = 0

    @property
    def client(self) -> DcfSheetClient:
        return self._client

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    async def replay_pending_file(self) -> int:
        """Apply updates saved from a prior failed flush. Returns count applied."""
        if not self._pending_path.is_file():
            return 0

        lines = await asyncio.to_thread(
            self._pending_path.read_text,
            encoding="utf-8",
        )
        records: list[dict] = []
        for line in lines.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("[sheet] Skipping invalid pending write line: %s", exc)

        if not records:
            await asyncio.to_thread(self._pending_path.unlink, missing_ok=True)
            return 0

        logger.info("[sheet] Replaying %d pending write(s) from %s", len(records), self._pending_path)
        updates = [_record_to_update(record) for record in records]
        applied = await self._flush_updates(updates, label="replay_pending")
        if applied == len(updates):
            await asyncio.to_thread(self._pending_path.unlink, missing_ok=True)
            logger.info("[sheet] Pending write replay complete (%d cell(s))", applied)
        else:
            remaining = updates[applied:]
            await asyncio.to_thread(self._rewrite_pending_file, remaining)
            logger.warning(
                "[sheet] Pending replay partial: applied=%d remaining=%d",
                applied,
                len(remaining),
            )
        return applied

    async def enqueue_write(self, job: ScrapeJob, value: str) -> None:
        self._queue.append(CellUpdate(job=job, value=value, action="write"))
        self._queued_total += 1
        logger.info(
            "[sheet] Queued write row=%d col=%s ticker=%s value=%r (buffer=%d)",
            job.row,
            job.value_col,
            job.ticker,
            value,
            len(self._queue),
        )
        await self.maybe_flush()

    async def enqueue_clear(self, job: ScrapeJob) -> None:
        self._queue.append(CellUpdate(job=job, value="", action="clear"))
        self._queued_total += 1
        logger.info(
            "[sheet] Queued clear row=%d col=%s ticker=%s (buffer=%d)",
            job.row,
            job.value_col,
            job.ticker,
            len(self._queue),
        )
        await self.maybe_flush()

    async def maybe_flush(self) -> None:
        if len(self._queue) >= self._batch_size:
            logger.info(
                "[sheet] Buffer full (%d/%d); flushing batch to Google Sheets",
                len(self._queue),
                self._batch_size,
            )
            await self.flush()

    async def flush(self) -> None:
        if not self._queue:
            return
        batch = self._queue
        self._queue = []
        logger.info(
            "[sheet] Sending %d cell update(s) to Google Sheets in one batch_update",
            len(batch),
        )
        applied = await self._flush_updates(batch, label="batch_flush")
        if applied < len(batch):
            failed = batch[applied:]
            await self._append_pending_file(failed)
            self._failed_total += len(failed)
            logger.error(
                "[sheet] %d cell update(s) moved to pending file after flush failure",
                len(failed),
            )
        self._flushed_total += applied
        self._last_flush_at = time.monotonic()
        if applied:
            logger.info(
                "[sheet] Google Sheets batch flush complete | applied=%d remaining_buffer=%d",
                applied,
                len(self._queue),
            )

    async def close(self) -> None:
        await self.flush()
        logger.info(
            "[sheet] Write buffer closed | queued=%d flushed=%d failed=%d pending_buffer=%d",
            self._queued_total,
            self._flushed_total,
            self._failed_total,
            len(self._queue),
        )

    async def _flush_updates(self, updates: list[CellUpdate], *, label: str) -> int:
        if not updates:
            return 0

        offset = 0
        while offset < len(updates):
            chunk = updates[offset : offset + self._batch_size]
            last_error: BaseException | None = None
            for attempt in range(1, SHEET_RETRY_ATTEMPTS + 1):
                try:
                    await asyncio.to_thread(self._client.apply_cell_updates, chunk)
                    for update in chunk:
                        self._log_applied(update)
                    offset += len(chunk)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= SHEET_RETRY_ATTEMPTS:
                        break
                    delay = _retry_delay(exc, attempt)
                    logger.warning(
                        "[sheet] %s failed attempt %d/%d (%d cell(s)): %s; retrying in %.1fs",
                        label,
                        attempt,
                        SHEET_RETRY_ATTEMPTS,
                        len(chunk),
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

            if last_error is not None:
                logger.error(
                    "[sheet] %s abandoned after %d attempt(s) at offset %d/%d: %s",
                    label,
                    SHEET_RETRY_ATTEMPTS,
                    offset,
                    len(updates),
                    last_error,
                )
                return offset

        return len(updates)

    def _log_applied(self, update: CellUpdate) -> None:
        job = update.job
        if update.action == "clear":
            logger.info(
                "Cleared row %d col %s (%s) %s old=%r",
                job.row,
                job.value_col,
                job.label,
                job.ticker,
                job.current_value,
            )
        else:
            logger.info(
                "Updated row %d col %s (%s) %s -> %r",
                job.row,
                job.value_col,
                job.label,
                job.ticker,
                update.value,
            )

    async def _append_pending_file(self, updates: list[CellUpdate]) -> None:
        if not updates:
            return
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)

        def _append() -> None:
            with self._pending_path.open("a", encoding="utf-8") as handle:
                for update in updates:
                    handle.write(json.dumps(_update_to_record(update), sort_keys=True))
                    handle.write("\n")

        await asyncio.to_thread(_append)
        logger.warning(
            "[sheet] Appended %d failed update(s) to %s",
            len(updates),
            self._pending_path,
        )

    def _rewrite_pending_file(self, updates: list[CellUpdate]) -> None:
        if not updates:
            self._pending_path.unlink(missing_ok=True)
            return
        with self._pending_path.open("w", encoding="utf-8") as handle:
            for update in updates:
                handle.write(json.dumps(_update_to_record(update), sort_keys=True))
                handle.write("\n")


def _update_to_record(update: CellUpdate) -> dict:
    job = update.job
    return {
        "action": update.action,
        "row": job.row,
        "value_col": job.value_col,
        "value_col_index": job.value_col_index,
        "ticker": job.ticker,
        "label": job.label,
        "source": job.source,
        "current_value": job.current_value,
        "value": update.value,
    }


def _record_to_update(record: dict) -> CellUpdate:
    job = ScrapeJob(
        row=int(record["row"]),
        ticker=str(record.get("ticker", "")),
        source=record.get("source", "valueinvesting"),  # type: ignore[arg-type]
        value_col=str(record["value_col"]),
        value_col_index=int(record["value_col_index"]),
        label=str(record.get("label", "")),
        current_value=str(record.get("current_value", "")),
    )
    return CellUpdate(
        job=job,
        value=str(record.get("value", "")),
        action=str(record.get("action", "write")),
    )
