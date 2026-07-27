"""The verdict logic in isolation — the piece that decides whether a short put may be sold.

The subtle case is a symbol with no calendar row: safe to call CLEAN only when the sweep is
vouched to cover the contract's window, and UNKNOWN otherwise. Conflating those two is what put
an earnings-spanning contract in front of a user (issue #113).
"""

from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.core.earnings import EarningsGuard
from wheel_screener.core.models import EarningsPolicy, EarningsStatus

TODAY = date(2026, 7, 27)
EXPIRY = date(2026, 8, 21)


def _guard(dates=None, **kw) -> EarningsGuard:
    kw.setdefault("buffer_days", 2)
    return EarningsGuard(dates or {}, TODAY, **kw)


def test_report_before_expiry_spans() -> None:
    g = _guard({"RDDT": date(2026, 7, 30)})
    assert g.status("RDDT", EXPIRY) is EarningsStatus.SPANS
    assert g.blocks("RDDT", EXPIRY)


def test_report_after_expiry_is_clean() -> None:
    g = _guard({"ADBE": date(2026, 9, 10)})
    assert g.status("ADBE", EXPIRY) is EarningsStatus.CLEAN
    assert not g.blocks("ADBE", EXPIRY)


def test_buffer_extends_the_danger_zone_past_expiry() -> None:
    """A date just past expiry still counts: unconfirmed dates drift, and earlier as easily
    as later."""
    assert _guard({"X": EXPIRY + timedelta(days=2)}).status("X", EXPIRY) is EarningsStatus.SPANS
    assert _guard({"X": EXPIRY + timedelta(days=3)}).status("X", EXPIRY) is EarningsStatus.CLEAN
    # ...and with no buffer configured, only on-or-before expiry counts
    no_buf = EarningsGuard({"X": EXPIRY + timedelta(days=1)}, TODAY, buffer_days=0)
    assert no_buf.status("X", EXPIRY) is EarningsStatus.CLEAN


def test_absent_from_a_covering_sweep_is_clean() -> None:
    """The positive fact that makes a narrow sweep sufficient."""
    g = _guard(covers_through=EXPIRY + timedelta(days=2))
    assert g.status("NEM", EXPIRY) is EarningsStatus.CLEAN
    assert not g.blocks("NEM", EXPIRY)


def test_absent_beyond_what_the_sweep_covers_is_unknown_not_clean() -> None:
    """One day short of the contract's window and the answer is no longer established."""
    g = _guard(covers_through=EXPIRY + timedelta(days=1))  # buffer needs expiry+2
    assert g.status("NEM", EXPIRY) is EarningsStatus.UNKNOWN
    assert g.blocks("NEM", EXPIRY)  # fail closed


def test_absent_with_no_coverage_claim_is_unknown() -> None:
    """A single-symbol lookup vouches for nothing, so silence must not read as safe."""
    g = _guard()
    assert g.status("NEM", EXPIRY) is EarningsStatus.UNKNOWN
    assert g.blocks("NEM", EXPIRY)
    assert not _guard(exclude_unknown=False).blocks("NEM", EXPIRY)  # opt out -> keep + flag


def test_flag_policy_never_blocks() -> None:
    """Search marks risky expiries instead of hiding them."""
    g = _guard({"RDDT": date(2026, 7, 30)}, policy=EarningsPolicy.FLAG)
    assert g.status("RDDT", EXPIRY) is EarningsStatus.SPANS
    assert not g.blocks("RDDT", EXPIRY)


def test_off_policy_blocks_nothing() -> None:
    g = _guard({"RDDT": date(2026, 7, 30)}, policy=EarningsPolicy.OFF)
    assert not g.blocks("RDDT", EXPIRY)
    assert not g.blocks_every_expiry("RDDT", EXPIRY)


def test_blocks_every_expiry_only_when_no_expiry_can_be_clean() -> None:
    """The name-level shortcut: skip the chain pull only when even the nearest expiry we'd
    consider is already past the report."""
    earliest = date(2026, 8, 3)
    g = _guard({"A": date(2026, 8, 1), "B": date(2026, 8, 20)})
    assert g.blocks_every_expiry("A", earliest)  # reports before the nearest expiry
    assert not g.blocks_every_expiry("B", earliest)  # later expiries are dirty, near one is fine
    assert not g.blocks_every_expiry("C", earliest)  # unknown names still get their chain pulled
