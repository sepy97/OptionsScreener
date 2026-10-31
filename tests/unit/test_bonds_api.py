"""The Bond calendar tab: signed-in only, each person's own portfolios, and the calendar feed."""

from __future__ import annotations

import csv
import html
import io
import json
import tempfile
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.bond_portfolios import MAX_BONDS, BondPortfolioStore  # noqa: E402
from wheel_screener.api.users import UserStore  # noqa: E402
from wheel_screener.bonds import bond_calendar as bc  # noqa: E402
from wheel_screener.bonds import spreadsheet as sheet  # noqa: E402
from wheel_screener.bonds.reports import Workbook  # noqa: E402
from wheel_screener.bonds.search import Bond, BondDirectory  # noqa: E402
from wheel_screener.bonds.service import BondCalendarService  # noqa: E402

SISTEMA_6 = Bond("RU000A0JXN21", "RU000A0JXN21", 'АФК "Система" ПАО БО-001P-06', "СистемБ1P6",
                 matdate=date(2027, 3, 26), coupon=10.5, currency="RUB")
SISTEMA_14 = Bond("RU000A10FA72", "RU000A10FA72", "АФК Система БО 002Р-14", "Систем2P14",
                  matdate=date(2030, 2, 1), coupon=18.0, currency="RUB")
OFZ = Bond("SU26238RMFS4", "RU000A1038V6", "ОФЗ-ПД 26238 15/05/2041", "ОФЗ 26238",
           matdate=date(2041, 5, 15), coupon=7.1, currency="RUB")


class _Moex:
    """Stands in for MOEX behind a portfolio's search and Add."""

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
            _event(date.today(), name=it["code"], isin=it["code"], secid=it["code"])
            for it in items]
        evs = [e for e in evs if date_from is None or e["date"] >= date_from]
        return bc.CalendarRun(evs, list(self.problems), ["(the build's own notes)"])


def _client(builds: _Builds | None = None, resolve=None):
    """A client over the real app with its OWN user + portfolio stores (not the repo's data/)."""
    c = TestClient(app)
    c.__enter__()
    app.state.settings.portfolio.cookie_secure = False  # TestClient speaks http
    tmp = tempfile.mkdtemp()
    app.state.users = UserStore(tmp + "/users.sqlite")
    app.state.bond_portfolios = BondPortfolioStore(tmp + "/jobs.sqlite")
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


def _store() -> BondPortfolioStore:
    return app.state.bond_portfolios


def _portfolio(user, name="Broker", *bonds) -> str:
    """A portfolio of `user` holding `bonds` — (code, name, matdate, qty) each. Returns its id."""
    pid = _store().create(user.id, name).id
    for code, bond_name, matdate, qty in bonds:
        _store().add(user.id, pid, {"code": code, "name": bond_name, "matdate": matdate,
                                    "qty": qty})
    return pid


SYS6 = ("RU000A0JXN21", "АФК Система, 001P-06", date(2027, 3, 26), 10.0)
OFZ_HELD = ("SU26238RMFS4", "", None, 2.0)


