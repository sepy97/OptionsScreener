"""Pure Schwab `/marketdata/v1/chains` JSON -> core ChainSnapshot/OptionContract mapping.

The chain nests contracts under callExpDateMap / putExpDateMap, keyed 'YYYY-MM-DD:DTE'
then strike. Schwab uses -999.0 as an "unavailable" sentinel for greeks/IV.
"""

from __future__ import annotations

from datetime import date

from wheel_screener.core.models import ChainSnapshot, GreeksSource, OptionContract, OptionType

_SENTINEL = -999.0


def _num(v: object) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f <= _SENTINEL else f


def _int(v: object) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _exp(key: str) -> date | None:
    try:
        return date.fromisoformat(key.split(":", 1)[0])
    except (ValueError, AttributeError, IndexError):
        return None


def _contract(o: dict, opt_type: OptionType, exp: date, underlying: str, und_price: float | None):
    strike = _num(o.get("strikePrice"))
    if strike is None:
        return None
    iv = _num(o.get("volatility"))  # Schwab IV is in percent (e.g. 33.77)
    bid = _num(o.get("bid"))
    ask = _num(o.get("ask"))
    # true midpoint (NOT Schwab's "mark", which is a model price); mark is kept in raw
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    return OptionContract(
        underlying_symbol=underlying,
        option_symbol=o.get("symbol") or "",
        option_type=opt_type,
        expiration=exp,
        strike=strike,
        dte=_int(o.get("daysToExpiration")) or 0,
        bid=bid,
        ask=ask,
        last=_num(o.get("last")),
        mid=mid,
        bid_size=_int(o.get("bidSize")),
        ask_size=_int(o.get("askSize")),
        volume=_int(o.get("totalVolume")),
        open_interest=_int(o.get("openInterest")),
        delta=_num(o.get("delta")),
        gamma=_num(o.get("gamma")),
        theta=_num(o.get("theta")),
        vega=_num(o.get("vega")),
        implied_volatility=(iv / 100.0 if iv is not None else None),
        underlying_price=und_price,
        greeks_source=GreeksSource.VENDOR_DEFAULT,
        raw={
            k: o.get(k)
            for k in ("mark", "rho", "timeValue", "theoreticalOptionValue", "intrinsicValue")
            if k in o
        },
    )


def parse_chain(payload: dict) -> ChainSnapshot:
    underlying = payload.get("symbol") or ""
    und_price = _num(payload.get("underlyingPrice"))
    contracts: list[OptionContract] = []
    maps = (("putExpDateMap", OptionType.PUT), ("callExpDateMap", OptionType.CALL))
    for map_key, opt_type in maps:
        for exp_key, strikes in (payload.get(map_key) or {}).items():
            exp = _exp(exp_key)
            if exp is None:
                continue
            for rows in (strikes or {}).values():
                for o in rows or []:
                    c = _contract(o, opt_type, exp, underlying, und_price)
                    if c is not None:
                        contracts.append(c)
    return ChainSnapshot(
        underlying_symbol=underlying, underlying_price=und_price, contracts=contracts
    )
