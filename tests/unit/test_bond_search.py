"""BondDirectory — finding a bond to add, by ISIN, MOEX code or name, over MOEX's cached list."""

from __future__ import annotations

from datetime import date

import pytest

from wheel_screener.bonds.search import Bond, BondDirectory, MoexUnavailable, search_key

# as MOEX writes them — note the Cyrillic Р in 002Р-14 and the full and short series forms
SISTEMA_6 = Bond("RU000A0JXN21", "RU000A0JXN21", 'АФК "Система" ПАО БО-001P-06', "СистемБ1P6",
                 "AFK Systema 001P-06", date(2027, 3, 26), 10.5, "RUB")
SISTEMA_14 = Bond("RU000A10FA72", "RU000A10FA72", "АФК Система БО 002Р-14", "Систем2P14",
                  matdate=date(2030, 2, 1), coupon=18.0, currency="RUB")
OFZ = Bond("SU26238RMFS4", "RU000A1038V6", "ОФЗ-ПД 26238 15/05/2041", "ОФЗ 26238",
           matdate=date(2041, 5, 15), coupon=7.1, currency="RUB")
GONE = Bond("RU000A0ZYQY7", "RU000A0ZYQY7", 'АФК "Система" ПАО БО-001P-07', traded=False)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Moex:
    """Stands in for MOEX: the traded list, its search, and the cards."""

    def __init__(self) -> None:
        self.listed = [SISTEMA_14, OFZ, SISTEMA_6]
        self.loads = 0
        self.searches: list[str] = []
        self.list_down = self.search_down = False

    def load(self) -> list[Bond]:
        self.loads += 1
        if self.list_down:
            raise OSError("timed out")
        return list(self.listed)

    def search(self, q: str) -> list[Bond]:
        self.searches.append(q)
        if self.search_down:
            raise OSError("timed out")
        return [GONE] if "систем" in q.lower() or q == GONE.isin else []

    def describe(self, code: str) -> Bond | None:
        if self.search_down:
            raise OSError("timed out")
        return GONE if code == GONE.isin else None


def _directory(moex: _Moex | None = None, clock: _Clock | None = None) -> BondDirectory:
    moex = moex or _Moex()
    return BondDirectory(now=clock or _Clock(), load=moex.load, moex_search=moex.search,
                         describe=moex.describe)


def test_search_key_folds_lookalike_letters_and_leading_zeros() -> None:
    assert search_key("002Р-14") == search_key("2p14")  # Cyrillic Р, Latin p
    assert search_key("БО-001P-06") == search_key("бо 1P6")
    assert search_key("ОФЗ 26238") == search_key("офз26238")
    assert search_key("Ёлка") == search_key("елка")


@pytest.mark.parametrize("query, expected", [
    ("Система 1P-6", [SISTEMA_6]),  # the issuer and the series in any form
    ("АФК Система, 001P-06", [SISTEMA_6]),  # cbonds' "issuer, series"
    ("система", [SISTEMA_6, SISTEMA_14]),  # every bond of an issuer, the nearest maturity first
    ("2P-14", [SISTEMA_14]),
    ("RU000A0JXN21", [SISTEMA_6]),  # an ISIN
    ("SU26238RMFS4", [OFZ]),  # a MOEX code
    ("RU000A1038V6", [OFZ]),  # an OFZ by its ISIN
    ("офз 26238", [OFZ]),
    ("AFK Systema", [SISTEMA_6]),  # the Latin name too
])
def test_every_word_must_match_the_name_or_codes(query, expected) -> None:
    assert _directory().search(query) == expected


def test_an_exact_code_comes_first() -> None:
    moex = _Moex()
    # a bond whose name happens to contain another one's code still ranks below that bond
    decoy = Bond("RU000A000001", "RU000A000001", "Заём RU000A10FA72", matdate=date(2026, 1, 1))
    moex.listed.append(decoy)
    assert _directory(moex).search("RU000A10FA72") == [SISTEMA_14, decoy]


def test_the_list_is_loaded_once_and_kept() -> None:
    moex, clock = _Moex(), _Clock()
    d = _directory(moex, clock)
    d.search("система")
    d.search("офз")
    assert moex.loads == 1
    clock.t += 6 * 3600 + 1
    d.search("офз")
    assert moex.loads == 2


def test_only_a_search_with_no_traded_match_asks_moex() -> None:
    moex = _Moex()
    d = _directory(moex)
    d.search("Система 1P-6")
    assert moex.searches == []
    # matured: not on the traded list; MOEX gets the longest word, the rest filters its answer
    assert d.search("Система 1P-7") == [GONE]
    assert moex.searches == ["Система"]
    d.search("Система 1P-7")
    assert moex.searches == ["Система"]  # remembered
    assert d.search("ab") == []  # too short for MOEX's search
    assert moex.searches == ["Система"]


def test_a_failed_list_is_retried_after_a_minute_and_a_stale_one_is_kept() -> None:
    moex, clock = _Moex(), _Clock()
    d = _directory(moex, clock)
    moex.list_down = True
    # no list yet: the MOEX search still finds what it can …
    assert d.search("Система 1P-7") == [GONE]
    # … and with that down too, the page is told MOEX is unavailable
    moex.search_down = True
    with pytest.raises(MoexUnavailable):
        d.search("офз 26238")
    loads = moex.loads
    clock.t += 30
    with pytest.raises(MoexUnavailable):
        d.search("xy")
    assert moex.loads == loads  # not asked again within the minute
    moex.list_down = moex.search_down = False
    clock.t += 31
    assert d.search("офз") == [OFZ]
    # later, when MOEX fails to send a fresh list, the old one keeps the search working
    moex.list_down = True
    clock.t += 6 * 3600 + 1
    assert d.search("офз") == [OFZ]


def test_lookup_finds_traded_and_untraded_bonds_by_either_code() -> None:
    moex = _Moex()
    d = _directory(moex)
    assert d.lookup("ru000a1038v6") == OFZ and d.lookup("SU26238RMFS4") == OFZ
    assert d.lookup(GONE.isin) == GONE  # not traded: MOEX's card
    assert d.lookup("RU000XXXXXXX") is None and d.lookup("  ") is None
    moex.search_down = True
    with pytest.raises(MoexUnavailable):
        d.lookup("RU000XXXXXXX")
    assert d.lookup("RU000A0JXN21") == SISTEMA_6  # the cached list needs no MOEX


def test_a_bond_is_known_by_either_code() -> None:
    assert OFZ.code == "RU000A1038V6"
    assert OFZ.known_as({"SU26238RMFS4"}) and OFZ.known_as({"RU000A1038V6"})
    assert not OFZ.known_as({"RU000A0JXN21"})
