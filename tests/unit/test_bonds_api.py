"""The Bond calendar tab: signed-in only, each person's own watchlist, and the calendar feed."""

from __future__ import annotations

import html
import json
import tempfile
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.bond_watchlists import BondWatchlistStore  # noqa: E402
from wheel_screener.api.users import UserStore  # noqa: E402
from wheel_screener.bonds import bond_calendar as bc  # noqa: E402
from wheel_screener.bonds.search import Bond, BondDirectory  # noqa: E402
from wheel_screener.bonds.service import BondCalendarService  # noqa: E402

SISTEMA_6 = Bond("RU000A0JXN21", "RU000A0JXN21", 'АФК "Система" ПАО БО-001P-06', "СистемБ1P6",
                 matdate=date(2027, 3, 26), coupon=10.5, currency="RUB")
SISTEMA_14 = Bond("RU000A10FA72", "RU000A10FA72", "АФК Система БО 002Р-14", "Систем2P14",
                  matdate=date(2030, 2, 1), coupon=18.0, currency="RUB")
OFZ = Bond("SU26238RMFS4", "RU000A1038V6", "ОФЗ-ПД 26238 15/05/2041", "ОФЗ 26238",
           matdate=date(2041, 5, 15), coupon=7.1, currency="RUB")


class _Moex:
    """Stands in for MOEX behind the watchlist's search and Add."""

    def __init__(self) -> None:
        self.down = False

    def load(self) -> list[Bond]:
        if self.down:
            raise OSError("timed out")
        return [SISTEMA_6, SISTEMA_14, OFZ]

    def search(self, q: str) -> list[Bond]:
        if self.down:
            raise OSError("timed out")
        return []

    def describe(self, code: str) -> Bond | None:
        if self.down:
            raise OSError("timed out")
        return None


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
    moex = c.moex = _Moex()
    app.state.bond_directory = BondDirectory(load=moex.load, moex_search=moex.search,
                                             describe=moex.describe)
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
    for path in ("/bonds/watchlist", "/bonds/export.csv", "/bonds/calendar.ics",
                 "/bonds/watchlist.csv", "/bonds/search?q=RU000A0JXN21"):
        assert client.get(path, follow_redirects=False).status_code == 303
    for path in ("/bonds/watchlist", "/bonds/watchlist/add", "/bonds/watchlist/remove",
                 "/bonds/watchlist/restore", "/bonds/watchlist/qty"):
        r = client.post(path, data={"text": "X1", "code": "RU000A0JXN21", "key": "X1"},
                        follow_redirects=False)
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
        assert r.status_code == 200 and "Imported 1 bond." in r.text
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
    assert r.status_code == 422 and "nothing changed" in r.text
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


SAVED = ('code,name,matdate,qty\r\n'
         'RU000A0JXN21,"АФК Система, 001P-06",2027-03-26,10\r\n'
         'SU26238RMFS4,,,\r\n')


def test_the_watchlist_is_a_bond_a_row_not_text(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, SAVED)
    page = client.get("/bonds/watchlist").text
    assert "<textarea" not in page and "2 bonds" in page
    assert page.count('hx-post="/bonds/watchlist/remove"') == 2
    assert 'aria-label="Remove АФК Система, 001P-06"' in page
    assert 'name="qty" value="10"' in page  # how many you hold, editable in place
    assert 'id="bond-q"' in page and 'hx-get="/bonds/search"' in page


def test_search_offers_add_and_marks_what_you_already_have(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, SAVED)
    r = client.get("/bonds/search", params={"q": "Система"})
    assert r.status_code == 200
    assert "АФК Система БО 002Р-14" in r.text and "matures 2030-02-01" in r.text
    assert "coupon 18%" in r.text
    assert r.text.count('hx-post="/bonds/watchlist/add"') == 1  # 001P-06 is listed already
    assert "in your watchlist" in r.text
    # the OFZ is listed by its MOEX code; found by ISIN, it is still yours
    assert "in your watchlist" in client.get("/bonds/search", params={"q": "RU000A1038V6"}).text
    assert "No bond matches" in client.get("/bonds/search", params={"q": "нет такой"}).text
    assert client.get("/bonds/search", params={"q": " "}).text.strip() == ""


def test_search_says_when_moex_is_down(client) -> None:
    _as(client)
    client.moex.down = True
    assert "MOEX did not answer" in client.get("/bonds/search", params={"q": "Система"}).text


