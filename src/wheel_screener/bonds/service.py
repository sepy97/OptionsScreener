"""The Bond calendar tab's engine: resolve a watchlist once, build its MOEX calendar, cache it.

It knows nothing about whose bonds these are — a watchlist is its text, in the script's format
(see ``api.bond_portfolios`` for whose is whose, and ``bonds.holdings`` for how a person's
portfolios become the text built here). An imported list is resolved once into the form the
script writes (``code,name,matdate,qty``): importing looks ISINs up by name, so a build never
repeats the lookup.

A build is ~one MOEX request per bond, so builds are cached by the watchlist's content and start
date for ``cache_ttl_seconds``. Keyed by content, two people with the same list share a build —
and nobody can be shown a calendar of any list but their own. One build runs at a time; requests
that arrive meanwhile wait for it and reuse it, which also keeps us polite to MOEX.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from wheel_screener.bonds import bond_calendar as bc

logger = logging.getLogger(__name__)

_MAX_CACHED = 32  # builds kept: a few people × (the default year, the whole calendar, custom)
# a build with problems is usually MOEX dropping requests, not a bad watchlist — so it is
# retried soon, rather than kept for hours as if those bonds had no events
_PROBLEM_TTL_SECONDS = 300


@dataclass
class SaveReport:
    """What resolving a watchlist did — shown so the name matches can be checked, as the script
    asks ("check that everything matched correctly")."""

    count: int
    reused: int = 0  # codes taken from the bonds held before (same name + maturity)
    resolved: list[str] = field(default_factory=list)  # "name → ISIN (MOEX name)" lines
    problems: list[str] = field(default_factory=list)
    sold_out: int = 0  # a report's bonds with a quantity of 0, left out
    source: str = ""  # where in the file the bonds came from (a spreadsheet's sheet)


class BondCalendarService:
    def __init__(
        self,
        *,
        cache_ttl_seconds: int = 3 * 3600,
        workers: int = 16,
        now: Callable[[], float] = time.monotonic,
        build: Callable[..., bc.CalendarRun] = bc.build_calendar,
        resolve: Callable[..., list[str]] = bc.resolve_names,
    ) -> None:
        self._ttl = cache_ttl_seconds
        self._workers = workers
        self._now = now
        self._build = build
        self._resolve = resolve
        self._cache: OrderedDict[tuple, tuple[float, bc.CalendarRun]] = OrderedDict()
        self._lock = threading.Lock()  # one build at a time

    def resolve(self, items: list[dict], previous: list[dict] = ()) -> SaveReport:
        """Fill in the ISINs of ``items`` given only by name (in place) — except those
        ``previous`` (the bonds as held before) already has a code for."""
        reused = bc.reuse_resolved(items, list(previous))
        resolved: list[str] = []
        problems: list[str] = []
        if any(not it["code"] for it in items):
            # the search among matured bonds is one request per bond — run it in parallel
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                problems = self._resolve(items, resolved.append, pmap=pool.map)
        logger.info("bond list resolved: %d bonds, %d looked up by name, %d problems",
                    len(items), len(resolved), len(problems))
        return SaveReport(len(items), reused, resolved, problems)

    def _ttl_of(self, run: bc.CalendarRun) -> float:
        return min(self._ttl, _PROBLEM_TTL_SECONDS) if run.problems else self._ttl

    def calendar(
        self, text: str, date_from: date | None, date_to: date | None, *, refresh: bool = False
    ) -> tuple[bc.CalendarRun, float]:
        """The events of watchlist ``text`` between date_from and date_to (None = no limit), and
        the build's age in seconds.

        The build covers [date_from, ∞) and is windowed to date_to here, so changing only the end
        of the period never costs another MOEX round."""
        key = (hashlib.sha256(text.encode("utf-8")).hexdigest(), date_from)
        with self._lock:
            hit = self._cache.get(key)
            if refresh or hit is None or self._now() - hit[0] > self._ttl_of(hit[1]):
                items = bc.parse_watchlist(text)
                run = self._build(items, date_from, None, workers=self._workers, log=logger.debug)
                hit = (self._now(), run)
                self._cache[key] = hit
                while len(self._cache) > _MAX_CACHED:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(key)
        built_at, run = hit
        events = run.events if date_to is None else [e for e in run.events if e["date"] <= date_to]
        return bc.CalendarRun(events, run.problems, run.notes), self._now() - built_at
