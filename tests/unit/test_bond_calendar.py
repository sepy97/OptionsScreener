"""The ported bond calendar (MOEX ISS), driven by a fake ISS — no network."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from wheel_screener.bonds import bond_calendar as bc


def _block(rows: list[dict]) -> dict:
    """An ISS response block ({columns, data}); ISS spells columns in upper case."""
    cols = sorted({k for r in rows for k in r})
    return {"columns": [c.upper() for c in cols], "data": [[r.get(c) for c in cols] for r in rows]}


class FakeIss:
    """Answers bc.get_json for a few bonds: {secid: {card, coupons, amortizations, offers}}."""

    def __init__(self, bonds: dict[str, dict], search: list[dict] | None = None) -> None:
        self.bonds = bonds
        self.search = search or []
        self.calls: list[str] = []

    def __call__(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        params = params or {}
        self.calls.append(path)
        if path == "/engines/stock/markets/bonds/securities.json":
            wanted = params.get("securities")
            cards = [b["card"] for s, b in self.bonds.items()
                     if b.get("traded", True) and (wanted is None or s in wanted.split(","))]
            return {"securities": _block(cards)}
        if path.endswith("/bondization.json"):
            secid = path.split("/")[2]
            out = {}
            for name in ("coupons", "amortizations", "offers"):
                rows = self.bonds[secid].get(name, [])
                out[name] = _block(rows)
                out[f"{name}.cursor"] = _block([{"index": 0, "total": len(rows), "pagesize": 100}])
            return out
        if path.startswith("/securities/"):
            secid = path.split("/")[2].removesuffix(".json")
            card = self.bonds.get(secid, {}).get("card", {})
            return {"description": _block([{"name": k, "value": v} for k, v in card.items()])}
        if path == "/securities.json":
            return {"securities": _block(self.search)}
        raise AssertionError(f"unexpected ISS call {path}")


@pytest.fixture
def iss(monkeypatch):
    def install(bonds: dict[str, dict], search: list[dict] | None = None) -> FakeIss:
        fake = FakeIss(bonds, search)
        monkeypatch.setattr(bc, "get_json", fake)
        monkeypatch.setattr(bc, "connect", lambda: None)  # no real MOEX connections in tests
        return fake
    return install


def _card(secid: str, matdate: str, shortname: str = "Bond") -> dict:
    return {"SECID": secid, "ISIN": secid, "SHORTNAME": shortname, "MATDATE": matdate,
            "FACEVALUE": 1000, "FACEUNIT": "SUR"}


# ---- watchlist parsing ----

def test_parse_csv_with_names_and_quoted_commas() -> None:
    items = bc.parse_watchlist(
        'code,name,matdate,qty\n,"АФК Система, 001P-06",2027-03-26,10\nRU000A0ZYQY7,,,\n'
    )
    assert items[0] == {"code": "", "name": "АФК Система, 001P-06",
                        "matdate": date(2027, 3, 26), "qty": 10.0}
    assert items[1]["code"] == "RU000A0ZYQY7" and items[1]["qty"] is None


def test_parse_russian_semicolon_headers_as_in_cbonds() -> None:
    items = bc.parse_watchlist("﻿Код;Эмиссия;Дата погашения;Количество\n"
                               "RU000A0JXN21;АФК Система, 001P-06;2027-03-26;5,5\n")
    assert items == [{"code": "RU000A0JXN21", "name": "АФК Система, 001P-06",
                      "matdate": date(2027, 3, 26), "qty": 5.5}]


def test_parse_plain_text_comments_and_duplicates_add_up() -> None:
    items = bc.parse_watchlist("RU000A0JXN21 10  # mine\n\n# a comment line\n"
                               "su26238rmfs4\nRU000A0JXN21 5\n")
    assert [(it["code"], it["qty"]) for it in items] == [("RU000A0JXN21", 15.0),
                                                       ("SU26238RMFS4", None)]


def test_format_watchlist_round_trips() -> None:
    items = bc.parse_watchlist(
        'code,name,matdate,qty\nX1,"A, 1",2027-01-02,3\n-,"B, 2",2024-01-01,\n'
    )
    assert bc.parse_watchlist(bc.format_watchlist(items)) == items


# ---- events ----

def test_events_for_maps_coupons_amortization_redemption_and_offers(iss) -> None:
    iss({"B1": {
        "card": _card("B1", "2028-06-01"),
        "coupons": [
            {"coupondate": "2027-01-10", "recorddate": "2027-01-09", "value": 35.5,
             "valueprc": 14.2, "faceunit": "SUR"},
            {"coupondate": "2027-07-10", "recorddate": None, "value": 0, "valueprc": 0,
             "faceunit": "SUR"},  # 0 = not announced yet
        ],
        "amortizations": [
            {"amortdate": "2027-12-01", "value": 500, "valueprc": 50, "faceunit": "SUR",
             "data_source": "amortization"},
            {"amortdate": "2028-06-01", "value": 500, "valueprc": 50, "faceunit": "SUR",
             "data_source": "maturity"},
        ],
        "offers": [
            {"offerdate": "2027-03-10", "offerdatestart": "2027-03-01",
             "offerdateend": "2027-03-05", "offertype": "Put option", "price": 100,
             "facevalue": 1000, "faceunit": "SUR"},
            {"offerdate": "2027-09-10", "offerdatestart": None, "offerdateend": None,
             "offertype": "Put option", "price": None, "facevalue": 1000, "faceunit": "SUR"},
        ],
    }})
    evs = bc.events_for("B1", qty=10, name="My bond")
    by = {(e["date"].isoformat(), e["kind"]): e for e in evs}

    c = by[("2027-01-10", bc.COUPON)]
    assert (c["amount"], c["cur"], c["rate"], c["total"]) == (35.5, "RUB", 14.2, 355.0)
    assert c["note"] == "record date 2027-01-09" and c["name"] == "My bond"
    assert c["short"] == "Bond"  # MOEX's short name, whatever the watchlist calls it
    assert by[("2027-07-10", bc.COUPON)]["note"] == "coupon amount not announced yet"
    assert by[("2027-12-01", bc.AMORTIZATION)]["note"] == "50% of face value"
    assert by[("2028-06-01", bc.REDEMPTION)]["amount"] == 500
    assert "applications until 2027-03-05" in by[("2027-03-01", bc.OFFER_OPENS)]["note"]
    assert by[("2027-03-05", bc.OFFER_LAST_DAY)]["remind"] == bc.REMIND_WINDOW
    assert by[("2027-03-10", bc.OFFER_BUYBACK)]["amount"] == 1000  # 100% of 1000 face
    # dates not announced yet -> remind two weeks early
    assert by[("2027-09-10", bc.OFFER_BUYBACK)]["remind"] == bc.REMIND_NO_WINDOW


def test_events_for_rejects_a_different_bond_by_maturity(iss) -> None:
    iss({"B1": {"card": _card("B1", "2028-06-01")}})
    with pytest.raises(LookupError, match="looks like a different bond"):
        bc.events_for("B1", None, expected_matdate=date(2030, 1, 1))


def test_events_for_redemption_from_card_when_no_amortization_schedule(iss) -> None:
    iss({"B1": {"card": _card("B1", "2028-06-01")}})
    [e] = bc.events_for("B1", None)
    assert (e["kind"], e["date"], e["amount"], e["cur"]) == (bc.REDEMPTION, date(2028, 6, 1),
                                                             1000.0, "RUB")


def test_build_calendar_windows_skips_and_isolates_bad_bonds(iss) -> None:
    iss({
        "B1": {"card": _card("B1", "2028-06-01"),
               "coupons": [{"coupondate": d, "value": 10, "valueprc": 10, "faceunit": "SUR"}
                           for d in ("2026-01-10", "2027-01-10", "2027-02-10")]},
        "B2": {"card": _card("B2", "2027-05-01"), "traded": False},  # card fetched one by one
    })
    items = bc.parse_watchlist(
        "code,name,matdate,qty\nB1,One,2028-06-01,\nB2,Two,2031-01-01,\n"  # B2: wrong maturity
        "OLD,Old,2020-01-01,\n-,Gone,,\n"
    )
    run = bc.build_calendar(items, date(2027, 1, 1), date(2027, 12, 31), workers=2)
    # the 2026 coupon is before the window, the 2028 redemption after it
    assert [(e["date"].isoformat(), e["name"]) for e in run.events] == [
        ("2027-01-10", "One"), ("2027-02-10", "One")
    ]
    assert len(run.problems) == 1 and run.problems[0].startswith("Two: MOEX maturity date")
    assert any("1 already matured" in n for n in run.notes)
    assert any("no longer lists" in n for n in run.notes)


# ---- name lookup ----

def test_resolve_names_matches_moex_naming_and_marks_matured_not_found(iss) -> None:
    iss({"RU000A0JXN21": {"card": {
        "SECID": "RU000A0JXN21", "ISIN": "RU000A0JXN21", "SHORTNAME": "Систем1P06",
        "SECNAME": 'АФК "Система" ПАО БО-001P-06', "LATNAME": "", "MATDATE": "2027-03-26",
    }}})
    items = bc.parse_watchlist('code,name,matdate,qty\n,"АФК Система, 001P-06",2027-03-26,\n'
                               ',"Ламбумиз, БО-П01",2024-06-20,\n,"Никто, 999P-99",2030-01-01,\n')
    log: list[str] = []
    problems = bc.resolve_names(items, log.append)
    assert items[0]["code"] == "RU000A0JXN21"
    assert items[1]["code"] == bc.NOT_FOUND  # matured and gone: don't search again
    assert items[2]["code"] == "" and problems == [
        "Никто, 999P-99: no ISIN found by name — add it to the watchlist manually"
    ]
    assert any("→  RU000A0JXN21" in line for line in log)


def test_series_variants_cover_the_spellings() -> None:
    assert bc.series_variants("001P-06") == {"001P-06", "1P-6", "001P06", "1P6"}


# ---- output ----

def _event(**kw) -> dict:
    e = {"date": date(2027, 1, 10), "kind": bc.COUPON, "name": "Bond, 1", "isin": "RU1",
         "secid": "RU1", "amount": 35.78, "cur": "RUB", "rate": 14.35, "qty": 2.0,
         "total": 71.56, "note": "record date 2027-01-09", "remind": None}
    return {**e, **kw}


def test_csv_text_matches_the_script_and_supports_excel_decimal_commas() -> None:
    lines = bc.csv_text([_event()]).splitlines()
    assert lines[0] == ",".join(f'"{c}"' if "," in c else c for c in bc.COLUMNS)
    assert lines[1] == ('2027-01-10,Купон,"Bond, 1",RU1,RU1,35.78,RUB,14.35,,2,71.56,'
                        "record date 2027-01-09")  # no portfolio: the script's own events
    held = bc.csv_text([_event(portfolio="ИИС")]).splitlines()[1]
    assert held.split(",")[9:12] == ["ИИС", "2", "71.56"]
    assert bc.csv_text([_event()], ";").splitlines()[1].split(";")[5] == "35,78"


def test_ics_keeps_the_script_uids_reminders_and_line_folding() -> None:
    long_note = "Offer, price 100% of face value; " * 4
    ics = bc.ics_calendar([
        _event(),
        _event(date=date(2027, 3, 5), kind=bc.OFFER_LAST_DAY, amount=None, total=None,
               note=long_note, remind=bc.REMIND_WINDOW),
        _event(date=date(2027, 9, 10), kind=bc.OFFER_BUYBACK, remind=bc.REMIND_NO_WINDOW),
    ], remind_days=3)
    uid = hashlib.sha1(f"2027-01-10|{bc.COUPON}|RU1".encode()).hexdigest()
    assert f"UID:{uid}@bond-calendar" in ics  # same IDs as the script: re-imports don't duplicate
    assert "TRIGGER:-P3D" in ics and "TRIGGER:-P14D" in ics
    assert "SUMMARY:Купон — Bond\\, 1 — 35.78 RUB" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert ics.endswith("END:VCALENDAR\r\n")


def test_monthly_cash_sums_payments_per_month_and_currency() -> None:
    cash = bc.monthly_cash([
        _event(), _event(date=date(2027, 1, 20), total=10.0),
        _event(kind=bc.OFFER_BUYBACK, total=999.0),  # not a payment you receive by holding
        _event(date=date(2027, 2, 1), total=5.0, cur="USD"),
    ])
    assert cash == [("2027-01", "RUB", pytest.approx(81.56)), ("2027-02", "USD", 5.0)]


def test_rows_from_a_spreadsheet_read_like_a_csv() -> None:
    rows = [["Тикер", "Название", "Количество, шт.", "Средняя цена"],
            ["RU000A10EMG2", "НоваБев3Р3", "120.0", "100.25"]]
    assert bc.is_header(rows[0]) and not bc.is_header(rows[1])
    assert bc.parse_rows(rows) == [
        {"code": "RU000A10EMG2", "name": "НоваБев3Р3", "matdate": None, "qty": 120.0}]
    assert bc.parse_rows([]) == []
