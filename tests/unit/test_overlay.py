from __future__ import annotations

from pathlib import Path

from wheel_screener.adapters.local.overlay import read_overlay, write_overlay
from wheel_screener.core.models import FundamentalMetrics


def test_overlay_round_trip_and_merge(tmp_path: Path) -> None:
    assert read_overlay(str(tmp_path)) == {}  # absent file -> empty

    assert write_overlay(str(tmp_path), {"AAA": FundamentalMetrics(pe=10.0, roe=0.2)}) == 1
    # second write updates AAA and adds BBB (merge, not overwrite)
    write_overlay(str(tmp_path), {
        "AAA": FundamentalMetrics(pe=12.0, fcf_yield=0.05),
        "BBB": FundamentalMetrics(pe=8.0),
    })

    got = read_overlay(str(tmp_path))
    assert set(got) == {"AAA", "BBB"}
    assert got["AAA"].pe == 12.0 and got["AAA"].fcf_yield == 0.05  # updated
    assert got["BBB"].pe == 8.0  # added