def test_add_names_and_dates_the_bond_as_moex_has_it(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, SAVED)
    r = client.post("/bonds/watchlist/add", data={"code": "RU000A10FA72", "q": "Система"})
    assert r.status_code == 200 and "3 bonds" in r.text and 'class="bond-added"' in r.text
    saved = bc.parse_watchlist(app.state.bond_watchlists.text(sam.id))
    assert saved[-1] == {"code": "RU000A10FA72", "name": "АФК Система БО 002Р-14",
                         "matdate": date(2030, 2, 1), "qty": None}
    # the open search is re-rendered alongside, its Add now "in your watchlist"
    assert 'id="bond-search-results" hx-swap-oob="innerHTML"' in r.text
    assert 'hx-post="/bonds/watchlist/add"' not in r.text
    # the same bond again — under its other code too — is refused, and nothing changes
    before = app.state.bond_watchlists.text(sam.id)
    assert "already in your watchlist" in client.post(
        "/bonds/watchlist/add", data={"code": "RU000A1038V6"}).text
    assert "does not know" in client.post("/bonds/watchlist/add", data={"code": "XX1"}).text
    client.moex.down = True
    app.state.bond_directory = BondDirectory(load=client.moex.load,
                                             moex_search=client.moex.search,
                                             describe=client.moex.describe)
    assert "MOEX did not answer" in client.post(
        "/bonds/watchlist/add", data={"code": "RU000A10FA72"}).text
    assert app.state.bond_watchlists.text(sam.id) == before


def test_add_stops_at_the_limit(client) -> None:
    from wheel_screener.api.bond_watchlists import MAX_BONDS

    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, "".join(f"B{i}\n" for i in range(MAX_BONDS)))
    r = client.post("/bonds/watchlist/add", data={"code": "RU000A10FA72"})
    assert f"at most {MAX_BONDS} bonds" in r.text


def test_remove_then_undo_puts_the_bond_back_as_it_was(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, SAVED)
    r = client.post("/bonds/watchlist/remove", data={"key": "RU000A0JXN21"})
    assert r.status_code == 200 and "1 bond<" in r.text
    assert "Removed <b>АФК Система, 001P-06</b>" in r.text
    assert [it["code"] for it in bc.parse_watchlist(
        app.state.bond_watchlists.text(sam.id))] == ["SU26238RMFS4"]
    undo = r.text.split('hx-post="/bonds/watchlist/restore"', 1)[1]
    values = undo.split("hx-vals='", 1)[1].split("'", 1)[0]
    restored = client.post("/bonds/watchlist/restore", data=json.loads(html.unescape(values)))
    assert "2 bonds" in restored.text
    assert bc.parse_watchlist(app.state.bond_watchlists.text(sam.id))[-1] == {
        "code": "RU000A0JXN21", "name": "АФК Система, 001P-06", "matdate": date(2027, 3, 26),
        "qty": 10.0}
    # removing what is already gone (a second click) just shows the list
    client.post("/bonds/watchlist/remove", data={"key": "RU000A0JXN21"})
    r = client.post("/bonds/watchlist/remove", data={"key": "RU000A0JXN21"})
    assert r.status_code == 200 and "Removed" not in r.text and 'role="alert"' not in r.text


def test_qty_is_saved_from_the_row_and_answered_with_the_row(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, SAVED)
    r = client.post("/bonds/watchlist/qty", data={"key": "SU26238RMFS4", "qty": "2,5"})
    assert r.status_code == 200 and r.text.strip().startswith("<tr")
    assert 'value="2.5"' in r.text
    assert "SU26238RMFS4,,,2.5\r\n" in app.state.bond_watchlists.text(sam.id)
    for bad in ("lots", "-3"):
        r = client.post("/bonds/watchlist/qty", data={"key": "SU26238RMFS4", "qty": bad})
        assert 'aria-invalid="true"' in r.text and 'value="2.5"' in r.text  # unchanged
    client.post("/bonds/watchlist/qty", data={"key": "SU26238RMFS4", "qty": ""})
    assert "SU26238RMFS4,,,\r\n" in app.state.bond_watchlists.text(sam.id)
    # a bond removed meanwhile (another tab): an empty answer, which drops the row
    assert client.post("/bonds/watchlist/qty", data={"key": "GONE", "qty": "1"}).text == ""


def test_edits_touch_only_your_own_list(client) -> None:
    alex = _as(client, "Alex")
    app.state.bond_watchlists.save(alex.id, SAVED)
    _as(client, "Sam")
    client.post("/bonds/watchlist/remove", data={"key": "RU000A0JXN21"})
    client.post("/bonds/watchlist/qty", data={"key": "SU26238RMFS4", "qty": "7"})
    client.post("/bonds/watchlist/add", data={"code": "RU000A10FA72"})
    assert app.state.bond_watchlists.text(alex.id) == SAVED


def test_the_watchlist_downloads_as_the_scripts_csv(client) -> None:
    sam = _as(client)
    app.state.bond_watchlists.save(sam.id, SAVED)
    r = client.get("/bonds/watchlist.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.decode("utf-8-sig") == SAVED
