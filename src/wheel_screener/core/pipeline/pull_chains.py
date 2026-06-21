"""Stage 2 — pull 30-45 DTE option chains for the pre-filter survivors."""

from __future__ import annotations

from wheel_screener.core.models import ChainFilter, ChainSnapshot, Underlying
from wheel_screener.core.ports import ChainProvider


def pull_chains(
    provider: ChainProvider, survivors: list[Underlying], filt: ChainFilter
) -> dict[str, ChainSnapshot]:
    """Fetch chains for survivors only, throttled within the provider's limits.

    TODO(M2): bounded-concurrency fan-out via the Schwab adapter (one request per
    underlying, ~120 req/min), honoring ``provider.capabilities()``.
    """
    raise NotImplementedError("Stage 2 (chain pull) lands in M2")
