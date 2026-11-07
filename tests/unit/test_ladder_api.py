"""A holding's call ladder on the Portfolio tab: which holdings open one, the panel each opens,
priced against the chain; and the AI analysis tab, which holds its place."""

from __future__ import annotations

import html
import json
import re
import tempfile
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app, templates  # noqa: E402
from wheel_screener.api.deps import get_service  # noqa: E402
from wheel_screener.api.ladder_form import PlanError, form_values, plan_from_form  # noqa: E402
from wheel_screener.api.users import UserStore  # noqa: E402
from wheel_screener.core import ladder as L  # noqa: E402
from wheel_screener.core.errors import ProviderUnavailableError  # noqa: E402
from wheel_screener.core.models import (  # noqa: E402
    BrokerageAccount,
    OptionContract,
    OptionType,
    Position,
    PositionKind,
)

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


# what the TER row sends: the broker's 100 shares at $390, and the $340 call sold for $6
TER = {"symbol": "TER", "shares": "100", "cost": "390", "call_strike": "340",
       "call_expiry": EXPIRY.isoformat(), "sold_for": "6"}


def _client(service: _Service | None = None) -> TestClient:
    c = TestClient(app)
    c.__enter__()
    app.state.settings.portfolio.cookie_secure = False  # TestClient speaks http
    app.state.users = UserStore(tempfile.mkdtemp() + "/users.sqlite")
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


def _panel(c: TestClient, **changes) -> str:
    r = c.get("/portfolio/ladder", params={**TER, **changes})
    assert r.status_code == 200, r.text
    return html.unescape(r.text)


# --- the panel ---------------------------------------------------------------------------------

