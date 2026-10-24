"""BondCalendarService — resolving a watchlist, and the cached calendar builds."""

from __future__ import annotations

from datetime import date

from wheel_screener.bonds import bond_calendar as bc
from wheel_screener.bonds.service import BondCalendarService


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _event(day: date, name: str = "One") -> dict:
    return {"date": day, "kind": bc.COUPON, "name": name, "isin": "B1", "secid": "B1",
            "amount": 10.0, "cur": "RUB", "rate": 10.0, "qty": None, "total": None,
            "note": "", "remind": None}


class _Builds:
    """Stands in for bc.build_calendar: counts builds, returns a fixed run."""

    def __init__(self, problems: list[str] | None = None) -> None:
        self.calls: list[tuple] = []
        self.problems = problems or []

    def __call__(self, items, date_from, date_to, *, workers, log) -> bc.CalendarRun:
        self.calls.append((tuple(it["code"] for it in items), date_from, date_to))
        evs = [_event(date(2027, 1, 10)), _event(date(2027, 6, 10)), _event(date(2028, 1, 10))]
        return bc.CalendarRun(evs, list(self.problems), ["note"])


def _service(builds=None, resolve=None, clock=None, ttl=3600) -> BondCalendarService:
    return BondCalendarService(
        cache_ttl_seconds=ttl, now=clock or _Clock(), build=builds or _Builds(),
        resolve=resolve or (lambda items, log, pmap=map: []),
    )


def test_resolve_looks_up_names_and_returns_the_canonical_csv() -> None:
    def resolve(items, log, pmap=map):
        for it in items:
            if not it["code"]:
                it["code"] = "RU000A0JXN21"
                log(f"  {it['name']}  →  {it['code']}")
        return []

    text, report = _service(resolve=resolve).resolve(
        'name,matdate,qty\n"АФК Система, 001P-06",2027-03-26,10\n')
    assert (report.count, report.reused, report.problems) == (1, 0, [])
    assert report.resolved == ["  АФК Система, 001P-06  →  RU000A0JXN21"]
    assert text == ('code,name,matdate,qty\r\n'
                    'RU000A0JXN21,"АФК Система, 001P-06",2027-03-26,10\r\n')


def test_resolve_reuses_codes_the_saved_watchlist_already_has() -> None:
    looked_up: list[str] = []

    def resolve(items, log, pmap=map):
        looked_up.extend(it["name"] for it in items if not it["code"])
        return []

    previous = 'code,name,matdate,qty\nRU000A0JXN21,"АФК Система, 001P-06",2027-03-26,\n'
    # the next paste carries names only: the known bond keeps its code, the new one is looked up
    text, report = _service(resolve=resolve).resolve(
        'name,matdate\n"АФК Система, 001P-06",2027-03-26\n"ГТЛК, 001P-04",2032-04-06\n',
        previous,
    )
    assert report.reused == 1 and looked_up == ["ГТЛК, 001P-04"]
    assert text.splitlines()[1].startswith("RU000A0JXN21,")


def test_calendar_is_cached_windowed_and_refreshable() -> None:
    builds, clock = _Builds(), _Clock()
    svc = _service(builds=builds, clock=clock)
    run, age = svc.calendar("B1\n", date(2027, 1, 1), date(2027, 12, 31))
    assert [e["date"] for e in run.events] == [date(2027, 1, 10), date(2027, 6, 10)]
    assert age == 0 and run.notes == ["note"]
    # a different END reuses the same build — the build is open-ended and windowed here
    run, _ = svc.calendar("B1\n", date(2027, 1, 1), None)
    assert len(run.events) == 3 and len(builds.calls) == 1
    assert builds.calls[0] == (("B1",), date(2027, 1, 1), None)
    clock.t += 60
    assert svc.calendar("B1\n", date(2027, 1, 1), None)[1] == 60  # age of the cached build
    svc.calendar("B1\n", date(2027, 1, 1), None, refresh=True)
    assert len(builds.calls) == 2


def test_builds_are_keyed_by_the_watchlist_and_start_and_expire() -> None:
    builds, clock = _Builds(), _Clock()
    svc = _service(builds=builds, clock=clock, ttl=3600)
    svc.calendar("B1\n", date(2027, 1, 1), None)
    svc.calendar("B1\n", None, None)  # the whole calendar is its own build
    svc.calendar("B1\nB2\n", date(2027, 1, 1), None)  # someone else's list is its own build
    assert [c[0] for c in builds.calls] == [("B1",), ("B1",), ("B1", "B2")]
    svc.calendar("B1\n", date(2027, 1, 1), None)
    assert len(builds.calls) == 3  # the same list again: cached
    clock.t += 3601
    svc.calendar("B1\n", date(2027, 1, 1), None)
    assert len(builds.calls) == 4


def test_a_build_with_problems_is_retried_soon() -> None:
    builds, clock = _Builds(problems=["B1: timed out"]), _Clock()
    svc = _service(builds=builds, clock=clock, ttl=3 * 3600)
    svc.calendar("B1\n", None, None)
    clock.t += 301  # a MOEX blip must not stick for the full 3h TTL
    svc.calendar("B1\n", None, None)
    assert len(builds.calls) == 2
