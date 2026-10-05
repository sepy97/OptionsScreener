"""The blocklist: tickers the screener leaves out, shared by everyone who uses it.

One list for the whole site, kept in the jobs database (so the nightly backup carries it) and
edited from the screener's Advanced filters by anyone — the screener is public, so there is no
"whose list" to ask about. Every screen reads the list as it stands when the screen starts, so the
latest edit is always the one applied; a screen already running keeps the list it started with.

The editing is open to anyone on the internet, so the store keeps the damage one visitor can do
small: tickers must look like tickers, the list has a size cap, and the edit endpoints share the
per-IP rate limit with the other write paths.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# Upper-case letters and digits, with the one-character class separators real tickers use
# (BRK.B, BF-B). Anything else is a typo or junk, never a symbol the screen could have produced.
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z0-9]{1,2})?$")
MAX_ENTRIES = 500


class BlocklistError(ValueError):
    """An edit that was refused; the message is safe to show as is."""


def parse_tickers(raw: str) -> list[str]:
    """Tickers from what someone typed: separated by spaces, commas or semicolons, any case.

    Raises BlocklistError naming the entries that are not tickers, so a typo is shown rather than
    silently dropped.
    """
    words = [w for w in re.split(r"[\s,;]+", (raw or "").upper()) if w]
    bad = [w for w in words if not _TICKER.match(w)]
    if bad:
        shown = ", ".join(w[:12] for w in bad[:5])
        raise BlocklistError(f"not a ticker: {shown}")
    return list(dict.fromkeys(words))  # in the order typed, each once


class BlocklistStore:
    """The list itself. One connection per call, so request threads and the screen's worker
    thread can all use it at once."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS blocklist ("
                    "symbol TEXT PRIMARY KEY, added_at TEXT NOT NULL)"
                )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0)

    def symbols(self) -> list[str]:
        """Every ticker on the list, alphabetically."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT symbol FROM blocklist ORDER BY symbol").fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def add(self, raw: str) -> list[str]:
        """Add what was typed; returns the tickers that were new. Already listed is not an error."""
        tickers = parse_tickers(raw)
        if not tickers:
            return []
        now = datetime.now(tz=UTC).isoformat()
        conn = self._connect()
        try:
            with conn:
                have = {r[0] for r in conn.execute("SELECT symbol FROM blocklist")}
                new = [t for t in tickers if t not in have]
                if len(have) + len(new) > MAX_ENTRIES:
                    raise BlocklistError(
                        f"the blocklist holds at most {MAX_ENTRIES} tickers — remove some first")
                conn.executemany(
                    "INSERT INTO blocklist (symbol, added_at) VALUES (?, ?)",
                    [(t, now) for t in new],
                )
        finally:
            conn.close()
        return new

    def remove(self, symbol: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM blocklist WHERE symbol = ?", ((symbol or "").upper(),))
        finally:
            conn.close()
