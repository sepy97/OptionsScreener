from __future__ import annotations

import threading
import time

import pytest

from wheel_screener.core.errors import AuthExpiredError, ProviderDataError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, ProviderCaps, Underlying
from wheel_screener.core.pipeline.pull_chains import pull_chains


class _FakeChains:
    """symbol -> Exception to raise, or None to return an (empty) snapshot."""

    def __init__(self, behavior: dict) -> None:
        self._behavior = behavior

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        b = self._behavior.get(symbol)
        if isinstance(b, Exception):
            raise b
        return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(name="fake", max_concurrency=2)


def _u(sym: str) -> Underlying:
    return Underlying(symbol=sym)


def test_pull_chains_skips_per_symbol_data_error() -> None:
    prov = _FakeChains({"AAA": None, "BBB": ProviderDataError("bad payload")})
    out, complete = pull_chains(prov, [_u("AAA"), _u("BBB")], ChainFilter())
    assert set(out) == {"AAA"}  # one bad symbol dropped, scan continues
    assert complete is True  # a per-symbol drop is still a COMPLETE scan (everyone was tried)


def test_pull_chains_reraises_systemic_error() -> None:
    # an expired token must NOT be masked as "no candidates"
    prov = _FakeChains({"AAA": AuthExpiredError("token expired")})
    with pytest.raises(AuthExpiredError):
        pull_chains(prov, [_u("AAA")], ChainFilter())


def test_pull_chains_skips_when_deadline_already_passed() -> None:
    prov = _FakeChains({"AAA": None, "BBB": None})
    # injected clock reads 10.0; deadline is 5.0 -> no budget left
    out, complete = pull_chains(
        prov, [_u("AAA"), _u("BBB")], ChainFilter(),
        deadline=5.0, monotonic=lambda: 10.0,
    )
    assert out == {} and complete is False  # no budget -> nothing scanned, flagged incomplete


class _CancelOnNth:
    """Serial provider that trips a cancel event on its Nth call -> deterministic partial."""

    def __init__(self, cancel: threading.Event, trip_on: int) -> None:
        self._cancel = cancel
        self._trip_on = trip_on
        self.calls = 0

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        self.calls += 1
        if self.calls >= self._trip_on:
            self._cancel.set()
        return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(name="fake", max_concurrency=1)  # serial = deterministic ordering


def test_pull_chains_cancellation_returns_partial() -> None:
    cancel = threading.Event()
    prov = _CancelOnNth(cancel, trip_on=2)  # cancel set while fetching the 2nd name
    out, complete = pull_chains(
        prov, [_u("AAA"), _u("BBB"), _u("CCC")], ChainFilter(), cancel=cancel
    )
    assert "AAA" in out and "CCC" not in out and len(out) < 3  # partial, not all-or-nothing
    assert complete is False  # cancel cut it short


class _SlowChains:
    """Every pull sleeps, so a short deadline forces a timeout."""

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        time.sleep(0.3)
        return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(name="fake", max_concurrency=1)


def test_pull_chains_timeout_flags_incomplete() -> None:
    # wait_for = 0.05s but each pull takes 0.3s -> FuturesTimeout -> partial AND flagged incomplete
    out, complete = pull_chains(
        _SlowChains(), [_u("AAA"), _u("BBB")], ChainFilter(),
        deadline=0.05, monotonic=lambda: 0.0,
    )
    assert complete is False and len(out) < 2  # the silent-timeout bug: now surfaced via `complete`


# --- prefetch integration -------------------------------------------------------------------

def test_pull_chains_skips_names_the_prefetch_says_are_empty() -> None:
    """Names with no contracts in the window shouldn't cost a chain request at all."""
    pulled = []

    class _Batchable:
        def capabilities(self):
            return ProviderCaps(name="x", max_concurrency=2, supports_batch_underlyings=True)

        def prefetch(self, symbols, filt):
            return {"B", "C"}  # only A has anything

        def get_chain(self, symbol, filt):
            pulled.append(symbol)
            return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    survivors = [_u(s) for s in ("A", "B", "C")]
    chains, complete = pull_chains(_Batchable(), survivors, ChainFilter())
    assert pulled == ["A"], "B and C must never be requested"
    assert set(chains) == {"A"} and complete is True


def test_a_failed_prefetch_falls_back_instead_of_losing_the_screen() -> None:
    pulled = []

    class _Broken:
        def capabilities(self):
            return ProviderCaps(name="x", max_concurrency=2, supports_batch_underlyings=True)

        def prefetch(self, symbols, filt):
            raise RuntimeError("bulk endpoint hiccup")

        def get_chain(self, symbol, filt):
            pulled.append(symbol)
            return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    chains, complete = pull_chains(_Broken(), [_u(s) for s in ("A", "B")], ChainFilter())
    assert sorted(pulled) == ["A", "B"], "a lost speed-up must not cost us the run"
    assert set(chains) == {"A", "B"} and complete is True


def test_a_provider_without_batching_is_untouched() -> None:
    pulled = []

    class _Solo:
        def capabilities(self):
            return ProviderCaps(name="x", max_concurrency=2, supports_batch_underlyings=False)

        def get_chain(self, symbol, filt):
            pulled.append(symbol)
            return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    pull_chains(_Solo(), [_u(s) for s in ("A", "B")], ChainFilter())
    assert sorted(pulled) == ["A", "B"]


def test_a_batch_capable_provider_skips_the_per_name_thread_pool() -> None:
    """The thread pool was never the lever: every worker queues on the same per-minute request
    budget, so eight threads and one thread finish together. A provider that can answer for many
    names in a few requests must be asked that way instead."""
    class _Batch:
        def __init__(self) -> None:
            self.batched: list[str] | None = None
            self.per_name: list[str] = []

        def capabilities(self):
            return ProviderCaps(name="fake", supports_batch_chains=True, max_concurrency=8)

        def get_chains(self, symbols, filt, *, cancel=None, deadline=None):
            self.batched = list(symbols)
            return {s: ChainSnapshot(underlying_symbol=s, contracts=[]) for s in symbols}, True

        def get_chain(self, symbol, filt):
            self.per_name.append(symbol)
            return ChainSnapshot(underlying_symbol=symbol, contracts=[])

    prov = _Batch()
    chains, complete = pull_chains(prov, [_u("AAA"), _u("BBB")], ChainFilter())
    assert complete and set(chains) == {"AAA", "BBB"}
    assert prov.batched == ["AAA", "BBB"]
    assert prov.per_name == [], "no per-name fetch when the provider can batch"


def test_a_batch_provider_reporting_an_incomplete_fetch_marks_the_scan_partial() -> None:
    class _Cut:
        def capabilities(self):
            return ProviderCaps(name="fake", supports_batch_chains=True)

        def get_chains(self, symbols, filt, *, cancel=None, deadline=None):
            return {symbols[0]: ChainSnapshot(underlying_symbol=symbols[0],
                                              contracts=[])}, False  # cancelled/timed out mid-fetch

        def get_chain(self, symbol, filt):
            raise AssertionError("must not fall back to per-name")

    chains, complete = pull_chains(_Cut(), [_u("AAA"), _u("BBB")], ChainFilter())
    assert complete is False and set(chains) == {"AAA"}
