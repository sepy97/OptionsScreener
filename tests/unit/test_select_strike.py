from __future__ import annotations

from datetime import date

from wheel_screener.core.models import OptionContract, OptionType
from wheel_screener.core.pipeline.select_strike import nearest_to_delta


def _put(strike: float, delta: float) -> OptionContract:
    return OptionContract(
        underlying_symbol="AAA",
        option_symbol=f"AAA{int(strike)}P",
        option_type=OptionType.PUT,
        expiration=date(2026, 8, 15),
        strike=strike,
        dte=40,
        delta=delta,
    )


def test_nearest_to_delta_picks_closest() -> None:
    chain = [_put(90, -0.10), _put(85, -0.18), _put(80, -0.25)]
    chosen = nearest_to_delta(chain, target_delta=-0.20)
    assert chosen is not None
    assert chosen.strike == 85


def test_nearest_to_delta_ignores_calls_and_missing_delta() -> None:
    call = OptionContract(
        underlying_symbol="AAA",
        option_symbol="AAA85C",
        option_type=OptionType.CALL,
        expiration=date(2026, 8, 15),
        strike=85,
        dte=40,
        delta=-0.20,  # exact match but wrong type -> ignored
    )
    no_delta = _put(84, delta=None)  # type: ignore[arg-type]
    chain = [call, no_delta, _put(80, -0.25)]
    chosen = nearest_to_delta(chain, target_delta=-0.20)
    assert chosen is not None
    assert chosen.strike == 80


def test_nearest_to_delta_empty() -> None:
    assert nearest_to_delta([], target_delta=-0.20) is None
