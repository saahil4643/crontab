"""ValueInvesting-specific scrape errors."""

from __future__ import annotations


class ValueInvestingLimitError(RuntimeError):
    """Free view limit or hard signup wall detected."""
