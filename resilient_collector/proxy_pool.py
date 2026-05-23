from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyConfig:
    raw: str | None = None

    @property
    def label(self) -> str:
        return self.raw or "direct"

    @property
    def playwright_config(self) -> dict[str, str] | None:
        return {"server": self.raw} if self.raw else None


async def build_validated_pool() -> list[ProxyConfig]:
    mode = os.environ.get("RESILIENT_PROXY_MODE", "forward").strip().lower()
    if mode in {"direct", "none", "off", "false", "0"}:
        logger.info("Browser proxy mode: direct")
        return [ProxyConfig()]

    proxy = (
        os.environ.get("RESILIENT_FORWARD_PROXY")
        or os.environ.get("PLAYWRIGHT_PROXY")
        or ""
    ).strip()
    if not proxy or proxy.lower() == "none":
        logger.info("Browser proxy mode: direct")
        return [ProxyConfig()]
    logger.info("Browser proxy mode: forward proxy %s", proxy)
    return [ProxyConfig(raw=proxy)]
