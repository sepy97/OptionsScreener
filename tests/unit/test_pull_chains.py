from __future__ import annotations

import threading

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
    out = pull_chains(prov, [_u("AAA"), _u("BBB")], ChainFilter())
    assert set(out) == {"AAA"}  # one bad symbol dropped, scan continues


def test_pull_chains_reraises_systemic_error() -> None:
    # an expired token must NOT be masked as "no candidates"
    prov = _FakeChains({"AAA": AuthExpiredError("token expired")})
    with pytest.raises(AuthExpiredError):
        pull_chains(prov, [_u("AAA")], ChainFilter())


def test_pull_chains_skips_when_deadline_already_passed() -> None:
    prov = _FakeChains({"AAA": None, "BBB": None})
    # injected clock reads 10.0; deadline is 5.0 -> no budget left
    out = pull_chains(
        prov, [_u("AAA"), _u("BBB")], ChainFilter(),
        deadline=5.0, monotonic=lambda: 10.0,
    )
    assert out == {}


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
    out = pull_chains(
        prov, [_u("AAA"), _u("BBB"), _u("CCC")], ChainFilter(), cancel=cancel
    )
    assert "AAA" in out and "CCC" not in out and len(out) < 3  # partial, not all-or-nothing
