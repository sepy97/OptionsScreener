"""Stage 3 — pull 30-45 DTE option chains for the fundamental survivors."""

from __future__ import annotations

from wheel_screener.core.models import ChainFilter, ChainSnapshot, Underlying
from wheel_screener.core.ports import ChainProvider


def pull_chains(
    provider: ChainProvider, survivors: list[Underlying], filt: ChainFilter
) -> dict[str, ChainSnapshot]:
    """Fetch a chain per survivor (one request per underlying; the adapter throttles).

    A symbol whose chain can't be fetched is simply omitted.
    """
    out: dict[str, ChainSnapshot] = {}
    for u in survivors:
        try:
            out[u.symbol] = provider.get_chain(u.symbol, filt)
        except Exception:  # noqa: BLE001 - one bad symbol shouldn't sink the whole scan
            continue
    return out
