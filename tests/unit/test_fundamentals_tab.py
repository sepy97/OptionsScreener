"""The Fundamentals tab: adapter mapping/caching and the HTML routes.

The analysis engine is a separate private package that this repo does not depend on, so none
of these tests may import it. The adapter is written to take its collaborators as arguments
(or resolve them through ``import_module``) precisely so it stays testable without it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.fundcore import provider as fc
from wheel_screener.config import FundcoreSettings
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.models import FundamentalReport, ReportCell, ReportGroup, ReportRow
from wheel_screener.core.ports import FundamentalReportProvider

# --- fakes standing in for the engine ------------------------------------------------------

class _ConfigError(Exception):
    pass


class _InsufficientData(Exception):
    pass


class _FMPError(Exception):
    pass


_ERRORS = SimpleNamespace(
    ConfigError=_ConfigError, InsufficientData=_InsufficientData, FMPError=_FMPError
)


def _engine_report(symbol="AAPL", period="annual", periods=("2025-12-31", "2024-12-31")):
    """An object shaped like the engine's report (duck-typed, not imported)."""
    cell = lambda v, g: SimpleNamespace(value=v, grade=g)  # noqa: E731
    row = SimpleNamespace(key="PE", label="P/E", description="P/E <= 10 is great",
                          cells=[cell(12.5, 0.5), cell(None, None)])
    group = SimpleNamespace(key="evaluation", label="Evaluation", rows=[row])
    return SimpleNamespace(symbol=symbol, period=period, periods=list(periods),
                           partial=False, groups=[group])


def _fake_engine(report=None, raises=None):
    def build_report(symbol, period="annual", years=10, api_key=None):
        if raises is not None:
            raise raises
        return report or _engine_report(symbol, period)
    return SimpleNamespace(build_report=build_report, __version__="2.0.0")


def _install(monkeypatch, engine):
    """Point the adapter's lazy import at a stand-in engine."""
    monkeypatch.setattr(
        fc, "import_module",
        lambda name: _ERRORS if name.endswith(".errors") else engine,
    )


def _provider(tmp_path, **kw):
    settings = FundcoreSettings(cache_dir=str(tmp_path / "cache"), **kw)
    return fc.FundcoreReportProvider("test-key", settings)


# --- error mapping -------------------------------------------------------------------------

def test_missing_engine_key_maps_to_auth_error():
    mapped = fc._map_error(_ConfigError("no FMP_API_KEY"), _ERRORS)
    assert isinstance(mapped, AuthExpiredError)


def test_thin_history_maps_to_a_data_error():
    assert isinstance(fc._map_error(_InsufficientData("1 period"), _ERRORS), ProviderDataError)


def test_no_data_at_all_reads_as_a_bad_ticker_not_a_short_history():
    err = _InsufficientData("insufficient annual data for ZZZZ: 0 period(s)")
    err.available, err.symbol = 0, "ZZZZ"
    mapped = fc._map_error(err, _ERRORS)
    assert isinstance(mapped, ProviderDataError)
    assert "is the ticker correct?" in str(mapped) and "period(s)" not in str(mapped)


@pytest.mark.parametrize(
    "status,expected",
    [(401, AuthExpiredError), (403, AuthExpiredError), (429, RateLimitedError),
     (500, ProviderUnavailableError), (503, ProviderUnavailableError),
     (404, ProviderDataError), (400, ProviderDataError)],
)
def test_http_status_behind_an_engine_error_is_recovered(status, expected):
    """The engine raises `FMPError(...) from <http error>`; the status survives on the cause."""
    cause = Exception("boom")
    cause.response = SimpleNamespace(status_code=status)
    err = _FMPError("request failed")
    err.__cause__ = cause
    assert isinstance(fc._map_error(err, _ERRORS), expected)


def test_transport_failure_without_a_status_is_an_outage():
    assert isinstance(fc._map_error(_FMPError("connection reset"), _ERRORS),
                      ProviderUnavailableError)


def test_unknown_failures_stay_provider_errors():
    assert isinstance(fc._map_error(RuntimeError("?"), _ERRORS), ProviderError)


# --- the provider --------------------------------------------------------------------------

def test_satisfies_the_port(tmp_path):
    assert isinstance(_provider(tmp_path), FundamentalReportProvider)


def test_maps_the_engine_report_onto_core_models(tmp_path, monkeypatch):
    _install(monkeypatch, _fake_engine())
    report = _provider(tmp_path).fundamental_report("aapl")
    assert isinstance(report, FundamentalReport)
    assert report.symbol == "AAPL" and report.period == "annual"
    row = report.groups[0].rows[0]
    assert row.label == "P/E" and row.description
    assert [(c.value, c.grade) for c in row.cells] == [(12.5, 0.5), (None, None)]


def test_absent_engine_is_reported_as_unavailable_not_a_crash(tmp_path, monkeypatch):
    def missing(name):
        raise ImportError("No module named 'fundcore'")

    monkeypatch.setattr(fc, "import_module", missing)
    with pytest.raises(ProviderUnavailableError):
        _provider(tmp_path).fundamental_report("AAPL")
    assert fc.engine_available() is False


def test_report_is_cached_so_a_public_page_cannot_hammer_the_api(tmp_path, monkeypatch):
    calls = []

    def build_report(symbol, period="annual", years=10, api_key=None):
        calls.append(symbol)
        return _engine_report(symbol, period)

    _install(monkeypatch, SimpleNamespace(build_report=build_report))
    provider = _provider(tmp_path)
    first = provider.fundamental_report("AAPL")
    second = provider.fundamental_report("AAPL")
    assert len(calls) == 1, "second view should be served from cache"
    assert second.model_dump() == first.model_dump()
    # a different period is a different report
    provider.fundamental_report("AAPL", period="quarter")
    assert len(calls) == 2


