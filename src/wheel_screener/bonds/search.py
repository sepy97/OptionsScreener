"""Finding a bond to add to a watchlist: by ISIN, MOEX code or name.

Searching runs over MOEX's list of the bonds it trades (~3,000 of them, with maturity dates and
coupons) — one request, kept for hours, so typing a query costs nothing. Every word typed must
appear in the bond's name or codes, compared the way ``bond_calendar`` compares names: Cyrillic
letters that look Latin are one letter (MOEX writes the Р of 001Р-06 either way), and leading
zeros don't count, so ``Система 1P-6`` finds ``АФК "Система" ПАО БО-001P-06``. Only when that finds
nothing — a matured or untraded bond — is MOEX's own security search asked.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from wheel_screener.bonds import bond_calendar as bc

logger = logging.getLogger(__name__)

_LIST_TTL_SECONDS = 6 * 3600  # new issues list a few a day; a search can wait for them
_LIST_RETRY_SECONDS = 60  # after MOEX failed to send the list, don't ask again on every keystroke
_MIN_MOEX_QUERY = 3  # MOEX's search wants at least this many characters
_MOEX_CACHED = 64  # fallback searches remembered, so retyping a query doesn't ask again


class MoexUnavailable(Exception):
    """MOEX did not answer — worth trying again in a moment."""


@dataclass(frozen=True)
class Bond:
    secid: str
    isin: str
    name: str  # MOEX's full name: АФК "Система" ПАО БО-001P-06
    shortname: str = ""
    latname: str = ""
    matdate: date | None = None
    coupon: float | None = None  # % a year, the current coupon
    currency: str = ""
    traded: bool = True

    @property
    def code(self) -> str:
        """What a watchlist keeps — the ISIN, as the name lookup saves it."""
        return self.isin or self.secid

    def known_as(self, codes: set[str]) -> bool:
        return self.secid in codes or bool(self.isin and self.isin in codes)


def search_key(text: str) -> str:
    """Letters and digits only, Latin-looking Cyrillic as Latin, leading zeros dropped."""
    s = re.sub(r"[^0-9A-ZА-Я]", "", text.upper().replace("Ё", "Е").translate(bc.LATIN))
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)


def _words(query: str) -> list[str]:
    return [w for w in re.split(r"[\s,;\"'«»()]+", query) if search_key(w)]


def _key_of(b: Bond) -> str:
    return search_key(" ".join((b.secid, b.isin, b.name, b.shortname, b.latname)))


def load_traded() -> list[Bond]:
    """Every bond MOEX trades, one per code (the list repeats a bond on each board)."""
    rows = bc.table(bc.get_json("/engines/stock/markets/bonds/securities.json", {
        "iss.only": "securities",
        "securities.columns": "SECID,ISIN,SHORTNAME,SECNAME,LATNAME,MATDATE,FACEUNIT,"
                              "COUPONPERCENT"}), "securities")
    bonds: dict[str, Bond] = {}
    for r in rows:
        secid = str(r.get("secid") or "").upper()
        if secid and secid not in bonds:
            bonds[secid] = Bond(
                secid=secid, isin=str(r.get("isin") or "").upper(),
                name=r.get("secname") or r.get("shortname") or secid,
                shortname=r.get("shortname") or "", latname=r.get("latname") or "",
                matdate=bc.parse_date(r.get("matdate")),
                coupon=bc.to_number(r.get("couponpercent")), currency=bc.currency_of(r))
    return list(bonds.values())


def search_moex(query: str) -> list[Bond]:
    """MOEX's own security search, bonds only — it knows matured and untraded bonds too, but not
    their maturity dates, and it matches ``query`` as one piece."""
    rows = bc.table(bc.get_json("/securities.json", {
        "q": query, "iss.only": "securities", "limit": 100,
        "group_by": "group", "group_by_filter": "stock_bonds"}), "securities")
    return [Bond(secid=str(r.get("secid") or "").upper(), isin=str(r.get("isin") or "").upper(),
                 name=r.get("name") or r.get("shortname") or r.get("secid") or "",
                 shortname=r.get("shortname") or "", traded=bool(r.get("is_traded")))
            for r in rows if r.get("secid")]


def describe_bond(code: str) -> Bond | None:
    """A bond MOEX doesn't trade, by code: its search row and its card (for the maturity date)."""
    sec = bc.find_security(code)
    if not sec:
        return None
    card = bc.describe(sec["secid"])
    return Bond(secid=str(sec["secid"]).upper(),
                isin=str(card.get("ISIN") or sec.get("isin") or "").upper(),
                name=card.get("NAME") or sec.get("name") or sec["secid"],
                shortname=card.get("SHORTNAME") or sec.get("shortname") or "",
                matdate=bc.parse_date(card.get("MATDATE")), traded=bool(sec.get("is_traded")))


