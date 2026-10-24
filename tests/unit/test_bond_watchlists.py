"""BondWatchlistStore — each person's watchlist, and their calendar-feed link."""

from __future__ import annotations

import sqlite3

import pytest

from wheel_screener.api.bond_watchlists import BondWatchlistStore


def _store(tmp_path) -> BondWatchlistStore:
    return BondWatchlistStore(str(tmp_path / "jobs.sqlite"))


def test_each_person_has_their_own_watchlist(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.text("sam") == ""
    store.save("sam", "RU000A0JXN21\n")
    store.save("alex", "SU26238RMFS4\n")
    store.save("sam", "RU000A0ZYQY7\n")  # saving replaces
    assert store.text("sam") == "RU000A0ZYQY7\n" and store.text("alex") == "SU26238RMFS4\n"


def test_a_feed_token_reads_only_its_owners_list_and_only_its_hash_is_kept(tmp_path) -> None:
    store = _store(tmp_path)
    store.save("sam", "RU000A0JXN21\n")
    assert not store.has_feed("sam")
    token = store.new_feed("sam")
    assert store.has_feed("sam") and store.text_for_feed(token) == "RU000A0JXN21\n"
    assert store.text_for_feed("not-a-token") is None and store.text_for_feed("") is None
    with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:  # the file is no way in
        dump = "\n".join(conn.iterdump())
    assert token not in dump


def test_a_new_feed_link_turns_the_old_one_off(tmp_path) -> None:
    store = _store(tmp_path)
    old = store.new_feed("sam")  # before any watchlist: a feed of an empty list
    assert store.text_for_feed(old) == ""
    new = store.new_feed("sam")
    assert store.text_for_feed(old) is None and store.text_for_feed(new) == ""
    store.save("sam", "X1\n")  # saving the list keeps the feed
    assert store.text_for_feed(new) == "X1\n"


def test_forget_removes_the_list_and_the_feed(tmp_path) -> None:
    store = _store(tmp_path)
    store.save("sam", "X1\n")
    token = store.new_feed("sam")
    store.forget("sam")
    assert store.text("sam") == "" and store.text_for_feed(token) is None


def test_an_edit_saves_what_the_change_returns_and_nothing_if_it_raises(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.edit("sam", lambda text: text + "X1\n") == "X1\n"  # no list yet: starts empty
    assert store.edit("sam", lambda text: text + "X2\n") == "X1\nX2\n"
    token = store.new_feed("sam")

    def refuse(text: str) -> str:
        raise ValueError("no")

    with pytest.raises(ValueError):
        store.edit("sam", refuse)
    assert store.text("sam") == "X1\nX2\n" and store.text_for_feed(token) == "X1\nX2\n"


def test_edits_at_once_each_see_the_others_result(tmp_path) -> None:
    import threading

    store = _store(tmp_path)
    store.save("sam", "")
    threads = [threading.Thread(target=store.edit, args=("sam", lambda t, i=i: t + f"B{i}\n"))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(store.text("sam").split()) == sorted(f"B{i}" for i in range(20))
