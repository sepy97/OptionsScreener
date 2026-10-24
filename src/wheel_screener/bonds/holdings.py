"""Bonds held in portfolios — and the calendar of them, with each person's quantities.

A person keeps any number of named portfolios; a holding is one bond in one portfolio, with how
many of it they hold. The same bond may be in several portfolios (two brokerage accounts).

The calendar is built for the person's bonds — every portfolio's, each bond once and without
quantities — so the MOEX build (``BondCalendarService``) is shared by every view of it: one
portfolio or all of them, any quantities. ``shape`` then puts the quantities in for the
portfolios shown: an event's quantity is the sum over the portfolios holding that bond, its total
that quantity times the payment, and ``holders`` says which portfolios those are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from wheel_screener.bonds import bond_calendar as bc

NAME_CHARS = 200


@dataclass(frozen=True)
class Holding:
    portfolio_id: str
    portfolio: str  # the portfolio's name
    code: str  # ISIN or MOEX code; "" while not found by name, "-" for a bond MOEX dropped
    name: str
    matdate: date | None
    qty: float | None  # None only for a bond imported from a list without quantities

    @property
    def key(self) -> str:
        return bc.item_key(self.code, self.name)

    def item(self, *, with_qty: bool = True) -> dict:
        """The bond as ``bond_calendar`` reads a watchlist line."""
        return {"code": self.code, "name": self.name, "matdate": self.matdate,
                "qty": self.qty if with_qty else None}


def clean_name(name: str | None) -> str:
    """A name as the CSV can carry it back: one line (the text is read line by line), no ``#``
    (the rest of a line after it is a comment)."""
    return " ".join(str(name or "").replace("#", " ").split())[:NAME_CHARS]


def as_text(holdings: list[Holding]) -> str:
    """Holdings as the script's watchlist CSV (``code,name,matdate,qty``)."""
    return bc.format_watchlist([h.item() for h in holdings])


def bonds_text(holdings: list[Holding]) -> str:
    """The bonds to build a calendar for: each once, without quantities — the build's cache key,
    so changing a quantity or which portfolio is shown never costs a MOEX round."""
    seen: dict[str, dict] = {}
    for h in holdings:
        seen.setdefault(h.key, h.item(with_qty=False))
    return bc.format_watchlist(sorted(seen.values(), key=lambda it: bc.item_key(it["code"],
                                                                               it["name"])))


def shape(events: list[dict], holdings: list[Holding]) -> list[dict]:
    """The events of the bonds in ``holdings`` (the portfolios shown), with their quantities.

    An event belongs to a holding listed under either of the bond's codes (an OFZ is SU26238RMFS4
    to MOEX and RU000A1038V6 by ISIN), or — for a bond found by name during the build — under its
    name. A bond listed under both codes is built twice; the copies are one event here, as they
    are one event in a calendar app (same date, kind and bond)."""
    by_code: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    for i, h in enumerate(holdings):
        if h.code and h.code != bc.NOT_FOUND:
            by_code.setdefault(h.code, []).append(i)
        else:
            by_name.setdefault(h.name.lower(), []).append(i)
    out: list[dict] = []
    seen: set[tuple] = set()
    for e in events:
        ident = (e["date"], e["kind"], e["secid"])
        if ident in seen:
            continue
        found = {i for code in (e["isin"], e["secid"]) if code for i in by_code.get(code, [])}
        found = found or set(by_name.get(str(e["name"]).lower(), []))
        if not found:
            continue  # a bond of a portfolio not shown
        held = [holdings[i] for i in sorted(found)]  # in the portfolios' order
        seen.add(ident)
        qtys = [h.qty for h in held if h.qty is not None]
        qty = sum(qtys) if qtys else None
        out.append({
            **e, "qty": qty,
            "total": e["amount"] * qty if e["amount"] is not None and qty else None,
            "holders": [(h.portfolio, h.qty) for h in held],
        })
    return out


def skip_notes(shown: list[Holding], date_from: date | None) -> list[str]:
    """The build's notes on bonds it left out (matured), counted over the portfolios shown — the
    build itself is of every portfolio."""
    bonds = {h.key: h.item(with_qty=False) for h in shown}
    return bc.skipped(list(bonds.values()), date_from)[2]


def holders_label(e: dict) -> str:
    """"Main 10 · IIS 5" — which portfolios hold an event's bond, and how many each."""
    return " · ".join(f"{name} {bc.fmt_qty(qty)}" if qty is not None else f"{name} (no qty)"
                      for name, qty in e.get("holders", ()))


def with_holders_note(events: list[dict]) -> list[dict]:
    """Events with the portfolios in their note — for the CSV and .ics, which keep the script's
    columns and so have no portfolio column."""
    return [{**e, "note": "; ".join(p for p in (e["note"], f"Portfolios: {holders_label(e)}")
                                    if p)}
            for e in events]
