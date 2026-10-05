"""The shared blocklist: what it accepts, that it is stored, and that a screen reads it fresh."""

from __future__ import annotations

import logging
import threading

import pytest

from wheel_screener.api.blocklist import MAX_ENTRIES, BlocklistError, BlocklistStore, parse_tickers
from wheel_screener.api.jobs import JobRunner, JobStore
from wheel_screener.core.models import ScreenCriteria


def test_tickers_are_read_the_way_people_type_them() -> None:
    assert parse_tickers(" gme, amc;brk.b  BF-B gme ") == ["GME", "AMC", "BRK.B", "BF-B"]
    assert parse_tickers("") == []


@pytest.mark.parametrize("junk", ["<script>", "A B C D E F G H I J K L 1X", "TOOLONGTICKER",
                                  "GME$", "'; DROP TABLE"])
def test_anything_that_is_not_a_ticker_is_refused_by_name(junk) -> None:
    with pytest.raises(BlocklistError, match="not a ticker"):
        parse_tickers(junk)


def test_the_list_is_stored_and_survives_a_restart(tmp_path) -> None:
    path = str(tmp_path / "jobs.sqlite")
    store = BlocklistStore(path)
    assert store.add("tsla, gme") == ["TSLA", "GME"]
    assert store.add("GME") == [], "already listed is not an error and not a duplicate"
    store.remove("tsla")
    assert BlocklistStore(path).symbols() == ["GME"]


def test_the_list_has_a_size_cap(tmp_path) -> None:
    store = BlocklistStore(str(tmp_path / "jobs.sqlite"))
    store.add(" ".join(f"A{i:04d}" for i in range(MAX_ENTRIES)))
    with pytest.raises(BlocklistError, match="at most"):
        store.add("ZZZ")
    assert len(store.symbols()) == MAX_ENTRIES


def test_it_shares_the_jobs_database_without_disturbing_it(tmp_path) -> None:
    path = str(tmp_path / "jobs.sqlite")
    JobStore(path).create("j", "2026-10-05T00:00:00+00:00")
    BlocklistStore(path).add("GME")
    assert JobStore(path).get("j") is not None and BlocklistStore(path).symbols() == ["GME"]


class _Recorder:
    """A service that records the criteria each screen ran with."""

    def __init__(self) -> None:
        self.seen: list[ScreenCriteria] = []
        self.gate: threading.Event | None = None

    def run_screen(self, criteria, today, *, cancel=None):
        self.seen.append(criteria)
        if self.gate is not None:
            self.gate.wait(2.0)
        return []


def _runner(tmp_path) -> tuple[JobRunner, _Recorder, BlocklistStore]:
    path = str(tmp_path / "jobs.sqlite")
    service, blocklist = _Recorder(), BlocklistStore(path)
    return JobRunner(service, JobStore(path), blocklist), service, blocklist


def test_each_screen_reads_the_list_as_it_starts(tmp_path) -> None:
    runner, service, blocklist = _runner(tmp_path)
    blocklist.add("GME")
    runner.run_blocking(ScreenCriteria())
    blocklist.add("AMC")
    runner.run_blocking(ScreenCriteria())
    assert [c.blocked_symbols for c in service.seen] == [
        frozenset({"GME"}), frozenset({"GME", "AMC"})]


def test_a_screen_can_switch_the_list_off(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wheel_screener.core")  # as the app and CLI do
    runner, service, blocklist = _runner(tmp_path)
    blocklist.add("GME")
    job = runner.get(runner.run_blocking(ScreenCriteria(), use_blocklist=False))
    assert service.seen[0].blocked_symbols == frozenset()
    assert "blocklist: off for this screen" in job["progress"]


def test_an_edit_while_a_screen_runs_waits_for_the_next_one(tmp_path) -> None:
    runner, service, blocklist = _runner(tmp_path)
    blocklist.add("GME")
    service.gate = threading.Event()
    job_id = runner.start(ScreenCriteria())
    for _ in range(200):
        if service.seen:
            break
        threading.Event().wait(0.01)
    blocklist.add("AMC")
    service.gate.set()
    runner.wait(job_id)
    assert service.seen[0].blocked_symbols == frozenset({"GME"})
