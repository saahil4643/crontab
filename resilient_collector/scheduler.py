"""Round-robin chunk scheduling helpers for the sheet collector."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from config import SourceKey
from models import ScrapeJob

YAHOO_SOURCES: frozenset[SourceKey] = frozenset(
    {"price_target_low", "price_target_avg", "price_target_high"}
)

ROUND_ROBIN_ORDER: tuple[str, ...] = (
    "alphaspread",
    "valueinvesting",
    "gurufocus",
    "yahoo",
)


@dataclass
class YahooTickerGroup:
    """Yahoo low/avg/high jobs for one ticker (one page load)."""

    ticker: str
    jobs: list[ScrapeJob]


def is_yahoo_source(source: SourceKey) -> bool:
    return source in YAHOO_SOURCES


def partition_scrape_queues(
    scrape_jobs: list[ScrapeJob],
) -> dict[str, deque]:
    """Split scrape jobs into named queues for round-robin."""
    queues: dict[str, deque] = {
        "alphaspread": deque(),
        "valueinvesting": deque(),
        "gurufocus": deque(),
        "yahoo": deque(),
    }
    yahoo_by_ticker: dict[str, list[ScrapeJob]] = defaultdict(list)

    for job in scrape_jobs:
        if job.source == "alphaspread":
            queues["alphaspread"].append(job)
        elif job.source == "valueinvesting":
            queues["valueinvesting"].append(job)
        elif job.source == "gurufocus":
            queues["gurufocus"].append(job)
        elif job.source in YAHOO_SOURCES:
            yahoo_by_ticker[job.ticker.strip().upper()].append(job)
        else:
            # Unknown sources fall through as single-job alphaspread-style work
            queues["alphaspread"].append(job)

    # Preserve first-seen ticker order from the sheet
    seen: set[str] = set()
    ordered_tickers: list[str] = []
    for job in scrape_jobs:
        if job.source not in YAHOO_SOURCES:
            continue
        key = job.ticker.strip().upper()
        if key in seen:
            continue
        seen.add(key)
        ordered_tickers.append(key)

    for ticker in ordered_tickers:
        jobs = yahoo_by_ticker.get(ticker) or []
        if jobs:
            queues["yahoo"].append(YahooTickerGroup(ticker=ticker, jobs=jobs))

    return queues


def take_chunk(queue: deque, chunk_size: int) -> list:
    """Pop up to chunk_size items from the left of a queue."""
    size = max(1, chunk_size)
    items: list = []
    while queue and len(items) < size:
        items.append(queue.popleft())
    return items


def queue_remaining_summary(queues: dict[str, deque]) -> str:
    parts = [f"{name}={len(q)}" for name, q in queues.items()]
    return " ".join(parts)


def any_queue_nonempty(queues: dict[str, deque]) -> bool:
    return any(len(q) > 0 for q in queues.values())
