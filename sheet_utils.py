"""Column letter helpers for gspread."""

from __future__ import annotations

from config import DEFAULT_SECURITY_TYPE

# Sheet "Nasdaq" / "NYSE" → Alpha Spread path segment
_EXCHANGE_ALIASES: dict[str, str] = {
    "nasdaq": "nasdaq",
    "nyse": "nyse",
    "amex": "amex",
    "nysemkt": "nyse",
    "nyse american": "amex",
}


def normalize_security_type(index_cell: str | None) -> str:
    """Map Index column (e.g. Nasdaq) to Alpha Spread security_type."""
    if not index_cell or not str(index_cell).strip():
        return DEFAULT_SECURITY_TYPE
    key = str(index_cell).strip().lower()
    return _EXCHANGE_ALIASES.get(key, key.replace(" ", ""))


def col_letter_to_index(letter: str) -> int:
    """A -> 1, B -> 2, AA -> 27."""
    letter = letter.strip().upper()
    n = 0
    for ch in letter:
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"Invalid column letter: {letter!r}")
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def col_index_to_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    if index < 1:
        raise ValueError("Column index must be >= 1")
    s = []
    while index:
        index, rem = divmod(index - 1, 26)
        s.append(chr(rem + ord("A")))
    return "".join(reversed(s))
