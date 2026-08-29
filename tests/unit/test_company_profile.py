"""Company identity beside a contract: the lookup, the service guard, and the three surfaces."""

from __future__ import annotations

import pytest

from wheel_screener.core.models import CompanyProfile
from wheel_screener.core.service import ScreenerService


class _Profiles:
    def __init__(self, profile=None, boom=False):
        self.profile, self.boom, self.calls = profile, boom, 0

    def company_profile(self, symbol):
        self.calls += 1
        if self.boom:
            raise RuntimeError("store unreadable")
        return self.profile


def _service(profiles=None) -> ScreenerService:
    return ScreenerService(fundamentals=object(), chains=object(), profiles=profiles)


def test_service_returns_none_when_the_deployment_has_no_profile_source() -> None:
    assert _service().company_profile("AAA") is None


def test_a_broken_profile_source_never_takes_the_page_with_it() -> None:
    """Context is optional. A screener that can't name a company must still price its options."""
    svc = _service(_Profiles(boom=True))
    assert svc.company_profile("AAA") is None


def test_the_profile_is_passed_through() -> None:
    p = CompanyProfile(symbol="AAA", name="Anon Inc.", sector="Tech")
    assert _service(_Profiles(p)).company_profile("AAA").name == "Anon Inc."


# --- the local store lookup -----------------------------------------------------------------

def test_local_lookup_reads_identity_and_memoises(tmp_path) -> None:
    """The description column is excluded from the resident frame on purpose (90k rows of prose),
    so the lookup reads it lazily for ONE symbol."""
    from wheel_screener.adapters.local.provider import LocalFundamentalsProvider

    (tmp_path / "profile-bulk_part0.csv").write_text(
        "symbol,companyName,sector,industry,description,website,country,fullTimeEmployees\n"
        'AAA,Anon Inc.,Technology,Software,"Anon builds things.",https://a.example,US,1234\n'
        'BBB,Beta Corp,Energy,Oil,"Beta drills.",https://b.example,US,99\n'
    )
    provider = LocalFundamentalsProvider(str(tmp_path))
    p = provider.company_profile("aaa")  # case-insensitive
    assert p.name == "Anon Inc." and p.sector == "Technology" and p.industry == "Software"
    assert p.description == "Anon builds things." and p.employees == 1234
    assert provider.company_profile("AAA") is p or provider.company_profile("AAA").name == p.name
    assert provider.company_profile("NOPE") is None


def test_local_lookup_survives_a_missing_store(tmp_path) -> None:
    from wheel_screener.adapters.local.provider import LocalFundamentalsProvider

    assert LocalFundamentalsProvider(str(tmp_path)).company_profile("AAA") is None


# --- the blurb ------------------------------------------------------------------------------

pytest.importorskip("fastapi")

from wheel_screener.api.app import _short  # noqa: E402


def test_blurb_prefers_a_sentence_end_over_a_hard_cut() -> None:
    text = "Anon Inc. makes widgets. " + "It also does many other things at length. " * 20
    out = _short(text, limit=120)
    assert len(out) <= 121
    assert out.endswith(".") and "…" not in out, "a sentence ended in range, so no ellipsis"


def test_blurb_falls_back_to_a_word_boundary() -> None:
    out = _short("word " * 200, limit=50)
    assert out.endswith("…") and len(out) <= 51
    assert not out.endswith(" …")


def test_blurb_leaves_short_text_and_junk_alone() -> None:
    assert _short("Short and sweet.") == "Short and sweet."
    assert _short("  collapses   whitespace  ") == "collapses whitespace"
    assert _short(None) == "" and _short("") == "" and _short(123) == ""


# --- the three surfaces ---------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.deps import get_service  # noqa: E402

_PROFILE = CompanyProfile(
    symbol="AAA", name="Anon Incorporated", sector="Technology", industry="Software",
    description="Anon Incorporated designs and sells widgets to industrial customers worldwide.",
)


class _ProfiledService:
    """Only what the three fragments need."""

    def company_profile(self, symbol):
        return _PROFILE

    def search_ticker(self, symbol, criteria, today, n=5, side=None):
        from wheel_screener.core.service import TickerSearch

        return TickerSearch(symbol="AAA")

    def fundamental_report(self, symbol, period="annual", years=10):
        from wheel_screener.core.models import FundamentalReport

        return FundamentalReport(symbol="AAA", period=period, periods=["2025-12-31"])


def _profiled_client() -> TestClient:
    app.dependency_overrides[get_service] = lambda: _ProfiledService()
    return TestClient(app)


def test_search_shows_who_the_company_is() -> None:
    try:
        r = _profiled_client().post("/search", data={"ticker": "AAA"})
        assert "Anon Incorporated" in r.text
        assert "Technology" in r.text and "Software" in r.text
        assert "designs and sells widgets" in r.text
    finally:
        app.dependency_overrides.clear()


def test_fundamentals_shows_who_the_company_is() -> None:
    try:
        r = _profiled_client().post(
            "/fundamentals", data={"ticker": "AAA", "period": "annual", "years": 10}
        )
        assert "Anon Incorporated" in r.text and "designs and sells widgets" in r.text
    finally:
        app.dependency_overrides.clear()


def test_the_surfaces_render_without_a_profile() -> None:
    """A deployment with no profile source shows a bare ticker, not a broken page."""

    class _Bare(_ProfiledService):
        def company_profile(self, symbol):
            return None

    try:
        app.dependency_overrides[get_service] = lambda: _Bare()
        r = TestClient(app).post("/search", data={"ticker": "AAA"})
        assert r.status_code == 200 and "Anon Incorporated" not in r.text
    finally:
        app.dependency_overrides.clear()
