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
