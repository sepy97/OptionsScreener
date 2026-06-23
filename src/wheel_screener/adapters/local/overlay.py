"""Incremental overlay store.

`refresh-fundamentals` re-fetches fresh per-symbol metrics for names that just reported
and writes them here; `LocalFundamentalsProvider` merges this overlay ON TOP of the bulk
store (overlay wins). Keeps fundamentals current without rewriting the multi-GB bulk CSVs.
"""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl

from wheel_screener.core.models import FundamentalMetrics

OVERLAY_FILENAME = "overlay_metrics.csv"
_FIELDS = list(FundamentalMetrics.model_fields)


def _path(data_dir: str) -> Path:
    return Path(os.path.expanduser(data_dir)) / OVERLAY_FILENAME


def read_overlay(data_dir: str) -> dict[str, FundamentalMetrics]:
    path = _path(data_dir)
    if not path.exists():
        return {}
    df = pl.read_csv(path, infer_schema_length=0)
    floats = [c for c in df.columns if c != "symbol" and c in _FIELDS]
    df = df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in floats])
    out: dict[str, FundamentalMetrics] = {}
    for r in df.iter_rows(named=True):
        sym = r.get("symbol")
        if sym:
            out[sym] = FundamentalMetrics(**{k: v for k, v in r.items() if k in _FIELDS})
    return out


def write_overlay(data_dir: str, metrics: dict[str, FundamentalMetrics]) -> int:
    """Merge ``metrics`` into the overlay (update existing symbols, add new). Returns row count."""
    merged = read_overlay(data_dir)
    merged.update(metrics)
    rows = [{"symbol": sym, **m.model_dump()} for sym, m in sorted(merged.items())]
    schema = {"symbol": pl.Utf8, **{f: pl.Float64 for f in _FIELDS}}
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=schema).write_csv(path)
    return len(rows)
