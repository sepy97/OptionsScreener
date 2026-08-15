"""Pure scoring functions — no I/O, unit-tested in isolation."""

from __future__ import annotations


def annualized_csp_yield(premium: float, strike: float, dte: int) -> float:
    """Annualized return on cash-secured collateral.

    Args:
        premium: credit received per share (mid or bid of the short put).
        strike:  put strike; collateral per share is the strike (the x100 cancels).
        dte:     calendar days to expiration.

    Returns:
        Annualized yield as a fraction (e.g. 0.18 == 18%/yr).

    Raises:
        ValueError: if ``dte`` or ``strike`` is not positive.
    """
    if dte <= 0 or strike <= 0:
        raise ValueError("dte and strike must be positive")
    return (premium / strike) * (365.0 / dte)


def annualized_cc_yield(premium: float, spot: float, dte: int) -> float:
    """Annualized premium yield on a covered call, measured against the share price.

    A covered call posts no cash — the collateral is 100 shares you already hold — so there is no
    "amount set aside" to divide by the way the CSP yield divides by the strike. The denominator
    is what those shares are worth at the current market price: the capital the position ties up.

    Deliberately NOT the holder's cost basis. Basis is per-holder and unknown to the screener, and
    anchoring to it would make the same contract score differently for two people looking at the
    same row. Market price keeps the number comparable across expiries and tickers.

    This is the *static* return — what you keep if the stock goes nowhere and the call expires
    worthless. It excludes the capital gain also realized if the shares are called away.

    Args:
        premium: credit received per share (the bid of the short call).
        spot:    current market price of the underlying.
        dte:     calendar days to expiration.

    Returns:
        Annualized yield as a fraction (e.g. 0.18 == 18%/yr).

    Raises:
        ValueError: if ``dte`` or ``spot`` is not positive.
    """
    if dte <= 0 or spot <= 0:
        raise ValueError("dte and spot must be positive")
    return (premium / spot) * (365.0 / dte)