def test_signed_out_visitors_are_sent_to_sign_in_and_back(client) -> None:
    r = client.get("/bonds?all=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/bonds%3Fall%3D1"
    r = client.get("/bonds/events", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login?next=/bonds"
    for path in ("/bonds/portfolios", "/bonds/portfolios/x", "/bonds/export.csv",
                 "/bonds/export.xlsx", "/bonds/grouped_export.xlsx",
                 "/bonds/calendar.ics", "/bonds/portfolios/x/holdings.csv",
                 "/bonds/search?q=RU000A0JXN21&portfolio=x"):
        assert client.get(path, follow_redirects=False).status_code == 303
    for path in ("/bonds/portfolios", "/bonds/portfolios/x/add", "/bonds/portfolios/x/remove",
                 "/bonds/portfolios/x/restore", "/bonds/portfolios/x/qty",
                 "/bonds/portfolios/x/import", "/bonds/portfolios/x/rename",
                 "/bonds/portfolios/x/delete", "/bonds/feed"):
        r = client.post(path, data={"name": "P", "code": "RU000A0JXN21", "key": "X1"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/bonds"
    # the sign-in page it lands on lights up the Bond calendar tab, not Portfolio
    login = client.get("/login?next=/bonds")
    assert 'href="/bonds" class="nav-tab active"' in login.text


def test_the_old_watchlist_link_goes_to_portfolios(client) -> None:
    _as(client)
    r = client.get("/bonds/watchlist", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/bonds/portfolios"


def test_with_no_portfolio_you_are_asked_to_name_one(client) -> None:
    _as(client)
    page = client.get("/bonds/portfolios")
    assert page.status_code == 200 and "Name your first portfolio" in page.text
    r = client.post("/bonds/portfolios", data={"name": "Broker"}, follow_redirects=False)
    assert r.status_code == 303
    pid = r.headers["location"].rsplit("/", 1)[1]
    assert client.get("/bonds/portfolios", follow_redirects=False).headers["location"] == (
        f"/bonds/portfolios/{pid}")
    page = client.get(f"/bonds/portfolios/{pid}").text
    assert "No bonds in Broker yet" in page and 'id="bond-add-qty"' in page
    r = client.post("/bonds/portfolios", data={"name": "broker"})
    assert r.status_code == 422 and "already have a portfolio called broker" in r.text


def test_rename_and_delete(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6)
    r = client.post(f"/bonds/portfolios/{pid}/rename", data={"name": "Тинькофф"},
                    follow_redirects=False)
    assert r.status_code == 303 and _store().portfolios(sam.id)[0].name == "Тинькофф"
    _portfolio(sam, "IIS")
    r = client.post(f"/bonds/portfolios/{pid}/rename", data={"name": "iis"})
    assert r.status_code == 422 and "already have" in r.text
    page = client.get(f"/bonds/portfolios/{pid}").text
    assert json.dumps("Delete Тинькофф and the 1 bond in it?") in html.unescape(page)
    r = client.post(f"/bonds/portfolios/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303 and [p.name for p in _store().portfolios(sam.id)] == ["IIS"]


def test_rename_and_update_open_as_popups_a_press_elsewhere_closes(client) -> None:
    pid = _portfolio(_as(client), "Broker", SYS6)
    page = client.get(f"/bonds/portfolios/{pid}").text
    assert page.count(" data-popup") == 2 and '<script src="/static/popup.js?v=' in page
    assert "details[data-popup][open]" in client.get("/static/popup.js").text


def test_nobody_reaches_anyone_elses_portfolio(client) -> None:
    alex = _as(client, "Alex")
    pid = _portfolio(alex, "Alex's", SYS6)
    _as(client, "Sam")
    assert client.get(f"/bonds/portfolios/{pid}").status_code == 404
    assert client.get(f"/bonds/portfolios/{pid}/holdings.csv").status_code == 404
    assert client.get("/bonds/search", params={"q": "Система", "portfolio": pid}).status_code == 404
    for action, data in (("add", {"code": "RU000A10FA72", "qty": "1"}),
                         ("remove", {"key": "RU000A0JXN21"}),
                         ("qty", {"key": "RU000A0JXN21", "qty": "99"}),
                         ("restore", {"code": "RU000A10FA72", "qty": "1"}),
                         ("rename", {"name": "Mine"}), ("delete", {})):
        assert client.post(f"/bonds/portfolios/{pid}/{action}", data=data).status_code == 404
    r = client.post(f"/bonds/portfolios/{pid}/import",
                    files={"file": ("w.csv", b"RU000A10FA72 1\n", "text/csv")})
    assert r.status_code == 404
    # the calendar's portfolio filter takes only your own
    assert "SAMBOND" not in client.get(f"/bonds/events?portfolio={pid}").text
    assert [(h.code, h.qty) for h in _store().holdings(alex.id)] == [("RU000A0JXN21", 10.0)]
    assert _store().portfolios(alex.id)[0].name == "Alex's"


def test_a_portfolio_is_a_bond_a_row(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6, OFZ_HELD)
    page = client.get(f"/bonds/portfolios/{pid}").text
    assert "<textarea" not in page and "2 bonds" in page
    assert page.count(f'hx-post="/bonds/portfolios/{pid}/remove"') == 2
    assert 'aria-label="Remove АФК Система, 001P-06"' in page
    assert 'name="qty" value="10"' in page
    assert f'hx-get="/bonds/search?portfolio={pid}"' in page
    assert page.count('id="bond-pill-count"') == 1  # the pill's; an edit's answer updates it


def test_search_offers_add_and_marks_what_this_portfolio_holds(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6, OFZ_HELD)
    other = _portfolio(sam, "IIS")
    r = client.get("/bonds/search", params={"q": "Система", "portfolio": pid})
    assert r.status_code == 200
    assert "АФК Система БО 002Р-14" in r.text and "matures 2030-02-01" in r.text
    assert "coupon 18%" in r.text
    assert r.text.count(f'hx-post="/bonds/portfolios/{pid}/add"') == 1  # 001P-06 is held
    assert "in this portfolio" in r.text and 'hx-include="#bond-q, #bond-add-qty"' in r.text
    # the OFZ is held by its MOEX code; found by ISIN, it is still this portfolio's
    r = client.get("/bonds/search", params={"q": "RU000A1038V6", "portfolio": pid})
    assert "in this portfolio" in r.text
    # …and another portfolio may hold the same bond too
    r = client.get("/bonds/search", params={"q": "RU000A1038V6", "portfolio": other})
    assert f'hx-post="/bonds/portfolios/{other}/add"' in r.text
    assert "No bond matches" in client.get(
        "/bonds/search", params={"q": "нет такой", "portfolio": pid}).text


def test_search_says_when_moex_is_down(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam)
    client.moex.down = True
    r = client.get("/bonds/search", params={"q": "Система", "portfolio": pid})
    assert "MOEX did not answer" in r.text


def test_add_needs_a_quantity_and_names_the_bond_as_moex_has_it(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6)
    for qty in ("", "0", "-2", "lots"):
        r = client.post(f"/bonds/portfolios/{pid}/add", data={"code": "RU000A10FA72", "qty": qty})
        assert "Enter how many you hold" in r.text
    assert len(_store().holdings(sam.id, pid)) == 1
    r = client.post(f"/bonds/portfolios/{pid}/add",
                    data={"code": "RU000A10FA72", "qty": "15", "q": "Система"})
    assert r.status_code == 200 and "2 bonds" in r.text and 'class="bond-added"' in r.text
    assert '<small id="bond-pill-count" hx-swap-oob="true">2</small>' in r.text
    held = _store().holdings(sam.id, pid)[-1]
    assert (held.code, held.name, held.matdate, held.qty) == (
        "RU000A10FA72", "АФК Система БО 002Р-14", date(2030, 2, 1), 15.0)
    # the open search is re-rendered alongside, its Add now "in this portfolio"
    assert 'id="bond-search-results" hx-swap-oob="innerHTML"' in r.text
    assert f'hx-post="/bonds/portfolios/{pid}/add"' not in r.text
    assert "already in this portfolio" in client.post(
        f"/bonds/portfolios/{pid}/add", data={"code": "RU000A0JXN21", "qty": "1"}).text
    assert "does not know" in client.post(
        f"/bonds/portfolios/{pid}/add", data={"code": "XX1", "qty": "1"}).text


def test_add_when_moex_is_down_changes_nothing(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6)
    client.moex.down = True
    r = client.post(f"/bonds/portfolios/{pid}/add", data={"code": "RU000A10FA72", "qty": "1"})
    assert "MOEX did not answer" in r.text and len(_store().holdings(sam.id, pid)) == 1


def test_add_stops_at_the_limit(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam)
    _store().replace(sam.id, pid, [{"code": f"B{i}", "name": "", "matdate": None, "qty": 1}
                                   for i in range(MAX_BONDS)])
    r = client.post(f"/bonds/portfolios/{pid}/add", data={"code": "RU000A10FA72", "qty": "1"})
    assert f"at most {MAX_BONDS} bonds" in r.text


def test_remove_then_undo_puts_the_bond_back_as_it_was(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6, OFZ_HELD)
    r = client.post(f"/bonds/portfolios/{pid}/remove", data={"key": "RU000A0JXN21"})
    assert r.status_code == 200 and "1 bond<" in r.text
    assert "Removed <b>АФК Система, 001P-06</b>" in r.text
    assert [h.code for h in _store().holdings(sam.id, pid)] == ["SU26238RMFS4"]
    undo = r.text.split(f'hx-post="/bonds/portfolios/{pid}/restore"', 1)[1]
    values = undo.split("hx-vals='", 1)[1].split("'", 1)[0]
    restored = client.post(f"/bonds/portfolios/{pid}/restore",
                           data=json.loads(html.unescape(values)))
    assert "2 bonds" in restored.text
    back = _store().holdings(sam.id, pid)[-1]
    assert (back.code, back.name, back.matdate, back.qty) == SYS6
    # removing what is already gone (a second click) just shows the list
    client.post(f"/bonds/portfolios/{pid}/remove", data={"key": "RU000A0JXN21"})
    r = client.post(f"/bonds/portfolios/{pid}/remove", data={"key": "RU000A0JXN21"})
    assert r.status_code == 200 and "Removed" not in r.text and 'role="alert"' not in r.text


def test_qty_is_saved_from_the_row_and_answered_with_the_row(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", OFZ_HELD)
    r = client.post(f"/bonds/portfolios/{pid}/qty", data={"key": "SU26238RMFS4", "qty": "2,5"})
    assert r.status_code == 200 and r.text.strip().startswith("<tr")
    assert 'value="2.5"' in r.text and _store().holdings(sam.id, pid)[0].qty == 2.5
    for bad in ("lots", "-3", "", "0"):
        r = client.post(f"/bonds/portfolios/{pid}/qty", data={"key": "SU26238RMFS4", "qty": bad})
        assert 'aria-invalid="true"' in r.text and 'value="2.5"' in r.text  # unchanged
    assert _store().holdings(sam.id, pid)[0].qty == 2.5
    # a bond removed meanwhile (another tab): an empty answer, which drops the row
    r = client.post(f"/bonds/portfolios/{pid}/qty", data={"key": "GONE", "qty": "1"})
    assert r.text == ""


def test_a_portfolio_downloads_as_the_scripts_csv(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Тинькофф", SYS6, OFZ_HELD)
    r = client.get(f"/bonds/portfolios/{pid}/holdings.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "filename*=UTF-8''%D0%A2" in r.headers["content-disposition"]
    assert r.content.decode("utf-8-sig") == (
        'code,name,matdate,qty\r\nRU000A0JXN21,"АФК Система, 001P-06",2027-03-26,10\r\n'
        'SU26238RMFS4,,,2\r\n')


def test_import_resolves_names_into_that_portfolio_only() -> None:
    def resolve(items, log, pmap=map):
        for it in items:
            it["code"] = it["code"] or "RU000A0JXN21"
            log(f"  {it['name']}  →  {it['code']}")
        return []

    c = _client(resolve=resolve)
    try:
        sam = _as(c)
        pid = _portfolio(sam, "Broker", OFZ_HELD)
        other = _portfolio(sam, "IIS", OFZ_HELD)
        r = c.post(f"/bonds/portfolios/{pid}/import",
                   data={"text": 'name,matdate,qty\n"АФК Система, 001P-06",2027-03-26,4\n'})
        assert r.status_code == 200 and "Imported 1 bond." in r.text
        assert "АФК Система, 001P-06  →  RU000A0JXN21" in r.text  # to check the match
        assert [(h.code, h.qty) for h in _store().holdings(sam.id, pid)] == [
            ("RU000A0JXN21", 4.0)]
        assert [h.code for h in _store().holdings(sam.id, other)] == ["SU26238RMFS4"]
    finally:
        c.__exit__(None, None, None)


def test_import_refuses_empty_and_oversized_lists(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6)
    r = client.post(f"/bonds/portfolios/{pid}/import", data={"text": "# nothing\n"})
    assert r.status_code == 422 and "nothing changed" in r.text
    many = "".join(f"B{i}\n" for i in range(MAX_BONDS + 1))
    r = client.post(f"/bonds/portfolios/{pid}/import", data={"text": many})
    assert r.status_code == 422 and f"at most {MAX_BONDS}" in r.text
    assert [h.code for h in _store().holdings(sam.id, pid)] == ["RU000A0JXN21"]  # untouched


def test_an_upload_wins_over_the_text_and_reads_windows_1251(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam)
    raw = "code;name;qty\nRU000A0ZYQY7;АФК Система, 001P-07;3\n".encode("cp1251")
    r = client.post(f"/bonds/portfolios/{pid}/import", data={"text": "IGNORED\n"},
                    files={"file": ("portfolio.csv", raw, "text/csv")})
    assert r.status_code == 200
    assert [(h.code, h.name, h.qty) for h in _store().holdings(sam.id, pid)] == [
        ("RU000A0ZYQY7", "АФК Система, 001P-07", 3.0)]


def test_an_intelinvest_report_imports_the_bonds_held_with_moex_dates(client) -> None:
    from _workbooks import intelinvest_report

    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6)
    raw = intelinvest_report()
    r = client.post(f"/bonds/portfolios/{pid}/import",
                    files={"file": ("complex_portfolio_report_11111.xlsx", raw,
                                    "application/vnd.openxmlformats-officedocument"
                                    ".spreadsheetml.sheet")})
    assert r.status_code == 200
    assert "Imported 2 bonds from sheet «Портфель облигаций»." in r.text
    assert "1 sold out (a quantity of 0) left out." in r.text
    assert [(h.code, h.name, h.qty) for h in _store().holdings(sam.id, pid)] == [
        ("RU000A10EMG2", "НоваБев3Р3", 120.0), ("RU000A0JS3W6", "ОФЗ 26207", 45.0)]


def test_imported_bonds_get_their_maturity_from_moexs_list(client) -> None:
    sam = _as(client)
    pid = _portfolio(sam)
    # the report has no dates; MOEX's list does (SISTEMA_14 matures 2030-02-01)
    client.post(f"/bonds/portfolios/{pid}/import",
                data={"text": "RU000A10FA72 4\nXX0000000001 1\n"})
    held = {h.code: h for h in _store().holdings(sam.id, pid)}
    assert held["RU000A10FA72"].matdate == date(2030, 2, 1)
    assert held["RU000A10FA72"].qty == 4.0
    assert held["XX0000000001"].matdate is None  # not traded: no date, and no request for one


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_update_brings_a_portfolio_up_to_date_from_a_newer_report(client) -> None:
    from _workbooks import HEADER, intelinvest_report, workbook

    sam = _as(client)
    pid = _portfolio(sam, "ГПБ")
    page = client.get(f"/bonds/portfolios/{pid}").text
    assert f'action="/bonds/portfolios/{pid}/update"' not in page  # nothing to update yet
    client.post(f"/bonds/portfolios/{pid}/import",
                files={"file": ("complex_portfolio_report_11111.xlsx", intelinvest_report(),
                                XLSX)})
    page = client.get(f"/bonds/portfolios/{pid}").text
    assert f'action="/bonds/portfolios/{pid}/update"' in page
    assert f"From <b>complex_portfolio_report_11111.xlsx</b>, imported {date.today()}" in page
    # a month on: more of one bond, a new one, the OFZ sold (0, as the report keeps it)
    newer = workbook({"Портфель облигаций": [
        ["intelinvest.ru"], HEADER,
        ["RU000A10EMG2", "НоваБев3Р3", 150.0], ["RU000A0JS3W6", "ОФЗ 26207", 0.0],
        ["RU000A10FA72", "Систем2P14", 30.0]]})
    r = client.post(f"/bonds/portfolios/{pid}/update",
                    files={"file": ("complex_portfolio_report_11111 (1).xlsx", newer, XLSX)})
    assert r.status_code == 200
    text = " ".join(html.unescape(r.text).split())
    assert "Updated from complex_portfolio_report_11111 (1).xlsx: 2 bonds held" in text
    assert "Since before: 1 new, 1 no longer held, 1 with a new quantity; 0 as they were." in text
    assert "new</span> Систем2P14 — 30" in text and "gone</span> ОФЗ 26207 — held 45" in text
    assert "qty</span> НоваБев3Р3 — 120 → 150" in text
    assert [(h.code, h.qty) for h in _store().holdings(sam.id, pid)] == [
        ("RU000A10EMG2", 150.0), ("RU000A10FA72", 30.0)]
    # the same file again: nothing to change
    r = client.post(f"/bonds/portfolios/{pid}/update",
                    files={"file": ("complex_portfolio_report_11111.xlsx", newer, XLSX)})
    assert "Nothing changed: the same 2 bonds in the same quantities." in r.text
    assert "this.closest('.bond-report').remove()" in html.unescape(r.text)  # it can be dismissed


def test_update_refuses_another_intelinvest_portfolios_report(client) -> None:
    from _workbooks import intelinvest_report

    sam = _as(client)
    gpb = _portfolio(sam, "ГПБ")
    report = intelinvest_report()
    client.post(f"/bonds/portfolios/{gpb}/import",
                files={"file": ("complex_portfolio_report_11111.xlsx", report, XLSX)})
    held = _store().holdings(sam.id, gpb)
    r = client.post(f"/bonds/portfolios/{gpb}/update",
                    files={"file": ("complex_portfolio_report_11769.xlsx", report, XLSX)})
    assert r.status_code == 422
    says = ("That is the report of Intelinvest portfolio 11769, but ГПБ is updated from "
            "portfolio 11111's — nothing changed.")
    assert html.unescape(r.text).count(says) == 1  # above the list, not in it as well
    assert _store().holdings(sam.id, gpb) == held
    # the import is how a portfolio moves to another report
    client.post(f"/bonds/portfolios/{gpb}/import",
                files={"file": ("complex_portfolio_report_11769.xlsx", report, XLSX)})
    assert _store().portfolio(sam.id, gpb).source == "complex_portfolio_report_11769.xlsx"
    # a portfolio made by hand has no report yet: its first update takes any
    iis = _portfolio(sam, "ИИС", SYS6)
    r = client.post(f"/bonds/portfolios/{iis}/update",
                    files={"file": ("complex_portfolio_report_11111.xlsx", report, XLSX)})
    assert r.status_code == 200 and "2 new, 1 no longer" in r.text
    assert _store().portfolio(sam.id, iis).source == "complex_portfolio_report_11111.xlsx"


def test_update_reaches_only_your_own_portfolio(client) -> None:
    alex = _as(client, "Alex")
    theirs = _portfolio(alex, "Alex's", SYS6)
    _as(client, "Sam")
    r = client.post(f"/bonds/portfolios/{theirs}/update",
                    files={"file": ("complex_portfolio_report_1.xlsx", b"", XLSX)})
    assert r.status_code == 404
    assert [h.qty for h in _store().holdings(alex.id, theirs)] == [10.0]


def test_a_file_that_is_not_a_portfolio_changes_nothing(client) -> None:
    from wheel_screener.api import app as app_module

    sam = _as(client)
    pid = _portfolio(sam, "Broker", SYS6)
    r = client.post(f"/bonds/portfolios/{pid}/import",
                    files={"file": ("x.xlsx", b"PK\x03\x04 broken", "application/octet-stream")})
    assert r.status_code == 422 and "not a spreadsheet" in r.text and "Nothing changed" in r.text
    big = b"x" * (app_module._MAX_UPLOAD_BYTES + 1)
    r = client.post(f"/bonds/portfolios/{pid}/import", files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413 and "too large" in r.text
    sold = "code,qty\nRU000A0JXN21,0\n"
    r = client.post(f"/bonds/portfolios/{pid}/import", data={"text": sold})
    assert r.status_code == 422 and "No bonds held in that file" in r.text
    assert [h.code for h in _store().holdings(sam.id, pid)] == ["RU000A0JXN21"]


# --- the calendar --------------------------------------------------------------------------------

def test_the_tab_shows_your_portfolios_and_lazy_loads_the_default_month(client) -> None:
    sam = _as(client)
    _portfolio(sam, "Broker", SYS6)
    r = client.get("/bonds")
    assert r.status_code == 200
    assert 'href="/bonds" class="nav-tab active"' in r.text and "1 bond in" in r.text
    assert 'name="portfolio"' not in r.text  # one portfolio: nothing to choose
    today = date.today()
    q = f"from={today.isoformat()}&amp;to={bc.one_month_after(today).isoformat()}"
    assert f'hx-get="/bonds/events?{q}"' in r.text
    assert f'name="to" value="{bc.one_month_after(today).isoformat()}"' in r.text
    iis = _portfolio(sam, "IIS", OFZ_HELD)
    r = client.get(f"/bonds?portfolio={iis}")
    assert 'name="portfolio"' in r.text and f'value="{iis}" selected' in r.text
    assert f"portfolio={iis}" in r.text and "1 bond in\n        IIS" in r.text


def test_an_empty_portfolio_says_so_without_building(client) -> None:
    sam = _as(client)
    assert "No\n     bonds in your portfolios yet" in client.get("/bonds/events").text
    pid = _portfolio(sam, "Broker")
    assert "This portfolio holds no bonds yet" in client.get(f"/bonds/events?portfolio={pid}").text
    assert app.state.bonds._build.calls == []


def test_all_portfolios_add_up_and_one_portfolio_shows_its_own() -> None:
    today = date.today()
    events = [_event(today), _event(today, name="ОФЗ 26238", isin="RU000A1038V6",
                                    secid="SU26238RMFS4", amount=10.0)]
    builds = _Builds(events)
    c = _client(builds)
    try:
        sam = _as(c)
        broker = _portfolio(sam, "Broker", SYS6)
        iis = _portfolio(sam, "IIS", (*SYS6[:3], 5.0), OFZ_HELD)
        r = c.get("/bonds/events")
        assert "<strong>2 events</strong>" in r.text
        assert "Broker 10 · IIS 5" in r.text and "IIS 2" in r.text  # who holds each
        assert "536,70\u00a0₽" in r.text  # 15 × 35.78, both portfolios
        r = c.get(f"/bonds/events?portfolio={broker}")
        assert "<strong>1 event</strong>" in r.text and "357,80\u00a0₽" in r.text
        assert "bond-holders" not in r.text  # one portfolio: no need to say whose
        c.get(f"/bonds/events?portfolio={iis}")
        assert len(builds.calls) == 1  # every view is the same build
        # a changed quantity is the same build too
        _store().set_qty(sam.id, broker, "RU000A0JXN21", 20.0)
        assert "894,50\u00a0₽" in c.get("/bonds/events").text and len(builds.calls) == 1
        # the downloads that are tables: a line per event and portfolio holding the bond
        rows = list(csv.reader(io.StringIO(
            c.get("/bonds/export.csv").content.decode("utf-8-sig"))))
        assert rows[0][8:11] == ["Portfolio", "Qty", "Total"]
        assert [(r[2], r[8], r[9], r[10], r[11]) for r in rows[1:]] == [
            ("АФК Система, 001P-06", "Broker", "20", "715.60", "record date 2027-01-09"),
            ("АФК Система, 001P-06", "IIS", "5", "178.90", "record date 2027-01-09"),
            ("ОФЗ 26238", "IIS", "2", "20.00", "record date 2027-01-09")]
        book = Workbook(c.get("/bonds/export.xlsx").content)
        assert [r[8:11] for r in book.rows(sheet.SHEET)[1:]] == [
            ["Broker", "20.0", "715.6"], ["IIS", "5.0", "178.9"], ["IIS", "2.0", "20.0"]]
        rows = list(csv.reader(io.StringIO(
            c.get(f"/bonds/export.csv?portfolio={broker}").content.decode("utf-8-sig"))))
        assert [r[8:10] for r in rows[1:]] == [["Broker", "20"]]
        # the .ics stays an event each, the portfolios in its note
        assert "Portfolios: Broker 20 · IIS 5" in c.get(
            "/bonds/calendar.ics").text.replace("\r\n ", "")
    finally:
        c.__exit__(None, None, None)


def test_events_group_by_month_with_problems_notes_and_totals() -> None:
    today = date.today()
    later = bc.one_month_after(today).replace(day=1)  # next month: still in the default month
    c = _client(_Builds([_event(today),
                         _event(later, bc.OFFER_LAST_DAY, amount=None, note="check")],
                        problems=["АФК Система, 001P-06: not found on MOEX",
                                  "ГТЛК, 001P-04: not in this portfolio"]))
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", SYS6, ("RU000A0ZYQY7", "Matured", date(2020, 1, 1), 1.0))
        _portfolio(sam, "IIS", ("RU000A10FA72", "Also matured", date(2021, 1, 1), 1.0))
        r = c.get("/bonds/events")
        assert "<strong>2 events</strong>" in r.text
        assert today.strftime("%B %Y") in r.text and later.strftime("%B %Y") in r.text
        assert 'class="ev ev-deadline"' in r.text and 'class="soon"' in r.text
        assert "Could not process 1" in r.text and "not found on MOEX" in r.text
        assert "not in this portfolio" not in r.text  # another portfolio's problem
        assert "Skipping 2 already matured bonds" in r.text  # over every portfolio shown
        assert ">Total</th>" in r.text and "357,80\u00a0₽" in r.text
        assert "Expected payments by month" in r.text
        broker = _store().portfolios(sam.id)[0].id
        assert "Skipping 1 already matured bonds" in c.get(
            f"/bonds/events?portfolio={broker}").text  # this portfolio's only
    finally:
        c.__exit__(None, None, None)


def test_the_tab_shows_a_month_ahead_and_the_feed_a_year() -> None:
    today = date.today()
    c = _client(_Builds([_event(today + timedelta(days=10), name="Soon"),
                         _event(today + timedelta(days=60), name="Later"),
                         _event(today + timedelta(days=400), name="Next year")]))
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", SYS6)
        r = c.get("/bonds/events")
        assert "<strong>1 event</strong>" in r.text and "Soon" in r.text and "Later" not in r.text
        assert f"{today} to {bc.one_month_after(today)}" in r.text
        assert "<strong>2 events</strong>" in c.get(
            f"/bonds/events?to={bc.one_year_after(today)}").text  # chosen dates still win
        link = c.post("/bonds/feed").text.split("/feeds/bonds/", 1)[1].split('"', 1)[0]
        c.cookies.clear()
        feed = c.get(f"/feeds/bonds/{link}").text
        assert "— Soon —" in feed and "— Later —" in feed and "Next year" not in feed
    finally:
        c.__exit__(None, None, None)


def test_period_flags_reach_the_build() -> None:
    builds = _Builds([_event(date(2027, 3, 1)), _event(date(2028, 3, 1))])
    c = _client(builds)
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", SYS6)
        assert "the whole calendar" in c.get("/bonds/events?all=1").text
        assert builds.calls[-1][1:] == (None, None)
        r = c.get("/bonds/events?from=2027-12-31&to=2027-01-01")  # swapped: normalized
        assert builds.calls[-1][1:] == (date(2027, 1, 1), None)  # end windowed after the build
        assert "<strong>1 event</strong>" in r.text and "2027-01-01 to 2027-12-31" in r.text
    finally:
        c.__exit__(None, None, None)


def test_the_calendar_view_is_a_month_grid_stepping_through_the_period() -> None:
    jan15 = date(2027, 1, 15)
    builds = _Builds([_event(jan15, qty=10.0), _event(jan15, bc.OFFER_LAST_DAY, amount=None),
                      _event(date(2027, 2, 3)), _event(date(2027, 3, 31), bc.REDEMPTION)])
    c = _client(builds)
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", SYS6)
        period = "from=2027-01-10&to=2027-03-31"
        # today is outside the period: its first month; the days before the period are marked
        r = c.get(f"/bonds/events?{period}&view=calendar")
        assert "<strong>4 events</strong>" in r.text  # the period's count, in either view
        assert "<h3>January 2027</h3>" in r.text and 'class="bond-grid"' in r.text
        assert 'href="#bond-day-2027-01-15"' in r.text
        assert r.text.count('id="bond-day-2027-01-15"') == 1  # the day's first row only
        assert 'class="bond-day off"' in r.text  # Jan 1–9: before the period
        assert "bond-day-2027-02-03" not in r.text  # one month's events
        assert 'aria-current="true">Calendar</a>' in r.text
        assert 'class="bond-step" aria-hidden="true">‹' in r.text  # nothing before January
        nxt = "from=2027-01-10&amp;to=2027-03-31&view=calendar&month=2027-02"
        assert f'hx-get="/bonds/events?{nxt}"' in r.text and f'hx-push-url="/bonds?{nxt}"' in r.text
        assert "Оферта: последний день заявок" in r.text.split('class="bond-legend"')[1]
        assert "357,80\u00a0₽" in r.text and "expected" in r.text  # the month's payments
        r = c.get(f"/bonds/events?{period}&view=calendar&month=2027-03")
        assert "<h3>March 2027</h3>" in r.text and 'id="bond-day-2027-03-31"' in r.text
        assert 'class="bond-step" aria-hidden="true">›' in r.text  # nothing after March
        # a month outside the period is the nearest one in it; nonsense is the default month
        assert "<h3>March 2027</h3>" in c.get(
            f"/bonds/events?{period}&view=calendar&month=2031-07").text
        assert "<h3>January 2027</h3>" in c.get(
            f"/bonds/events?{period}&view=calendar&month=soon").text
        assert len(builds.calls) == 1  # every month is the same build
    finally:
        c.__exit__(None, None, None)


def test_the_whole_calendar_steps_from_the_first_event_to_the_last() -> None:
    today = date.today()
    first, last = date(today.year - 3, 6, 1), date(today.year + 4, 2, 1)
    c = _client(_Builds([_event(first), _event(last)]))
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", SYS6)
        r = c.get("/bonds/events?all=1&view=calendar")
        assert f"<h3>{today:%B %Y}</h3>" in r.text  # this month, between the two
        assert "No events in" in r.text and "Today</a>" not in r.text
        assert f"<h3>{first:%B %Y}</h3>" in c.get(
            "/bonds/events?all=1&view=calendar&month=1990-01").text
        r = c.get("/bonds/events?all=1&view=calendar&month=2999-01")
        assert f"<h3>{last:%B %Y}</h3>" in r.text and "Today</a>" in r.text
    finally:
        c.__exit__(None, None, None)


def test_a_busy_day_shows_three_chips_and_how_many_more() -> None:
    day = date(2027, 1, 15)
    c = _client(_Builds([_event(day, name=f"Bond {i}", secid=f"B{i}") for i in range(5)]))
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", SYS6)
        r = c.get("/bonds/events?from=2027-01-01&to=2027-01-31&view=calendar")
        assert 'class="bond-chips capped"' in r.text and "+2 more" in r.text
    finally:
        c.__exit__(None, None, None)


def test_amounts_read_the_russian_way_and_never_wrap() -> None:
    from wheel_screener.api.app import _bond_count, _bond_money, _bond_num

    nbsp = "\u00a0"
    assert _bond_money(120000.0, "RUB") == f"120{nbsp}000,00{nbsp}₽"
    assert _bond_money(35.78, "SUR") == f"35,78{nbsp}₽"
    assert _bond_money(1234567.8, "USD") == f"1{nbsp}234{nbsp}567,80{nbsp}$"
    assert _bond_money(5.0, "CHF") == f"5,00{nbsp}CHF"  # no sign: its code
    assert _bond_money(5.0, "") == "5,00" and _bond_money(None, "RUB") == ""
    assert _bond_num(12.3456) == "12,3456" and _bond_num(14.35) == "14,35"  # a coupon's rate
    assert _bond_count(1500.0) == f"1{nbsp}500" and _bond_count(0.5) == "0,5"
    assert _bond_count(10.0) == "10" and _bond_count(None) == ""


def test_the_view_chosen_last_is_the_one_the_tab_opens_in(client) -> None:
    sam = _as(client)
    _portfolio(sam, "Broker", SYS6)
    r = client.get("/bonds/events")
    assert 'class="bond-table' in r.text and "bond-grid" not in r.text  # the list by default
    assert "bond_view" not in r.headers.get("set-cookie", "")  # nothing chosen yet
    cookie = client.get("/bonds/events?view=calendar").headers["set-cookie"]
    assert "bond_view=calendar" in cookie and "Path=/bonds" in cookie
    assert "bond-grid" in client.get("/bonds/events").text  # no view asked: the last one
    assert "bond-grid" in client.get("/bonds/events?view=table").text  # not a view: ignored
    assert "bond-grid" not in client.get("/bonds/events?view=list").text
    assert "bond-grid" not in client.get("/bonds/events").text
    # the tab passes a link's view and month on to the calendar it loads
    today = date.today()
    q = f"from={today.isoformat()}&amp;to={bc.one_month_after(today).isoformat()}"
    r = client.get("/bonds?view=calendar&month=2027-02")
    assert f'hx-get="/bonds/events?{q}&amp;view=calendar&amp;month=2027-02"' in r.text
    assert f'hx-get="/bonds/events?{q}"' in client.get("/bonds?view=grid&month=feb").text


def test_csv_and_ics_downloads_are_the_scripts_formats(client) -> None:
    sam = _as(client)
    _portfolio(sam, "Broker", SYS6)
    r = client.get("/bonds/export.csv?all=1")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith("\ufeffDate,Event,Bond".encode())  # utf-8-sig, as the script
    assert "35,78" in client.get("/bonds/export.csv?all=1&sep=%3B").content.decode("utf-8-sig")
    r = client.get("/bonds/export.xlsx?all=1")
    assert r.headers["content-type"] == sheet.MEDIA_TYPE
    assert 'filename="bond_calendar.xlsx"' in r.headers["content-disposition"]
    assert Workbook(r.content).rows(sheet.SHEET)[1][5] == "35.78"
    page = client.get("/bonds/events?all=1").text
    assert 'href="/bonds/export.xlsx?all=1"' in page and "CSV for Excel" not in page
    r = client.get("/bonds/calendar.ics?all=1")
    assert r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in r.text and "X-WR-CALNAME:Bonds" in r.text
    assert "Total for 10 pcs" in r.text.replace("\r\n ", "")


def test_the_grouped_export_is_a_line_per_event_and_portfolio_on_two_sheets() -> None:
    day = date(2027, 1, 10)
    c = _client(_Builds([_event(day, short="СистемБ1P6"),
                         _event(date(2027, 3, 26), bc.REDEMPTION, amount=1000.0, rate=None,
                                short="СистемБ1P6")]))
    try:
        sam = _as(c)
        gpb = _portfolio(sam, "ГПБ", SYS6)
        _portfolio(sam, "ИИС", (*SYS6[:3], 4.0))
        r = c.get("/bonds/grouped_export.xlsx?all=1")
        assert r.headers["content-type"] == sheet.MEDIA_TYPE
        assert 'filename="grouped_export.xlsx"' in r.headers["content-disposition"]
        book = Workbook(r.content)
        serial = str((day - date(1899, 12, 30)).days)
        assert book.rows(sheet.GROUPED_SHEETS[0]) == [
            [serial, "СистемБ1P6", "14,35", "ГПБ", "357,80"],
            [serial, "СистемБ1P6", "14,35", "ИИС", "143,12"]]
        assert [row[2:] for row in book.rows(sheet.GROUPED_SHEETS[1])] == [
            [bc.REDEMPTION, "ГПБ", "10000"], [bc.REDEMPTION, "ИИС", "4000"]]
        # one portfolio shown: only its lines
        r = c.get(f"/bonds/grouped_export.xlsx?all=1&portfolio={gpb}")
        assert [row[3] for row in Workbook(r.content).rows(sheet.GROUPED_SHEETS[0])] == ["ГПБ"]
        page = c.get(f"/bonds/events?all=1&portfolio={gpb}").text
        assert f'href="/bonds/grouped_export.xlsx?all=1&amp;portfolio={gpb}"' in page
    finally:
        c.__exit__(None, None, None)


def test_the_feed_link_is_shown_once_reads_without_signing_in_and_can_be_replaced() -> None:
    c = _client()
    try:
        sam = _as(c)
        _portfolio(sam, "Broker", ("SAMBOND", "", None, 1.0))
        r = c.post("/bonds/feed")
        assert r.status_code == 200 and "shown only once" in r.text
        origin = app.state.settings.passkeys.origin.rstrip("/")
        link = r.text.split(f'value="{origin}', 1)[1].split('"', 1)[0]
        assert link.startswith("/feeds/bonds/") and link.endswith(".ics")
        assert f'href="webcal://{origin.split("://", 1)[1]}{link}"' in r.text
        _portfolio(sam, "IIS", ("SAMBOND2", "", None, 2.0))
        c.cookies.clear()  # a calendar app has no session
        feed = c.get(link)
        assert feed.status_code == 200 and "BEGIN:VCALENDAR" in feed.text
        unfolded = feed.text.replace("\r\n ", "")
        assert "— SAMBOND —" in unfolded and "— SAMBOND2 —" in unfolded  # every portfolio
        _session_for(c, sam)  # back in the browser: the button now offers a NEW link
        assert "New feed link" in c.get("/bonds/events").text
        c.post("/bonds/feed")
        c.cookies.clear()
        assert c.get(link).status_code == 404  # the old link is off
        assert c.get("/feeds/bonds/guess.ics").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_removing_someone_removes_their_portfolios_and_feed() -> None:
    c = _client()
    try:
        alex = _as(c, "Alex")
        _portfolio(alex, "Alex's", SYS6)
        link = _store().new_feed(alex.id)
        _as(c, "Sam", admin=True)
        assert "Alex no longer has access" in c.post(
            "/admin/people/remove", data={"user_id": alex.id}).text
        assert _store().portfolios(alex.id) == [] and _store().user_for_feed(link) is None
    finally:
        c.__exit__(None, None, None)
