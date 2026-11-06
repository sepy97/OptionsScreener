"""The Call ladder tab: signed in only, each person's own ladders, priced against the chain; and
the AI analysis tab, which holds its place."""

from __future__ import annotations

import html
import tempfile
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.deps import get_service  # noqa: E402
from wheel_screener.api.ladder_plans import (  # noqa: E402
    LadderPlanStore,
    PlanError,
    plan_from_form,
)
from wheel_screener.api.users import UserStore  # noqa: E402
from wheel_screener.core import ladder as L  # noqa: E402
from wheel_screener.core.errors import ProviderUnavailableError  # noqa: E402
from wheel_screener.core.models import OptionContract, OptionType  # noqa: E402

TODAY = date.today()
EXPIRY = TODAY + timedelta(days=42)
NEXT = EXPIRY + timedelta(days=28)
REPORT = NEXT + timedelta(days=12)


class _Service:
    """Stands in for the chain behind a ladder: TER at $269.28, priced by the model."""

    carry_rate = 0.04

    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.asked = error, []

    def ladder_market(self, symbol: str, today: date) -> L.Market:
        self.asked.append(symbol)
        if self.error is not None:
            raise self.error
        calls = []
        for e in (TODAY + timedelta(days=14), EXPIRY, NEXT, REPORT + timedelta(days=21)):
            for k in range(250, 455, 5):
                mid = L.call_value(269.28, k, (e - today).days, 0.68)
                calls.append(OptionContract(
                    underlying_symbol=symbol, option_symbol=f"{symbol}{e:%y%m%d}C{k}",
                    option_type=OptionType.CALL, expiration=e, strike=k, dte=(e - today).days,
                    bid=max(0.0, mid - 0.3), ask=mid + 0.3, mid=mid, implied_volatility=0.68))
        return L.Market(spot=269.28, calls=calls, next_earnings=REPORT)


TER = {"symbol": "ter", "shares": "100", "cost": "390", "put_premium": "0",
       "call_premiums": "6,00", "rungs": "340, 355, 370, 390", "call_strike": "340",
       "call_expiry": EXPIRY.isoformat(), "call_sold_for": "$6.00"}


def _client(service: _Service | None = None) -> TestClient:
    c = TestClient(app)
    c.__enter__()
    app.state.settings.portfolio.cookie_secure = False  # TestClient speaks http
    tmp = tempfile.mkdtemp()
    app.state.users = UserStore(tmp + "/users.sqlite")
    app.state.ladder_plans = LadderPlanStore(tmp + "/jobs.sqlite")
    c.service = service or _Service()
    app.dependency_overrides[get_service] = lambda: c.service
    return c


def _as(c: TestClient, name: str = "Sam"):
    user = app.state.users.create_user(name)
    token, _ = app.state.users.create_session(user.id, timedelta(days=1))
    c.cookies.set(app.state.settings.portfolio.cookie_name, token)
    return user


@pytest.fixture
def client():
    c = _client()
    yield c
    app.dependency_overrides.clear()
    c.__exit__(None, None, None)


