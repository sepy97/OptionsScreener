"""Stage 3 — strike selection: the put nearest the target delta."""

from __future__ import annotations

from wheel_screener.core.models import ChainSnapshot, OptionContract, OptionType


def nearest_to_delta(
    contracts: list[OptionContract],
    target_delta: float,
    option_type: OptionType = OptionType.PUT,
) -> OptionContract | None:
    """Return the contract whose delta is closest to ``target_delta``.

    For puts, delta is negative and ``target_delta`` is typically -0.20. Contracts
    of the wrong type or with no delta are ignored. Returns None if none qualify.
    """
    candidates = [
        c for c in contracts if c.option_type == option_type and c.delta is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.delta - target_delta))


def select_strike(snapshot: ChainSnapshot, target_delta: float) -> OptionContract | None:
    """Stage-3 wrapper: apply DTE/liquidity gates, then ``nearest_to_delta``.

    TODO(M2): gate on DTE window, open interest, and bid/ask spread before selecting.
    """
    raise NotImplementedError("Stage 3 wiring lands in M2")