def test_the_panel_needs_a_sign_in(client) -> None:
    r = client.get("/portfolio/ladder", params=TER, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/portfolio")
    r = client.get("/portfolio/ladder", params=TER, headers={"HX-Request": "true"},
                   follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login?next=/portfolio"
    assert client.service.asked == []


def test_the_call_ladder_tab_is_gone(client) -> None:
    _as(client)
    assert client.get("/ladder", follow_redirects=False).status_code == 404
    assert 'href="/ladder"' not in client.get("/analysis").text


def test_a_holding_s_ladder_shows_the_orders_to_place(client) -> None:
    _as(client)
    page = _panel(client)
    assert client.service.asked == ["TER"]
    assert page.lstrip().startswith('<tr class="detail exits-row ladder-row">')
    assert "<b>Hold.</b>" in page
    assert L.leg("BUY TO CLOSE", 1, "TER", EXPIRY, 340) + "\nLIMIT 1.50" in page
    assert "WHEN TER LAST ≥ 329.80" in page
    assert L.leg("SELL TO OPEN", 1, "TER", NEXT, 355) in page
    assert "NET CREDIT LIMIT" in page and "One cancels other" in page
    assert "$384.00" in page  # real cost: $390 less the $6.00 call
    assert "and the last expiry before the report." in page  # where the roll lands
    # the rungs, planned from the chain: from the open call up to the cost
    assert re.findall(r"<td><b>\$(\d+)</b>", page) == ["340", "355", "375", "390"]
    assert "3 rolls to the target" in page
    # the plan: 1% a month unless told, $340 calls sold again and again to start with
    assert "if TER grows 1% a month, steadily" in page and "Sell the $340 call," in page
    # each panel's tickets have their own ids, so two open panels don't copy each other's
    ids = re.findall(r'data-copy="(order-[a-z]+-[0-9a-f]+)"', page)
    assert len(ids) == 2 and all(f'id="{i}"' in page for i in ids)


def test_adjusting_plans_again_without_saving_anything(client) -> None:
    _as(client)
    page = _panel(client, premiums="8.40", growth="2.5%")
    assert "$375.60" in page  # $390 less $8.40 and the $6.00 call
    assert "if TER grows 2.5% a month, steadily" in page and 'value="2.5%"' in page
    # the Adjust form re-plans this same row, carrying the broker's call along
    assert 'hx-get="/portfolio/ladder" hx-target="closest tr" hx-swap="outerHTML"' in page
    assert f'name="call_expiry" value="{EXPIRY.isoformat()}"' in page


def test_a_call_sold_too_long_ago_to_read_asks_what_it_sold_for(client) -> None:
    _as(client)
    page = _panel(client, sold_for="")
    assert "enter what it sold for under Adjust" in page
    assert "Take profit <small>" not in page and "WHEN TER LAST" in page  # the roll still rests
    assert 'name="sold_for"' in page


def test_with_no_call_open_it_says_what_to_sell(client) -> None:
    _as(client)
    page = _panel(client, call_strike="", call_expiry="", sold_for="")
    assert "No call open." in page and "SELL TO OPEN 1 TER" in page
    assert 'name="sold_for"' not in page  # no call to have sold


@pytest.mark.parametrize("changes, said", [
    ({"shares": "50"}, "100 shares or more"),
    ({"call_expiry": ""}, "both its strike and its expiry"),
    ({"growth": "40"}, "between -10% and 10% a month"),
    ({"cost": "abc"}, "should be a number"),
])
def test_numbers_that_can_not_be_priced_are_said_in_the_panel(client, changes, said) -> None:
    _as(client)
    page = _panel(client, **changes)
    assert said in page and "<details class=\"advanced\" open>" in page
    assert client.service.asked == []


def test_a_chain_that_does_not_load_is_a_message_in_the_panel() -> None:
    c = _client(_Service(ProviderUnavailableError("schwab chain fetch failed for TER")))
    try:
        _as(c)
        page = _panel(c)
        assert "chain didn't load" in page and "schwab chain fetch failed" in page
        assert 'name="cost" inputmode="decimal" value="390"' in page  # still adjustable
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


# --- which holdings open one -------------------------------------------------------------------

def _shares(symbol="TER", quantity=100, cost=390.0, price=269.28) -> Position:
    return Position(symbol=symbol, underlying=symbol, kind=PositionKind.SHARES,
                    quantity=quantity, asset_type="EQUITY", market_value=price * quantity,
                    average_price=cost)


def _call(symbol="TER", strike=340.0, expiry=EXPIRY, sold=6.0) -> Position:
    return Position(symbol=f"{symbol} {expiry:%y%m%d}C{strike:g}", underlying=symbol,
                    kind=PositionKind.SHORT_CALL, quantity=1, option_type=OptionType.CALL,
                    strike=strike, expiration=expiry, dte=(expiry - TODAY).days,
                    opening_price=sold, market_value=-600.0)


def _rendered(*positions: Position) -> str:
    account = BrokerageAccount(broker="snaptrade", account_id="a1",
                               display_name="Fidelity ••••1111", positions=list(positions))
    return html.unescape(templates.get_template("_positions.html").render(
        accounts=[account], swaps={}, balances_error=None))


def test_a_holding_with_its_call_below_cost_opens_its_ladder() -> None:
    page = _rendered(_shares(), _call(), _shares("KO", cost=60.0, price=70.0))
    rows = re.findall(r"<tr class=\"row-open\" hx-get=\"/portfolio/ladder\".*?hx-vals='(.*?)'",
                      page, re.S)
    assert [json.loads(v) for v in rows] == [{**TER, "premiums": "", "growth": ""}]
    assert "call below cost</span>" in page
    assert ">below cost</span>" in page  # and the call itself says so, in Open options


def test_a_holding_under_its_cost_with_no_call_open_opens_its_ladder() -> None:
    page = _rendered(_shares())
    assert 'hx-get="/portfolio/ladder"' in page and ">below cost</span>" in page


@pytest.mark.parametrize("positions", [
    [_shares(), _call(strike=390.0)],  # the call is at the cost already
    [_shares(price=400.0)],  # no call, and the shares are past the cost
    [_shares(quantity=50)],  # too few for a call
    [_shares(cost=None)],  # no cost reported
])
def test_a_holding_that_needs_no_ladder_opens_none(positions) -> None:
    page = _rendered(*positions)
    assert 'hx-get="/portfolio/ladder"' not in page and "below cost" not in page


def test_the_row_sends_what_the_panel_reads() -> None:
    plan = L.for_holding(_shares(), [_shares(), _call()])
    assert plan_from_form(form_values(plan)) == plan


# --- reading the numbers -----------------------------------------------------------------------

def test_prices_are_read_as_people_type_them() -> None:
    plan = plan_from_form({**TER, "cost": "$390.00", "sold_for": "6,00", "premiums": "8,4"})
    assert (plan.cost, plan.call_sold_for, plan.call_premiums, plan.premiums) == \
        (390.0, 6.0, 6.0, 8.4)
    assert plan.rungs == ()  # for the chain to plan
    assert plan_from_form({**TER, "call_strike": "", "call_expiry": ""}).call_sold_for is None
    with pytest.raises(PlanError, match="ticker"):
        plan_from_form({**TER, "symbol": "not a ticker"})


def test_the_ai_analysis_tab_holds_its_place(client) -> None:
    r = client.get("/analysis")
    assert r.status_code == 200
    assert "<h2>AI analysis</h2>" in r.text
    assert 'aria-current="page">AI analysis' in r.text


def test_static_links_change_with_what_they_link_to(tmp_path) -> None:
    from wheel_screener import __version__
    from wheel_screener.api.app import _asset_version
    (tmp_path / "a.css").write_text("a { }")
    before = _asset_version(tmp_path)
    assert before.startswith(__version__ + ".")
    (tmp_path / "a.css").write_text("a { color: red }")
    assert _asset_version(tmp_path) != before
    assert templates.env.globals["asset_v"].startswith(__version__ + ".")