def _create(c: TestClient, **changes) -> str:
    r = c.post("/ladder", data={**TER, **changes}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def test_the_tab_needs_a_sign_in(client) -> None:
    r = client.get("/ladder", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/ladder"
    r = client.get("/ladder/abc", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login?next=/ladder"
    assert client.post("/ladder", data=TER, follow_redirects=False).status_code == 303
    assert client.service.asked == []


def test_with_no_ladder_yet_the_tab_offers_to_start_one(client) -> None:
    _as(client)
    r = client.get("/ladder")
    assert r.status_code == 200
    assert "Start your first ladder" in r.text and 'aria-current="page">Call ladder' in r.text


def test_a_new_ladder_shows_the_orders_to_place(client) -> None:
    _as(client)
    where = _create(client)
    page = html.unescape(client.get(where).text)
    assert client.service.asked == ["TER"]
    assert "<b>Hold.</b>" in page
    assert L.leg("BUY TO CLOSE", 1, "TER", EXPIRY, 340) + "\nLIMIT 1.50" in page
    assert "WHEN TER LAST ≥ 329.80" in page
    assert L.leg("SELL TO OPEN", 1, "TER", NEXT, 355) in page
    assert "NET CREDIT LIMIT" in page and "One cancels other" in page
    assert "$384.00" in page  # real cost: $390 less the $6.00 call
    assert "Last expiry before the report." in page
    assert 'data-copy="order-roll"' in page and "/static/copy.js" in page


def test_typos_are_said_and_nothing_is_saved(client) -> None:
    _as(client)
    r = client.post("/ladder", data={**TER, "shares": "50"})
    assert r.status_code == 422 and "100 shares or more" in r.text
    r = client.post("/ladder", data={**TER, "call_expiry": ""})
    assert r.status_code == 422 and "both its strike and its expiry" in r.text
    assert 'value="340, 355, 370, 390"' in r.text  # what was typed comes back
    assert app.state.ladder_plans.plans(_user_id(client)) == []


def _user_id(c: TestClient) -> str:
    token = c.cookies.get(app.state.settings.portfolio.cookie_name)
    return app.state.users.session(token).user.id


def test_one_ladder_per_ticker(client) -> None:
    _as(client)
    _create(client)
    r = client.post("/ladder", data={**TER, "symbol": "TER"})
    assert r.status_code == 422 and "already have a ladder for TER" in r.text


def test_someone_else_s_ladder_is_not_found(client) -> None:
    _as(client, "Sam")
    where = _create(client)
    _as(client, "Alex")
    assert client.get(where).status_code == 404
    assert client.post(where, data=TER).status_code == 404
    assert client.post(where + "/delete").status_code == 404
    assert client.service.asked == []  # none of Alex's requests reached the chain


def test_editing_the_numbers_after_a_roll(client) -> None:
    _as(client)
    where = _create(client)
    r = client.post(where, data={**TER, "call_strike": "355", "call_expiry": NEXT.isoformat(),
                                 "call_sold_for": "9.10", "call_premiums": "9.15"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == where
    page = html.unescape(client.get(where).text)
    assert "$380.85" in page  # $390 less $9.15
    # the new call expires on the last expiry before the report, so it can't roll
    assert "No roll on this call." in page
    r = client.post(where, data={**TER, "rungs": ""})
    assert r.status_code == 422 and "Enter the rungs" in r.text


def test_deleting_a_ladder(client) -> None:
    _as(client)
    where = _create(client)
    r = client.post(where + "/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ladder"
    assert client.get(where).status_code == 404
    assert "Start your first ladder" in client.get("/ladder").text


def test_a_chain_that_does_not_load_is_a_message_on_the_page() -> None:
    c = _client(_Service(ProviderUnavailableError("schwab chain fetch failed for TER")))
    try:
        _as(c)
        where = _create(c)
        r = c.get(where)
        assert r.status_code == 200
        assert "chain didn't load" in r.text and "schwab chain fetch failed" in r.text
        assert 'name="call_strike"' in r.text  # the numbers can still be edited
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_ai_analysis_tab_holds_its_place(client) -> None:
    r = client.get("/analysis")
    assert r.status_code == 200
    assert "<h2>AI analysis</h2>" in r.text
    assert 'aria-current="page">AI analysis' in r.text
    assert 'href="/ladder"' in r.text  # the nav carries both new tabs


@pytest.mark.parametrize("raw, rungs", [
    ("340, 355, 370, 390", (340.0, 355.0, 370.0, 390.0)),
    ("390 340;355  370", (340.0, 355.0, 370.0, 390.0)),
    ("30.5,32,35", (30.5, 32.0, 35.0)),
])
def test_rungs_are_read_however_they_are_typed(raw, rungs) -> None:
    assert plan_from_form({**TER, "rungs": raw}).rungs == rungs


def test_a_plan_reads_prices_as_people_type_them() -> None:
    plan = plan_from_form(TER)
    assert (plan.symbol, plan.call_premiums, plan.call_sold_for) == ("TER", 6.0, 6.0)
    assert plan_from_form({**TER, "call_strike": "", "call_expiry": ""}).call_sold_for is None
    with pytest.raises(PlanError, match="should be a number"):
        plan_from_form({**TER, "cost": "abc"})
    with pytest.raises(PlanError, match="ticker"):
        plan_from_form({**TER, "symbol": "not a ticker"})


def test_the_store_keeps_each_person_s_plans_apart(tmp_path) -> None:
    store = LadderPlanStore(str(tmp_path / "jobs.sqlite"))
    plan = plan_from_form(TER)
    saved = store.create("u1", plan)
    assert store.plans("u2") == [] and store.plan("u2", saved.id) is None
    store.delete("u2", saved.id)  # not theirs: nothing happens
    again = store.plan("u1", saved.id)
    assert again.plan == plan
    store.create("u2", plan)  # one per ticker is per person
