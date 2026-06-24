"""Stage 3 — pull option chains for the fundamental survivors (concurrently)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

from wheel_screener.core.errors import ProviderDataError, ProviderError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, Underlying
from wheel_screener.core.ports import ChainProvider

logger = logging.getLogger(__name__)


def pull_chains(
    provider: ChainProvider,
    survivors: list[Underlying],
    filt: ChainFilter,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, ChainSnapshot]:
    """Fetch a chain per survivor concurrently (bounded by the provider's max_concurrency).

    Per-symbol data errors are logged and skipped; systemic failures (auth/rate/outage)
    PROPAGATE so the caller can surface them. If a ``deadline`` (monotonic seconds) passes
    or ``cancel`` is set, collection stops and whatever completed so far is returned
    (partial) rather than blocking — this is what makes the run cancellable + time-bounded.
    """
    if not survivors:
        return {}
    if deadline is not None and monotonic() >= deadline:
        logger.warning("chain pull skipped: no time budget remaining")
        return {}

    workers = max(1, provider.capabilities().max_concurrency)
    out: dict[str, ChainSnapshot] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(provider.get_chain, u.symbol, filt): u.symbol for u in survivors}
        wait_for = None if deadline is None else max(0.0, deadline - monotonic())
        try:
            for fut in as_completed(futures, timeout=wait_for):
                if cancel is not None and cancel.is_set():
                    logger.warning(
                        "chain pull cancelled; %d/%d collected", len(out), len(survivors)
                    )
                    break
                symbol = futures[fut]
                try:
                    out[symbol] = fut.result()
                except ProviderDataError as e:
                    logger.warning("dropping %s: malformed chain (%s)", symbol, e)
                except ProviderError:
                    raise  # systemic (auth/rate/outage) — surface it, don't mask
                except Exception as e:  # noqa: BLE001 - unexpected per-symbol issue: skip
                    logger.warning("dropping %s: unexpected error (%s)", symbol, e)
        except FuturesTimeout:
            logger.warning("chain pull timed out; %d/%d collected", len(out), len(survivors))
        finally:
            for f in futures:
                f.cancel()  # cancel any not-yet-started pulls
    return out