def test_a_corrupt_cache_entry_falls_back_to_a_fresh_build(tmp_path, monkeypatch):
    _install(monkeypatch, _fake_engine())
    settings = FundcoreSettings(cache_dir=str(tmp_path / "c"))
    cache = DiskCache(settings.cache_dir, settings.cache_ttl_seconds)
    cache.set("AAPL|annual|10", {"not": "a report"})
    provider = fc.FundcoreReportProvider("k", settings, cache=cache)
    assert provider.fundamental_report("AAPL").symbol == "AAPL"


def test_years_are_clamped_to_the_configured_ceiling(tmp_path, monkeypatch):
    seen = {}

    def build_report(symbol, period="annual", years=10, api_key=None):
        seen["years"] = years
        return _engine_report(symbol, period)

    _install(monkeypatch, SimpleNamespace(build_report=build_report))
    _provider(tmp_path, max_years=12).fundamental_report("AAPL", years=999)
    assert seen["years"] == 12, "an unbounded request would multiply upstream cost"


def test_blank_ticker_is_rejected_before_any_call(tmp_path):
    with pytest.raises(ProviderDataError):
        _provider(tmp_path).fundamental_report("   ")


def test_the_engine_receives_our_api_key(tmp_path, monkeypatch):
    seen = {}

    def build_report(symbol, period="annual", years=10, api_key=None):
        seen["key"] = api_key
        return _engine_report(symbol, period)

    _install(monkeypatch, SimpleNamespace(build_report=build_report))
    _provider(tmp_path).fundamental_report("AAPL")
    assert seen["key"] == "test-key"


# --- the routes ----------------------------------------------------------------------------

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import _grade_class, app  # noqa: E402
from wheel_screener.api.deps import get_service, get_settings  # noqa: E402
from wheel_screener.config import Settings  # noqa: E402


def _core_report():
    return FundamentalReport(
        symbol="AAPL", period="annual", periods=["2025-12-31", "2024-12-31"], partial=True,
        groups=[ReportGroup(key="evaluation", label="Evaluation", rows=[
            ReportRow(key="PE", label="P/E", description="P/E <= 10 is great",
                      cells=[ReportCell(value=12.5, grade=0.5), ReportCell()]),
        ])],
    )


class _StubService:

    def company_profile(self, symbol):
        return None  # optional context; the templates render nothing without it
    def __init__(self, report=None, error=None):
        self._report, self._error = report, error
        self.calls = []

    def fundamental_report(self, symbol, period="annual", years=10):
        self.calls.append((symbol, period, years))
        if self._error is not None:
            raise self._error
        return self._report


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _with_service(service):
    app.dependency_overrides[get_service] = lambda: service


def test_grade_class_tiers():
    assert _grade_class(1.0) == "g-hi" and _grade_class(0.5) == "g-mid"
    assert _grade_class(0.0) == "g-lo"
    assert _grade_class(None) == "", "ungraded must be blank, not styled as poor"


def test_tab_is_reachable_and_linked_from_the_nav(client):
    page = client.get("/fundamentals")
    assert page.status_code == 200
    assert 'href="/fundamentals"' in page.text
    assert 'name="ticker"' in page.text


def test_a_ticker_deep_link_prefills_and_autoruns(client):
    page = client.get("/fundamentals?ticker=msft")
    assert 'value="MSFT"' in page.text
    assert "load, submit" in page.text, "a deep link should run without a second click"


def test_report_renders_as_a_graded_grid(client):
    service = _StubService(report=_core_report())
    _with_service(service)
    r = client.post("/fundamentals", data={"ticker": "aapl", "period": "annual", "years": 10})
    assert r.status_code == 200
    assert "Evaluation" in r.text and "P/E" in r.text
    assert "12.50" in r.text
    assert "g-mid" in r.text, "a 0.5 grade should be tinted"
    assert "Limited history" in r.text, "a partial report must say so"
    assert service.calls == [("aapl", "annual", 10)]


def test_unknown_period_falls_back_to_annual(client):
    service = _StubService(report=_core_report())
    _with_service(service)
    client.post("/fundamentals", data={"ticker": "AAPL", "period": "weekly", "years": 5})
    assert service.calls[0][1] == "annual"


def test_blank_ticker_is_a_422_with_an_explanation(client):
    _with_service(_StubService(report=_core_report()))
    r = client.post("/fundamentals", data={"ticker": "  ", "period": "annual", "years": 10})
    assert r.status_code == 422 and "ticker" in r.text


@pytest.mark.parametrize(
    "error,fragment",
    [(ProviderUnavailableError("fundamental reports are not configured for this deployment"),
      "not configured"),
     (ProviderUnavailableError("the fundamentals engine is not installed in this deployment"),
      "not installed"),
     (ProviderDataError("insufficient annual data for NEWCO: 1 period(s)"), "insufficient"),
     (RateLimitedError("rate limit hit"), "rate limit")],
)
def test_failures_degrade_to_an_explanatory_card(client, error, fragment):
    """Including the engine simply not being deployed -- the rest of the app is unaffected."""
    _with_service(_StubService(error=error))
    r = client.post("/fundamentals", data={"ticker": "AAPL", "period": "annual", "years": 10})
    assert r.status_code == 200
    assert fragment in r.text.lower()
    assert "<table" not in r.text
