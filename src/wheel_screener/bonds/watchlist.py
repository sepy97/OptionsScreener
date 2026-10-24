"""Editing a watchlist one bond at a time — what the Bond calendar tab's list does to its text.

A watchlist is kept as the text the script writes (``code,name,matdate,qty``). Each edit parses it,
changes one bond and writes it back the same way, so a list built here reads exactly like an
uploaded one, and the calendar and the downloads never know the difference. A bond is named by
its key (``bond_calendar.item_key``): its code, or its name while it has none.
"""

from __future__ import annotations

import math

from wheel_screener.bonds import bond_calendar as bc

_NAME_CHARS = 200


class WatchlistError(ValueError):
    """An edit that can't be made; the message says why, in words for the page."""


def rows(text: str) -> list[dict]:
    """The bonds of ``text`` as the list shows them: by name, each with its ``key``."""
    items = bc.parse_watchlist(text)
    for it in items:
        it["key"] = bc.item_key(it["code"], it["name"])
    return sorted(items, key=lambda it: ((it["name"] or it["code"]).lower(), it["code"]))


def codes(text: str) -> set[str]:
    return {it["code"] for it in bc.parse_watchlist(text) if it["code"]}


def clean(item: dict) -> dict:
    """A bond as the saved text can carry it back: one line (the text is read line by line), no
    ``#`` (the rest of a line after it is a comment)."""
    name = " ".join(str(item["name"] or "").replace("#", " ").split())[:_NAME_CHARS]
    return {**item, "code": str(item["code"] or "").strip().upper(), "name": name}


def add(text: str, item: dict, *, aliases: tuple[str, ...] = (), limit: int) -> str:
    """``text`` with ``item`` (code, name, matdate, qty) added at the end. ``aliases`` are the
    bond's other codes — an OFZ is SU26238RMFS4 to MOEX and RU000A1038V6 by ISIN — so a bond
    already listed under either is not added twice."""
    item = clean(item)
    items = bc.parse_watchlist(text)
    key = bc.item_key(item["code"], item["name"])
    known = {bc.item_key(it["code"], it["name"]) for it in items}
    if key in known or any(a and a in known for a in aliases):
        raise WatchlistError(f"{item['name'] or item['code']} is already in your watchlist.")
    if len(items) >= limit:
        raise WatchlistError(f"A watchlist holds at most {limit} bonds — remove one first.")
    return bc.format_watchlist([*items, item])


def remove(text: str, key: str) -> tuple[str, dict]:
    """``text`` without the bond ``key``, and that bond (to put back if the removal is undone)."""
    items = bc.parse_watchlist(text)
    for i, it in enumerate(items):
        if bc.item_key(it["code"], it["name"]) == key:
            return bc.format_watchlist(items[:i] + items[i + 1:]), it
    raise WatchlistError("That bond is no longer in your watchlist.")


def set_qty(text: str, key: str, qty: float | None) -> tuple[str, dict]:
    """``text`` with bond ``key`` held in ``qty`` (None = no quantity), and that bond."""
    if qty is not None and not (math.isfinite(qty) and qty >= 0):
        raise WatchlistError("a quantity is a number, 0 or more")
    items = bc.parse_watchlist(text)
    for it in items:
        if bc.item_key(it["code"], it["name"]) == key:
            it["qty"] = qty
            return bc.format_watchlist(items), it
    raise WatchlistError("That bond is no longer in your watchlist.")
