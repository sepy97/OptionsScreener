"""BondPortfolioStore — each person's named portfolios, the bonds in them, and their feed link."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date

import pytest

from wheel_screener.api.bond_portfolios import (
    LEGACY_NAME,
    MAX_PORTFOLIOS,
    BondPortfolioStore,
    PortfolioError,
)


def _store(tmp_path) -> BondPortfolioStore:
    return BondPortfolioStore(str(tmp_path / "jobs.sqlite"))


def _item(code="RU000A0JXN21", name="АФК Система, 001P-06", matdate=date(2027, 3, 26), qty=10.0):
    return {"code": code, "name": name, "matdate": matdate, "qty": qty}


def test_portfolios_are_each_persons_own_and_named_once(tmp_path) -> None:
    store = _store(tmp_path)
    broker = store.create("sam", "  Broker  ")
    iis = store.create("sam", "ИИС")
    store.create("alex", "Broker")  # someone else's name is theirs to use
    assert [(p.name, p.count) for p in store.portfolios("sam")] == [("Broker", 0), ("ИИС", 0)]
    with pytest.raises(PortfolioError, match="already have"):
        store.create("sam", "broker")
    with pytest.raises(PortfolioError, match="already have"):
        store.create("sam", "иис")  # not only ASCII case
    with pytest.raises(PortfolioError, match="name"):
        store.create("sam", "   ")
    store.rename("sam", broker.id, "Тинькофф")
    store.rename("sam", iis.id, "ИИС")  # its own name is not "taken"
    with pytest.raises(PortfolioError):
        store.rename("sam", iis.id, "тинькофф")
    assert [p.name for p in store.portfolios("sam")] == ["Тинькофф", "ИИС"]
    assert store.portfolio("alex", broker.id) is None  # an id alone reaches nothing


def test_there_is_a_limit_to_how_many(tmp_path) -> None:
    store = _store(tmp_path)
    for i in range(MAX_PORTFOLIOS):
        store.create("sam", f"P{i}")
    with pytest.raises(PortfolioError, match=f"at most {MAX_PORTFOLIOS}"):
        store.create("sam", "one more")


def test_holdings_add_remove_and_quantities(tmp_path) -> None:
    store = _store(tmp_path)
    pid = store.create("sam", "Broker").id
    added = store.add("sam", pid, _item(name="Займ #1\nвторая  строка"))
    assert added.name == "Займ 1 вторая строка"  # survives the CSV it is exported as
    with pytest.raises(PortfolioError, match="already in this portfolio"):
        store.add("sam", pid, _item(code=" ru000a0jxn21 "))
    # an OFZ held by its MOEX code is the same bond as its ISIN
    store.add("sam", pid, _item(code="SU26238RMFS4", name="ОФЗ 26238", qty=5))
    with pytest.raises(PortfolioError, match="already"):
        store.add("sam", pid, _item(code="RU000A1038V6", name="ОФЗ 26238"),
                  aliases=("SU26238RMFS4", "RU000A1038V6"))
    assert store.set_qty("sam", pid, "SU26238RMFS4", 7.5)
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(PortfolioError):
            store.set_qty("sam", pid, "SU26238RMFS4", bad)
    assert not store.set_qty("sam", pid, "RU000XXXXXXX", 1.0)
    assert [(h.code, h.qty) for h in store.holdings("sam", pid)] == [
        ("RU000A0JXN21", 10.0), ("SU26238RMFS4", 7.5)]
    gone = store.remove("sam", pid, "RU000A0JXN21")
    assert gone is not None and gone.qty == 10.0 and gone.matdate == date(2027, 3, 26)
    assert store.remove("sam", pid, "RU000A0JXN21") is None
    assert [p.count for p in store.portfolios("sam")] == [1]


def test_nobody_reaches_anyone_elses_portfolio(tmp_path) -> None:
    store = _store(tmp_path)
    pid = store.create("sam", "Broker").id
    store.add("sam", pid, _item())
    with pytest.raises(PortfolioError):
        store.add("alex", pid, _item(code="RU000A10FA72"))
    with pytest.raises(PortfolioError):
        store.replace("alex", pid, [])
    assert store.remove("alex", pid, "RU000A0JXN21") is None
    assert not store.set_qty("alex", pid, "RU000A0JXN21", 99.0)
    store.rename("alex", pid, "Mine now")
    store.delete("alex", pid)
    assert store.holdings("alex") == []
    assert [(p.name, p.count) for p in store.portfolios("sam")] == [("Broker", 1)]
    assert store.holdings("sam")[0].qty == 10.0


def test_the_same_bond_in_two_portfolios_and_all_holdings_in_order(tmp_path) -> None:
    store = _store(tmp_path)
    broker, iis = store.create("sam", "Broker").id, store.create("sam", "IIS").id
    store.add("sam", broker, _item(qty=10))
    store.add("sam", iis, _item(qty=5))
    store.add("sam", broker, _item(code="SU26238RMFS4", name="ОФЗ 26238", qty=1))
    assert [(h.portfolio, h.code, h.qty) for h in store.holdings("sam")] == [
        ("Broker", "RU000A0JXN21", 10.0), ("Broker", "SU26238RMFS4", 1.0),
        ("IIS", "RU000A0JXN21", 5.0)]
    store.delete("sam", broker)  # its bonds go with it
    assert [h.portfolio for h in store.holdings("sam")] == ["IIS"]


def test_replace_puts_an_imported_list_in_place(tmp_path) -> None:
    store = _store(tmp_path)
    pid = store.create("sam", "Broker").id
    store.add("sam", pid, _item())
    store.replace("sam", pid, [_item(code="RU000A10FA72", name="Система 2P14", qty=None),
                               _item(code="", name="ГТЛК, 001P-04", qty=3)])
    assert [(h.key, h.qty) for h in store.holdings("sam", pid)] == [
        ("RU000A10FA72", None), ("гтлк, 001p-04", 3.0)]


def test_edits_at_once_each_land(tmp_path) -> None:
    store = _store(tmp_path)
    pid = store.create("sam", "Broker").id
    threads = [threading.Thread(target=store.add, args=("sam", pid, _item(code=f"B{i}", qty=1)))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(h.code for h in store.holdings("sam", pid)) == sorted(f"B{i}" for i in range(20))


def test_a_feed_token_names_its_owner_and_only_its_hash_is_kept(tmp_path) -> None:
    store = _store(tmp_path)
    assert not store.has_feed("sam")
    old = store.new_feed("sam")
    assert store.has_feed("sam") and store.user_for_feed(old) == "sam"
    new = store.new_feed("sam")
    assert store.user_for_feed(old) is None and store.user_for_feed(new) == "sam"
    assert store.user_for_feed("") is None and store.user_for_feed("guess") is None
    with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:  # the file is no way in
        dump = "\n".join(conn.iterdump())
    assert new not in dump


def test_forget_removes_everything_of_theirs(tmp_path) -> None:
    store = _store(tmp_path)
    pid = store.create("sam", "Broker").id
    store.add("sam", pid, _item())
    token = store.new_feed("sam")
    store.create("alex", "Alex's")
    store.forget("sam")
    assert store.portfolios("sam") == [] and store.holdings("sam") == []
    assert store.user_for_feed(token) is None
    assert [p.name for p in store.portfolios("alex")] == ["Alex's"]


def _legacy_db(tmp_path) -> str:
    """A jobs database as the watchlist version left it."""
    path = str(tmp_path / "jobs.sqlite")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE bond_watchlists (user_id TEXT PRIMARY KEY, text TEXT NOT NULL"
                     " DEFAULT '', updated_at TEXT NOT NULL, feed_hash TEXT UNIQUE)")
        conn.executemany("INSERT INTO bond_watchlists VALUES (?, ?, ?, ?)", [
            ("sam", 'code,name,matdate,qty\r\nRU000A0JXN21,"АФК Система, 001P-06",2027-03-26,10'
                    '\r\nSU26238RMFS4,,,\r\n', "2026-10-01", "a" * 64),
            ("alex", "", "2026-10-01", "b" * 64),  # a feed made before any list
            ("kim", "", "2026-10-01", None),
        ])
    return path


def test_each_watchlist_becomes_a_portfolio_and_its_feed_keeps_working(tmp_path) -> None:
    import hashlib

    path = _legacy_db(tmp_path)
    store = BondPortfolioStore(path)
    [main] = store.portfolios("sam")
    assert main.name == LEGACY_NAME and main.count == 2
    assert [(h.code, h.name, h.matdate, h.qty) for h in store.holdings("sam")] == [
        ("RU000A0JXN21", "АФК Система, 001P-06", date(2027, 3, 26), 10.0),
        ("SU26238RMFS4", "", None, None)]
    assert store.portfolios("alex") == [] and store.portfolios("kim") == []
    with sqlite3.connect(path) as conn:
        hashes = dict(conn.execute("SELECT user_id, feed_hash FROM bond_feeds"))
        legacy = conn.execute("SELECT text FROM bond_watchlists WHERE user_id = 'sam'").fetchone()
    assert hashes == {"sam": "a" * 64, "alex": "b" * 64}
    assert "RU000A0JXN21" in legacy[0]  # left as it was, for going back a version
    # a token whose hash the old table held still reads the calendar
    token = "t" * 10
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE bond_feeds SET feed_hash = ? WHERE user_id = 'sam'",
                     (hashlib.sha256(token.encode()).hexdigest(),))
    assert store.user_for_feed(token) == "sam"


def test_a_watchlist_is_moved_only_once(tmp_path) -> None:
    path = _legacy_db(tmp_path)
    store = BondPortfolioStore(path)
    store.delete("sam", store.portfolios("sam")[0].id)
    BondPortfolioStore(path)  # the next start
    assert store.portfolios("sam") == []  # a deleted Main does not come back
