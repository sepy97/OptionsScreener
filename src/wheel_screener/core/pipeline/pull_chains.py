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
) -> tuple[dict[str, ChainSnapshot], bool]:
    """Fetch a chain per survivor concurrently (bounded by the provider's max_concurrency).

    Returns ``(chains, complete)``. ``complete`` is False when a ``deadline`` (monotonic seconds)
    or ``cancel`` cut the scan short, so the caller can flag the run as PARTIAL instead of silently
    treating a timed-out run as a complete one. Per-symbol data errors are logged and skipped (still
    a complete scan); systemic failures (auth/rate/outage) PROPAGATE so the caller can surface them.
    """
    if not survivors:
        return {}, True
    if deadline is not None and monotonic() >= deadline:
        logger.warning("chain pull skipped: no time budget remaining")
        return {}, False

    caps = provider.capabilities()
    workers = max(1, caps.max_concurrency)
    targets = _skip_empty_chains(provider, survivors, filt) if caps.supports_batch_underlyings \
        else survivors
    out: dict[str, ChainSnapshot] = {}
    complete = True
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(provider.get_chain, u.symbol, filt): u.symbol for u in targets}
        wait_for = None if deadline is None else max(0.0, deadline - monotonic())
        try:
            for fut in as_completed(futures, timeout=wait_for):
                if cancel is not None and cancel.is_set():
                    logger.warning(
                        "chain pull cancelled; %d/%d collected", len(out), len(survivors)
                    )
                    complete = False
                    break
                symbol = futures[fut]
                try:
                    out[symbol] = fut.result()
                except ProviderDataError as e:
                    logger.debug("dropping %s: malformed/empty chain (%s)", symbol, e)  # routine
                except ProviderError:
                    raise  # systemic (auth/rate/outage) — surface it, don't mask
                except Exception as e:  # noqa: BLE001 - unexpected per-symbol issue: skip
                    logger.warning("dropping %s: unexpected error (%s)", symbol, e)
        except FuturesTimeout:
            logger.warning("chain pull timed out; %d/%d collected", len(out), len(survivors))
            complete = False
        finally:
            for f in futures:
                f.cancel()  # cancel any not-yet-started pulls
    logger.info("chains: %d/%d survivors returned a chain", len(out), len(survivors))
    return out, complete


def _skip_empty_chains(provider, survivors, filt):
    """Ask a batch-capable provider which names have no contracts at all, and drop them.

    Most of a screen's chain requests return nothing: measured on a 400-name run, 61% of names
    had no put in the DTE window, and we spent a request on every one. A provider that can answer
    for many underlyings at once resolves that in a handful of calls.

    Best-effort by design — a prefetch failure falls back to pulling every name, because losing a
    speed-up is not a reason to lose a screen.
    """
    prefetch = getattr(provider, "prefetch", None)
    if prefetch is None:
        return survivors
    try:
        empty = prefetch([u.symbol for u in survivors], filt)
    except ProviderError:
        raise  # systemic (auth/rate/outage) — the per-symbol pulls would fail the same way
    except Exception as e:  # noqa: BLE001 - any other prefetch problem is not fatal
        logger.warning("chain prefetch failed (%s); pulling every name individually", e)
        return survivors
    if not empty:
        return survivors
    logger.info(
        "prefetch: %d/%d names have no contracts in the window — skipping their chain pull",
        len(empty), len(survivors),
    )
    return [u for u in survivors if u.symbol not in empty]
