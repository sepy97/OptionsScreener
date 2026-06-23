from __future__ import annotations

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
