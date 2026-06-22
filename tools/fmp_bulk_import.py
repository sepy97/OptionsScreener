#!/usr/bin/env python3
"""Standalone FMP bulk importer — one-time whole-market fundamentals download.

Downloads FMP `/stable/*-bulk` CSV datasets once and writes them into one or more data
directories (e.g. the screener's and pythonBot's). No third-party dependencies, so it
runs anywhere with stock Python 3.

Bandwidth-friendly: each dataset is downloaded ONCE (to the first --out dir) and then
local-copied to the others. Resumable: existing files are skipped.

Key resolution: --api-key, else $FMP_BULK_API_KEY, else `FMP_BULK_API_KEY=...` in --env.

Example:
    python3 tools/fmp_bulk_import.py \
        --out data/fundamentals \
        --out /Users/me/dev/pythonBot/data \
        --years 2015-2025 --period annual
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://financialmodelingprep.com/stable"

# Bulk endpoints have a short-window rate limit (HTTP 429 "Limit Reach"); retry with backoff.
RETRY_CODES = {429, 500, 502, 503, 504}

# one CSV each (latest snapshot)
SNAPSHOTS = ["ratios-ttm-bulk", "key-metrics-ttm-bulk", "scores-bulk", "dcf-bulk"]
# one CSV per fiscal year (looped over --years)
YEARLY = [
    "income-statement-bulk",
    "balance-sheet-statement-bulk",
    "cash-flow-statement-bulk",
    "ratios-bulk",
    "key-metrics-bulk",
]


def load_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    if os.environ.get("FMP_BULK_API_KEY"):
        return os.environ["FMP_BULK_API_KEY"]
    envf = Path(args.env)
    if envf.exists():
        m = re.search(r"FMP_BULK_API_KEY=(\S+)", envf.read_text())
        if m:
            return m.group(1)
    sys.exit("error: no bulk key (set FMP_BULK_API_KEY, pass --api-key, or add it to .env)")


def fetch_to(url: str, dest: Path, label: str, retries: int = 6, base_wait: int = 12) -> int | None:
    """Stream a URL to dest atomically; retry 429/5xx with linear backoff.

    Returns bytes written, or None on a non-retryable error / exhausted retries.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fmp-bulk-import/1.0"})
            total = 0
            with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
            tmp.rename(dest)
            return total
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            if e.code in RETRY_CODES and attempt < retries:
                wait = base_wait * (attempt + 1)
                print(f"   . {label}: HTTP {e.code}; backing off {wait}s (retry {attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            body = e.read()[:120].decode("utf-8", "replace").replace("\n", " ")
            print(f"   ! {label}: HTTP {e.code} {body}")
            return None
        except Exception as e:  # noqa: BLE001 - best-effort importer, log and skip
            tmp.unlink(missing_ok=True)
            print(f"   ! {label}: {type(e).__name__}: {e}")
            return None
    return None


def _is_empty_csv(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(4096).count(b"\n") <= 1  # header-only / empty -> end of pagination


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", action="append", required=True, help="output dir (repeatable)")
    ap.add_argument("--years", default="2015-2025", help="inclusive fiscal-year range, e.g. 2015-2025")
    ap.add_argument("--period", default="annual", choices=["annual", "quarter"])
    ap.add_argument("--api-key")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--max-parts", type=int, default=200, help="safety cap for profile-bulk pages")
    args = ap.parse_args()

    key = load_key(args)
    y0, y1 = (int(x) for x in args.years.split("-"))
    outdirs = [Path(os.path.expanduser(d)) for d in args.out]
    for d in outdirs:
        d.mkdir(parents=True, exist_ok=True)
    primary, mirrors = outdirs[0], outdirs[1:]

    def url(path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{BASE}/{path}{sep}apikey={key}"

    def mirror(fname: str) -> None:
        src = primary / fname
        for d in mirrors:
            tgt = d / fname
            if src.exists() and not tgt.exists():
                tgt.write_bytes(src.read_bytes())

    def get(path: str, fname: str) -> None:
        dest = primary / fname
        if dest.exists():
            print(f"   = {fname} (cached)")
        else:
            n = fetch_to(url(path), dest, fname)
            if n is None:
                return
            print(f"   + {fname}  {n / 1e6:.2f} MB")
        mirror(fname)

    print("== snapshots ==")
    for ds in SNAPSHOTS:
        get(ds, f"{ds}.csv")

    print("== profiles (paginated) ==")
    for part in range(args.max_parts):
        fname = f"profile-bulk_part{part}.csv"
        dest = primary / fname
        if not dest.exists():
            n = fetch_to(url(f"profile-bulk?part={part}"), dest, fname)
            if n is None:
                break
            if _is_empty_csv(dest):
                dest.unlink()
                print(f"   (end of profiles at part {part})")
                break
            print(f"   + {fname}  {n / 1e6:.2f} MB")
        mirror(fname)

    print(f"== yearly {y0}-{y1} ({args.period}) ==")
    for ds in YEARLY:
        for yr in range(y0, y1 + 1):
            get(f"{ds}?year={yr}&period={args.period}", f"{ds}_{yr}_{args.period}.csv")

    files = sorted(primary.glob("*.csv"))
    total = sum(p.stat().st_size for p in files)
    print(f"\ndone: {len(files)} files, {total / 1e6:.1f} MB in {primary}")
    for d in mirrors:
        print(f"      mirrored to {d}")


if __name__ == "__main__":
    main()
