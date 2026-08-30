from __future__ import annotations

from datetime import date

from wheel_screener.core.models import OptionType
from wheel_screener.core.osi import parse_osi


def test_parses_the_padded_symbol_a_broker_actually_sends() -> None:
    s = parse_osi("AAPL  260918P00190000")
    assert s is not None
    assert s.underlying == "AAPL" and s.option_type is OptionType.PUT
    assert s.expiration == date(2026, 9, 18)
    assert s.strike == 190.0, "the field is thousandths of a dollar, not dollars"


def test_an_adjusted_root_is_not_mangled() -> None:
    """Adjusted contracts (AAPL1, after a corporate action) are ordinary. A parser anchored to
    the LEFT eats a digit of the date here and produces a wrong expiry that still looks real."""
    s = parse_osi("AAPL1 260918C00190000")
    assert s is not None and s.underlying == "AAPL1"
    assert s.expiration == date(2026, 9, 18) and s.option_type is OptionType.CALL


def test_roots_of_every_length_land_on_the_same_expiry() -> None:
    for root in ("F", "GM", "AMD", "AAPL", "GOOGL", "BRKB"):
        padded = f"{root:<6}260918P00042500"
        s = parse_osi(padded)
        assert s is not None and s.underlying == root
        assert s.expiration == date(2026, 9, 18) and s.strike == 42.5


def test_fractional_and_large_strikes_survive_the_scale() -> None:
    assert parse_osi("SPY   260918P00007500").strike == 7.5
    assert parse_osi("NVDA  260918C09999000").strike == 9999.0
    assert parse_osi("AMZN  260918P00012345").strike == 12.345


def test_dte_counts_from_the_day_asked() -> None:
    s = parse_osi("AAPL  260918P00190000")
    assert s.dte(date(2026, 8, 29)) == 20
    assert s.dte(date(2026, 9, 18)) == 0
    assert s.dte(date(2026, 9, 19)) == -1  # expired yesterday, still held on the books


def test_a_non_option_row_is_not_an_error() -> None:
    """A position list mixes options with shares, funds and cash sweeps."""
    for plain in ("AAPL", "", "   ", "MMDA1", "SWVXX"):
        assert parse_osi(plain) is None


def test_an_impossible_date_is_refused_rather_than_guessed() -> None:
    assert parse_osi("AAPL  261332P00190000") is None  # month 13
    assert parse_osi("AAPL  260931P00190000") is None  # September has 30 days
