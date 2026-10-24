"""The Bond calendar tab: signed-in only, each person's own watchlist, and the calendar feed."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.bond_watchlists import BondWatchlistStore  # noqa: E402
from wheel_screener.api.users import UserStore  # noqa: E402
from wheel_screener.bonds import bond_calendar as bc  # noqa: E402
from wheel_screener.bonds.service import BondCalendarService  # noqa: E402


def _event(day: date, kind: str = bc.COUPON, qty: float | None = None, **kw) -> dict:
    amount = kw.pop("amount", 35.78)
    e = {"date": day, "kind": kind, "name": "АФК Система, 001P-06", "isin": "RU000A0JXN21",
         "secid": "RU000A0JXN21", "amount": amount, "cur": "RUB", "rate": 14.35, "qty": qty,
         "total": amount * qty if amount is not None and qty else None,
         "note": "record date 2027-01-09", "remind": None}
    return {**e, **kw}


class _Builds:
    """Stands in for the MOEX build: one coupon per bond in the list, named after its code."""

    def __init__(self, events: list[dict] | None = None, problems: list[str] | None = None):
        self.events, self.problems = events, problems or []
        self.calls: list[tuple] = []

    def __call__(self, items, date_from, date_to, *, workers, log) -> bc.CalendarRun:
        self.calls.append((tuple(it["code"] for it in items), date_from, date_to))
        evs = self.events if self.events is not None else [
            _event(date.today(), name=it["code"], isin=it["code"]) for it in items]
        evs = [e for e in evs if date_from is None or e["date"] >= date_from]
        return bc.CalendarRun(evs, list(self.problems), ["Skipping 1 already matured bonds."])


def _client(builds: _Builds | None = None, resolve=None):
    """A client over the real app with its OWN user + watchlist stores (not the repo's data/)."""
    c = TestClient(app)
    c.__enter__()
    app.state.settings.portfolio.cookie_secure = False  # TestClient speaks http
    tmp = tempfile.mkdtemp()
    app.state.users = UserStore(tmp + "/users.sqlite")
    app.state.bond_watchlists = BondWatchlistStore(tmp + "/jobs.sqlite")
    app.state.bonds = BondCalendarService(
        build=builds or _Builds(), resolve=resolve or (lambda items, log, pmap=map: []))
    return c


def _as(c, name="Sam", *, admin=False):
    """Sign `c` in as a new user, straight through the store. Returns the user."""
    user = app.state.users.create_user(name, is_admin=admin)
    _session_for(c, user)
    return user


def _session_for(c, user) -> None:
    token, _ = app.state.users.create_session(user.id, timedelta(days=1))
    c.cookies.set(app.state.settings.portfolio.cookie_name, token)


@pytest.fixture
def client():
    c = _client()
    yield c
    c.__exit__(None, None, None)


def test_signed_out_visitors_are_sent_to_sign_in_and_back(client) -> None:
    r = client.get("/bonds?all=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/bonds%3Fall%3D1"
    r = client.get("/bonds/events", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login?next=/bonds"
    for path in ("/bonds/watchlist", "/bonds/export.csv", "/bonds/calendar.ics"):
        assert client.get(path, follow_redirects=False).status_code == 303
    r = client.post("/bonds/watchlist", data={"text": "X1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/bonds"
    # the sign-in page it lands on lights up the Bond calendar tab, not Portfolio
    login = client.get("/login?next=/bonds")
    assert 'href="/bonds" class="nav-tab active"' in login.text


def test_the_tab_shows_your_list_and_lazy_loads_the_default_year(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, "RU000A0JXN21\n")
    r = client.get("/bonds")
    assert r.status_code == 200
    assert 'href="/bonds" class="nav-tab active"' in r.text and "1 bond in your watchlist" in r.text
    today = date.today()
    q = f"from={today.isoformat()}&amp;to={bc.one_year_after(today).isoformat()}"
    assert f'hx-get="/bonds/events?{q}"' in r.text


def test_everyone_sees_only_their_own_calendar(client) -> None:
    sam = _as(client, "Sam")
    app.state.bond_watchlists.save(sam.id, "SAMBOND\n")
    assert "SAMBOND" in client.get("/bonds/events").text
    alex = _as(client, "Alex")
    app.state.bond_watchlists.save(alex.id, "ALEXBOND\n")
    body = client.get("/bonds/events").text
    assert "ALEXBOND" in body and "SAMBOND" not in body
    assert "ALEXBOND" in client.get("/bonds/export.csv").content.decode("utf-8-sig")


def test_an_empty_watchlist_says_so_without_building(client) -> None:
    _as(client)
    r = client.get("/bonds/events")
    assert "Your watchlist is empty" in r.text and app.state.bonds._build.calls == []


def test_events_group_by_month_with_problems_notes_and_totals() -> None:
    today = date.today()
    later = bc.one_year_after(today).replace(day=1)
    c = _client(_Builds([_event(today, qty=10),
                         _event(later, bc.OFFER_LAST_DAY, amount=None, note="check")],
                        problems=["ГТЛК, 001P-04: not found on MOEX"]))
    try:
        sam = _as(c)
        app.state.bond_watchlists.save(sam.id, "RU000A0JXN21 10\n")
        r = c.get("/bonds/events")
        assert "<strong>2 events</strong>" in r.text
        assert today.strftime("%B %Y") in r.text and later.strftime("%B %Y") in r.text
        assert 'class="ev ev-deadline"' in r.text and 'class="soon"' in r.text
        assert "Could not process 1" in r.text and "not found on MOEX" in r.text
        assert "Skipping 1 already matured bonds." in r.text
        assert ">Total</th>" in r.text and "357.80 RUB" in r.text
        assert "Expected payments by month" in r.text
    finally:
        c.__exit__(None, None, None)


def test_period_flags_reach_the_build() -> None:
    builds = _Builds([_event(date(2027, 3, 1)), _event(date(2028, 3, 1))])
    c = _client(builds)
    try:
        sam = _as(c)
        app.state.bond_watchlists.save(sam.id, "B1\n")
        assert "the whole calendar" in c.get("/bonds/events?all=1").text
        assert builds.calls[-1][1:] == (None, None)
        r = c.get("/bonds/events?from=2027-12-31&to=2027-01-01")  # swapped: normalized
        assert builds.calls[-1][1:] == (date(2027, 1, 1), None)  # end windowed after the build
        assert "<strong>1 event</strong>" in r.text and "2027-01-01 to 2027-12-31" in r.text
    finally:
        c.__exit__(None, None, None)


def test_csv_and_ics_downloads_are_the_scripts_formats(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, "RU000A0JXN21\n")
    r = client.get("/bonds/export.csv?all=1")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith("﻿Date,Event,Bond".encode())  # utf-8-sig, as the script
    assert "35,78" in client.get("/bonds/export.csv?all=1&sep=%3B").content.decode("utf-8-sig")
    r = client.get("/bonds/calendar.ics?all=1")
    assert r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in r.text and "X-WR-CALNAME:Bonds" in r.text


def test_saving_resolves_names_into_your_list_only() -> None:
    def resolve(items, log, pmap=map):
        for it in items:
            it["code"] = it["code"] or "RU000A0JXN21"
            log(f"  {it['name']}  →  {it['code']}")
        return []

    c = _client(resolve=resolve)
    try:
        alex = _as(c, "Alex")
        app.state.bond_watchlists.save(alex.id, "ALEXBOND\n")
        sam = _as(c, "Sam")
        page = c.get("/bonds/watchlist")
        assert page.status_code == 200 and "ALEXBOND" not in page.text
        r = c.post("/bonds/watchlist",
                   data={"text": 'name,matdate\n"АФК Система, 001P-06",2027-03-26\n'})
        assert r.status_code == 200 and "Saved 1 bond." in r.text
        assert "АФК Система, 001P-06  →  RU000A0JXN21" in r.text  # to check the match
        assert app.state.bond_watchlists.text(sam.id).splitlines()[1].startswith("RU000A0JXN21,")
        assert app.state.bond_watchlists.text(alex.id) == "ALEXBOND\n"
    finally:
        c.__exit__(None, None, None)


def test_saving_refuses_empty_and_oversized_lists(client) -> None:
    from wheel_screener.api.bond_watchlists import MAX_BONDS

    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, "RU000A0JXN21\n")
    r = client.post("/bonds/watchlist", data={"text": "# nothing\n"})
    assert r.status_code == 422 and "nothing saved" in r.text
    many = "".join(f"B{i}\n" for i in range(MAX_BONDS + 1))
    r = client.post("/bonds/watchlist", data={"text": many})
    assert r.status_code == 422 and f"at most {MAX_BONDS}" in r.text
    assert app.state.bond_watchlists.text(sam.id) == "RU000A0JXN21\n"  # untouched


def test_an_upload_wins_over_the_text_and_reads_windows_1251(client) -> None:
    sam = _as(client)
    raw = "code;name\nRU000A0ZYQY7;АФК Система, 001P-07\n".encode("cp1251")
    r = client.post("/bonds/watchlist", data={"text": "IGNORED\n"},
                    files={"file": ("watchlist.csv", raw, "text/csv")})
    assert r.status_code == 200
    saved = bc.parse_watchlist(app.state.bond_watchlists.text(sam.id))
    assert [(it["code"], it["name"]) for it in saved] == [("RU000A0ZYQY7", "АФК Система, 001P-07")]


def test_the_feed_link_is_shown_once_reads_without_signing_in_and_can_be_replaced() -> None:
    c = _client()
    try:
        sam = _as(c)
        app.state.bond_watchlists.save(sam.id, "SAMBOND\n")
        r = c.post("/bonds/feed")
        assert r.status_code == 200 and "shown only once" in r.text
        origin = app.state.settings.passkeys.origin.rstrip("/")
        link = r.text.split(f'value="{origin}', 1)[1].split('"', 1)[0]
        assert link.startswith("/feeds/bonds/") and link.endswith(".ics")
        assert f'href="webcal://{origin.split("://", 1)[1]}{link}"' in r.text
        c.cookies.clear()  # a calendar app has no session
        feed = c.get(link)
        assert feed.status_code == 200 and "SAMBOND" in feed.text
        assert "BEGIN:VCALENDAR" in feed.text
        _session_for(c, sam)  # back in the browser: the button now offers a NEW link
        assert "New feed link" in c.get("/bonds/events").text
        c.post("/bonds/feed")
        c.cookies.clear()
        assert c.get(link).status_code == 404  # the old link is off
        assert c.get("/feeds/bonds/guess.ics").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_removing_someone_removes_their_watchlist_and_feed() -> None:
    c = _client()
    try:
        alex = _as(c, "Alex")
        app.state.bond_watchlists.save(alex.id, "ALEXBOND\n")
        link = app.state.bond_watchlists.new_feed(alex.id)
        _as(c, "Sam", admin=True)
        assert "Alex no longer has access" in c.post(
            "/admin/people/remove", data={"user_id": alex.id}).text
        assert app.state.bond_watchlists.text(alex.id) == ""
        assert app.state.bond_watchlists.text_for_feed(link) is None
    finally:
        c.__exit__(None, None, None)
