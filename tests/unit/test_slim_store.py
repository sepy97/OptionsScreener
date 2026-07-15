from __future__ import annotations

import importlib.util
from pathlib import Path

# tools/ isn't an importable package — load the standalone script by path
_SPEC = importlib.util.spec_from_file_location(
    "slim_store", Path(__file__).parents[2] / "tools" / "slim_store.py"
)
slim_store = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slim_store)  # type: ignore[union-attr]


def _fake_store(root: Path) -> None:
    for part in ("part0", "part1"):
        (root / f"profile-bulk_{part}.csv").write_text("symbol\nAAA\n")
    for name in ("ratios-ttm-bulk.csv", "key-metrics-ttm-bulk.csv", "dcf-bulk.csv",
                 "overlay_metrics.csv"):
        (root / name).write_text("symbol\nAAA\n")
    for yr in range(2015, 2026):  # 2015..2025
        (root / f"income-statement-bulk_{yr}_annual.csv").write_text("x")
        (root / f"balance-sheet-statement-bulk_{yr}_annual.csv").write_text("x")
        (root / f"cash-flow-statement-bulk_{yr}_annual.csv").write_text("x")  # never read
        (root / f"key-metrics-bulk_{yr}_annual.csv").write_text("x")  # annual (not ttm) — unread


def test_select_runtime_files_mirrors_provider(tmp_path) -> None:
    _fake_store(tmp_path)
    selected = {p.name for p in slim_store.select_runtime_files(tmp_path, statement_years=3)}

    # profiles + TTM snapshots + overlay are kept whole
    assert {
        "profile-bulk_part0.csv", "profile-bulk_part1.csv", "ratios-ttm-bulk.csv",
        "key-metrics-ttm-bulk.csv", "dcf-bulk.csv", "overlay_metrics.csv",
    } <= selected
    # only the latest 3 fiscal years of income + balance statements
    assert "income-statement-bulk_2025_annual.csv" in selected
    assert "income-statement-bulk_2023_annual.csv" in selected
    assert "income-statement-bulk_2022_annual.csv" not in selected  # older than the window
    assert "balance-sheet-statement-bulk_2024_annual.csv" in selected
    assert sum(n.startswith("income-statement-bulk") for n in selected) == 3
    # never-read files are excluded
    assert not any(n.startswith("cash-flow") for n in selected)
    assert not any(n.startswith("key-metrics-bulk_") for n in selected)  # annual, not ttm


def test_slim_copies_subset_and_reports(tmp_path) -> None:
    src, out = tmp_path / "full", tmp_path / "slim"
    src.mkdir()
    _fake_store(src)
    kept, total = slim_store.slim(src, out, statement_years=3)
    assert 0 < kept < total  # a real reduction
    names = {p.name for p in out.glob("*.csv")}
    assert "ratios-ttm-bulk.csv" in names and not any(n.startswith("cash-flow") for n in names)


def test_slim_rejects_non_store(tmp_path) -> None:
    import pytest

    (tmp_path / "random.csv").write_text("x")  # no profile-bulk parts
    with pytest.raises(SystemExit, match="profile-bulk"):
        slim_store.slim(tmp_path, tmp_path / "out")
