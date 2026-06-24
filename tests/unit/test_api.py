from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from datetime import date  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.deps import get_service, get_settings  # noqa: E402
from wheel_screener.config import Settings  # noqa: E402
from wheel_screener.core.errors import AuthExpiredError, RateLimitedError  # noqa: E402
from wheel_screener.core.models import (  # noqa: E402
    CandidateResult,
    OptionContract,
    OptionType,
)


class _FakeFundamentals:
    def known_symbols(self) -> set[str]:
        return {"AAA"}


class _FakeService:
    """Stands in for ScreenerService via dependency_overrides — no Schwab/FMP."""

    def __init__(self, result: list | None = None, error: Exception | None = None) -> None:
        self.fundamentals = _FakeFundamentals()
        self._result = result if result is not None else []
        self._error = error

    def run_screen(self, criteria, today, *, cancel=None):
        if self._error is not None:
            raise self._error
        return self._result


def _candidate() -> CandidateResult:
    contract = OptionContract(
        underlying_symbol="AAA", option_symbol="AAA80P", option_type=OptionType.PUT,
        expiration=date(2026, 8, 15), strike=80.0, dte=40, bid=1.0, ask=1.1,
        raw={"mark": 1.05},
    )
    return CandidateResult(
        symbol="AAA", contract=contract, annualized_yield=0.2, premium=1.0,
        collateral=8000.0, score=0.5,
    )


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _client(service: _FakeService, settings: Settings | None = None) -> TestClient:
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_settings] = lambda: settings or Settings()
    return TestClient(app)


def test_screen_returns_candidates_with_clean_contract() -> None:
    client = _client(_FakeService(result=[_candidate()]))
    r = client.post("/screen", json={})
    assert r.status_code == 200  # regression: the missing-`today` arg used to 500 here
    body = r.json()
    assert len(body) == 1 and body[0]["symbol"] == "AAA"
    contract = body[0]["contract"]
    assert "spread_pct" in contract and "raw" not in contract  # M3.0 contract holds on the wire


def test_screen_maps_auth_error_to_401() -> None:
    client = _client(_FakeService(error=AuthExpiredError("token gone")))
    r = client.post("/screen", json={})
    assert r.status_code == 401 and r.json()["error"] == "AuthExpiredError"


def test_screen_maps_rate_limit_to_429_with_retry_after() -> None:
    client = _client(_FakeService(error=RateLimitedError("slow down")))
    r = client.post("/screen", json={})
    assert r.status_code == 429 and r.headers.get("Retry-After") == "60"


def test_health_reports_store_and_token(tmp_path) -> None:
    settings = Settings()
    token = tmp_path / "token.json"
    token.write_text("{}")
    settings.schwab.token_path = str(token)
    r = _client(_FakeService(), settings=settings).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "store_loaded": True, "schwab_token": True}


def test_health_degraded_without_token(tmp_path) -> None:
    settings = Settings()
    settings.schwab.token_path = str(tmp_path / "missing.json")
    body = _client(_FakeService(), settings=settings).get("/health").json()
    assert body["schwab_token"] is False and body["status"] == "degraded"
