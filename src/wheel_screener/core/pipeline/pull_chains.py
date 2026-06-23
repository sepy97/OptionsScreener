"""Stage 3 — pull option chains for the fundamental survivors (concurrently)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from wheel_screener.core.errors import ProviderDataError, ProviderError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, Underlying
from wheel_screener.core.ports import ChainProvider

logger = logging.getLogger(__name__)


def pull_chains(
    provider: ChainProvider, survivors: list[Underlying], filt: ChainFilter
) -> dict[str, ChainSnapshot]:
    """Fetch a chain per survivor concurrently (bounded by the provider's max_concurrency).

    Per-symbol data errors are logged and skipped so one bad name can't sink the scan.
    Systemic failures (auth expired, rate limited, provider unreachable) PROPAGATE — a
    silently-empty result is indistinguishable from "nothing matched" and would mislead
    the caller (and any UI) into showing no candidates when the provider is actually down.
    """
    if not survivors:
        return {}
    workers = max(1, provider.capabilities().max_concurrency)
    out: dict[str, ChainSnapshot] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(provider.get_chain, u.symbol, filt): u.symbol for u in survivors}
        for fut in as_completed(futures):
            symbol = futures[fut]
            try:
                out[symbol] = fut.result()
            except ProviderDataError as e:
                logger.warning("dropping %s: malformed chain (%s)", symbol, e)
            except ProviderError:
                raise  # systemic (auth/rate/outage) — surface it, don't mask as no-results
            except Exception as e:  # noqa: BLE001 - unexpected per-symbol issue: skip, keep scanning
                logger.warning("dropping %s: unexpected error (%s)", symbol, e)
    return out
