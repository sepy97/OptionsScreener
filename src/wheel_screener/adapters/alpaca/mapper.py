"""Pure Alpaca options JSON -> core ChainSnapshot/OptionContract mapping.

Alpaca splits what we need across two endpoints: the market-data *snapshot*
(``latestQuote``/``latestTrade``/``greeks``/``impliedVolatility``, keyed by OCC symbol) and the
reference *contracts* endpoint (``open_interest``). We merge them by OCC symbol; strike,
expiration and type are parsed from the OCC/OSI symbol itself. Alpaca IV is already a fraction
(0.345), unlike Schwab's percent. Missing fields are simply null (no -999 sentinels).
"""

from __future__ import annotations

from datetime import date

from wheel_screener.core.models import ChainSnapshot, GreeksSource, OptionContract, OptionType


def _num(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: object) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def parse_occ_symbol(symbol: str) -> tuple[str, date, OptionType, float] | None:
    """OCC/OSI symbol -> (root, expiration, option_type, strike), or None if unparseable.

    Format: ROOT + YYMMDD + (C|P) + strike×1000 zero-padded to 8 digits
    (e.g. ``AAPL260815P00190000`` -> AAPL, 2026-08-15, PUT, 190.0)."""
    if not symbol or len(symbol) < 16:  # >=1 root + 6 date + 1 type + 8 strike
        return None
    try:
        strike = int(symbol[-8:]) / 1000.0
        t = symbol[-9].upper()
        if t not in ("C", "P"):
            return None
        d = symbol[-15:-9]
        exp = date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))
        root = symbol[:-15]
    except (ValueError, IndexError):
        return None
    if not root:
        return None
    return root, exp, (OptionType.PUT if t == "P" else OptionType.CALL), strike


def _contract(
    occ: str, snap: dict, oi_by_symbol: dict, underlying: str, today: date
) -> OptionContract | None:
    parsed = parse_occ_symbol(occ)
    if parsed is None:
        return None
    _root, exp, opt_type, strike = parsed
    q = snap.get("latestQuote") or {}
    g = snap.get("greeks") or {}
    trade = snap.get("latestTrade") or {}
    bid, ask = _num(q.get("bp")), _num(q.get("ap"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    return OptionContract(
        underlying_symbol=underlying,
        option_symbol=occ,
        option_type=opt_type,
        expiration=exp,
        strike=strike,
        dte=max((exp - today).days, 0),
        bid=bid,
        ask=ask,
        last=_num(trade.get("p")),
        mid=mid,
        bid_size=_int(q.get("bs")),
        ask_size=_int(q.get("as")),
        open_interest=_int(oi_by_symbol.get(occ)),
        delta=_num(g.get("delta")),
        gamma=_num(g.get("gamma")),
        theta=_num(g.get("theta")),
        vega=_num(g.get("vega")),
        implied_volatility=_num(snap.get("impliedVolatility")),  # already a fraction
        greeks_source=GreeksSource.VENDOR_DEFAULT,
        raw={"rho": g.get("rho")} if g.get("rho") is not None else {},
    )


def build_chain(
    underlying: str, snapshots: dict | None, oi_by_symbol: dict | None, today: date
) -> ChainSnapshot:
    # underlying_price stays None: Alpaca's option snapshot is option-only (no spot), and the field
    # is informational — unused by the select/yield/rank pipeline — so we don't pay an extra
    # stock-quote call per underlying just to fill it. (Schwab returns it in-band, hence the gap.)
    oi = oi_by_symbol or {}
    contracts = [
        c
        for occ, snap in (snapshots or {}).items()
        if (c := _contract(occ, snap or {}, oi, underlying, today)) is not None
    ]
    return ChainSnapshot(underlying_symbol=underlying, contracts=contracts)