class BondDirectory:
    """MOEX's bond list, cached, with the search and lookup the watchlist's Add uses."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _LIST_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
        load: Callable[[], list[Bond]] = load_traded,
        moex_search: Callable[[str], list[Bond]] = search_moex,
        describe: Callable[[str], Bond | None] = describe_bond,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._load = load
        self._moex_search = moex_search
        self._describe = describe
        self._listed: list[tuple[str, Bond]] | None = None
        self._loaded_at = 0.0
        self._failed_at: float | None = None
        self._lock = threading.Lock()  # one load at a time; searches meanwhile wait for it
        self._moex_cache: OrderedDict[str, list[Bond]] = OrderedDict()

    def _traded(self) -> list[tuple[str, Bond]]:
        with self._lock:
            now = self._now()
            if self._listed is not None and now - self._loaded_at < self._ttl:
                return self._listed
            if self._failed_at is None or now - self._failed_at >= _LIST_RETRY_SECONDS:
                try:
                    bonds = self._load()
                except Exception as exc:
                    logger.warning("bond search: MOEX did not send its bond list (%s)", exc)
                    self._failed_at = now
                else:
                    self._listed = [(_key_of(b), b) for b in bonds]
                    self._loaded_at, self._failed_at = now, None
                    return self._listed
            if self._listed is not None:
                return self._listed  # a stale list beats none
            raise MoexUnavailable("MOEX did not send its bond list")

    def _search_moex(self, query: str) -> list[Bond]:
        q = query.strip().lower()
        with self._lock:
            if q in self._moex_cache:
                self._moex_cache.move_to_end(q)
                return self._moex_cache[q]
        try:
            found = self._moex_search(query)
        except Exception as exc:
            raise MoexUnavailable(f"MOEX search failed ({exc})") from exc
        with self._lock:
            self._moex_cache[q] = found
            while len(self._moex_cache) > _MOEX_CACHED:
                self._moex_cache.popitem(last=False)
        return found

    def search(self, query: str) -> list[Bond]:
        """Bonds matching every word of ``query``: an exact code first, then by maturity.

        Raises MoexUnavailable when MOEX could not be asked at all."""
        words = _words(query)
        if not words:
            return []
        keys = [search_key(w) for w in words]
        try:
            listed = self._traded()
            unavailable = None
        except MoexUnavailable as exc:
            listed, unavailable = [], exc
        found = [b for k, b in listed if all(w in k for w in keys)]
        if not found:
            # MOEX matches its query as one piece: give it the longest word, filter by the rest
            longest = max(words, key=len)
            if len(longest) >= _MIN_MOEX_QUERY:
                found = [b for b in self._search_moex(longest)
                         if all(w in _key_of(b) for w in keys)]
            elif unavailable is not None:
                raise unavailable
        exact = query.strip().upper()
        return sorted(found, key=lambda b: (exact not in (b.secid, b.isin),
                                            b.matdate or date.max, b.name))

    def listed(self, code: str) -> Bond | None:
        """The traded bond with this ISIN or MOEX code, from the cached list alone — never a
        request per bond; None if it isn't traded, or MOEX did not send the list."""
        code = code.strip().upper()
        try:
            listed = self._traded()
        except MoexUnavailable:
            return None
        return next((b for _, b in listed if code and code in (b.secid, b.isin)), None)

    def lookup(self, code: str) -> Bond | None:
        """The bond with this ISIN or MOEX code, traded or not; None if MOEX doesn't know it.

        Raises MoexUnavailable when MOEX could not be asked."""
        code = code.strip().upper()
        if not code:
            return None
        found = self.listed(code)
        if found is not None:
            return found
        try:
            return self._describe(code)
        except Exception as exc:
            raise MoexUnavailable(f"MOEX lookup failed ({exc})") from exc
