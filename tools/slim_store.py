#!/usr/bin/env python3
"""Copy just the runtime-needed subset of the bulk fundamentals store into a slim deploy dir.

The full store (``tools/fmp_bulk_import.py`` output) is ~2 GB — 10+ fiscal years of income /
balance / cash-flow statements. ``LocalFundamentalsProvider`` only ever reads a fraction of it:
the profile parts, the TTM snapshots (ratios / key-metrics / dcf), the latest few fiscal years of
the income + balance statements (coalesced per symbol), and the optional overlay. Cash-flow
statements, annual key-metrics, and statements older than that window are never opened.

This mirrors exactly what the provider reads, so the output is a drop-in store a few hundred MB
in size — small enough to rsync to the droplet and far lighter on the 2 GB box's memory (polars
loads these files). The selection rule imports the provider's own constants, so it can't drift.

    uv run python tools/slim_store.py --src data/fundamentals --out data/fundamentals-slim
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from wheel_screener.adapters.local.overlay import OVERLAY_FILENAME
from wheel_screener.adapters.local.provider import _STATEMENT_YEARS

# whole-file, one row per symbol (the provider reads these entirely)
_TTM_FILES = ("ratios-ttm-bulk.csv", "key-metrics-ttm-bulk.csv", "dcf-bulk.csv")
# multi-year statements: only the latest N fiscal years are ever read (coalesced per symbol)
_STATEMENT_PREFIXES = ("income-statement-bulk", "balance-sheet-statement-bulk")


def select_runtime_files(src: Path, statement_years: int = _STATEMENT_YEARS) -> list[Path]:
    """The exact files LocalFundamentalsProvider reads — mirrors its globs and year limit."""
    files: list[Path] = sorted(src.glob("profile-bulk_part*.csv"))
    for name in (*_TTM_FILES, OVERLAY_FILENAME):
        p = src / name
        if p.exists():
            files.append(p)
    for prefix in _STATEMENT_PREFIXES:  # newest N fiscal years (reverse-sorted, like the provider)
        files += sorted(src.glob(f"{prefix}_*.csv"), reverse=True)[:statement_years]
    return files


def _human(nbytes: int) -> str:
    mb = nbytes / 1_000_000
    return f"{mb / 1000:.2f} GB" if mb >= 1000 else f"{mb:.1f} MB"


def slim(src: Path, out: Path, statement_years: int = _STATEMENT_YEARS) -> tuple[int, int]:
    """Copy the runtime subset src -> out. Returns (kept_bytes, src_total_bytes)."""
    files = select_runtime_files(src, statement_years)
    if not any(f.name.startswith("profile-bulk_part") for f in files):
        raise SystemExit(f"no profile-bulk parts in {src} — is this a fundamentals store?")
    out.mkdir(parents=True, exist_ok=True)
    kept = 0
    for f in files:
        shutil.copy2(f, out / f.name)
        kept += f.stat().st_size
    src_total = sum(p.stat().st_size for p in src.glob("*.csv"))
    return kept, src_total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=Path("data/fundamentals"), type=Path, help="full store dir")
    ap.add_argument("--out", default=Path("data/fundamentals-slim"), type=Path, help="slim output")
    ap.add_argument("--statement-years", type=int, default=_STATEMENT_YEARS,
                    help="fiscal years of income/balance statements to keep (must match provider)")
    args = ap.parse_args()
    kept, total = slim(args.src, args.out, args.statement_years)
    n = len(list(args.out.glob("*.csv")))
    saved = f" ({100 * (1 - kept / total):.0f}% smaller)" if total else ""
    print(f"slim store: {n} files, {_human(kept)} from {_human(total)}{saved} -> {args.out}")
    print("note: ship data/earnings_calendar.csv too (separate small file, used for the blackout)")


if __name__ == "__main__":
    main()
