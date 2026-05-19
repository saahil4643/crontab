"""
Collect JSON (and optional HTML) from Playwright network responses during navigation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from playwright.sync_api import Page, Response

logger = logging.getLogger(__name__)


@dataclass
class CapturedPayload:
    """One JSON body captured from a network response."""

    url: str
    body: Any


@dataclass
class PageCapture:
    """Everything collected for a single ticker visit."""

    url: str
    json_payloads: list[CapturedPayload] = field(default_factory=list)
    html: str = ""

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "json_count": len(self.json_payloads),
            "json_urls": [p.url for p in self.json_payloads],
            "html_length": len(self.html),
        }


class NetworkCapture:
    """
    Attach to a Playwright page and record JSON responses while a page loads.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._payloads: list[CapturedPayload] = []
        self._attached = False

    def _on_response(self, response: Response) -> None:
        try:
            if response.status < 200 or response.status >= 400:
                return
            url = response.url
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type and not url.endswith(".json"):
                return
            body = response.json()
            self._payloads.append(CapturedPayload(url=url, body=body))
            logger.debug("Captured JSON %s (%d bytes)", url[:120], len(str(body)))
        except Exception as exc:
            logger.debug("Skip response %s: %s", response.url[:80], exc)

    def attach(self) -> None:
        if not self._attached:
            self._page.on("response", self._on_response)
            self._attached = True

    def detach(self) -> None:
        if self._attached:
            self._page.remove_listener("response", self._on_response)
            self._attached = False

    def collect_during(
        self,
        navigate: Callable[[], None],
        *,
        post_load_wait_ms: int = 2500,
        save_html: bool = True,
    ) -> PageCapture:
        """
        Run navigate() while listening for JSON responses, then optionally snapshot HTML.
        """
        self._payloads = []
        self.attach()
        try:
            navigate()
            if post_load_wait_ms > 0:
                self._page.wait_for_timeout(post_load_wait_ms)
        finally:
            self.detach()

        html = ""
        if save_html:
            try:
                html = self._page.content()
            except Exception as exc:
                logger.debug("Could not read page HTML: %s", exc)

        return PageCapture(
            url=self._page.url,
            json_payloads=list(self._payloads),
            html=html,
        )


def save_debug_capture(
    capture: PageCapture,
    directory: Path,
    *,
    source: str,
    ticker: str,
) -> None:
    """Write JSON payloads and HTML snippet for failed extractions."""
    directory.mkdir(parents=True, exist_ok=True)
    safe_ticker = "".join(c if c.isalnum() else "_" for c in ticker)
    base = directory / f"{source}_{safe_ticker}"

    meta = {"url": capture.url, **capture.to_debug_dict()}
    (base.with_suffix(".meta.json")).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    for i, payload in enumerate(capture.json_payloads):
        path = base.parent / f"{base.name}_json_{i}.json"
        path.write_text(json.dumps(payload.body, indent=2, default=str), encoding="utf-8")

    if capture.html:
        (base.with_suffix(".html")).write_text(capture.html[:500_000], encoding="utf-8")

    logger.info("Saved debug capture under %s*", base)
