from __future__ import annotations

import math

import pytest

from wheel_screener.core.ranking import annualized_csp_yield


def test_annualized_yield_basic() -> None:
    # $2 premium on a $100 strike, 30 DTE
    y = annualized_csp_yield(premium=2.0, strike=100.0, dte=30)
    assert math.isclose(y, 0.02 * 365 / 30, rel_tol=1e-9)


@pytest.mark.parametrize("dte,strike", [(0, 100.0), (-5, 100.0), (30, 0.0), (30, -1.0)])
def test_annualized_yield_guards(dte: int, strike: float) -> None:
    with pytest.raises(ValueError):
        annualized_csp_yield(premium=2.0, strike=strike, dte=dte)
