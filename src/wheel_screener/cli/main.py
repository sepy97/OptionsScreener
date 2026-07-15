"""Typer entry point. Commands call ScreenerService; rendering is the only logic here."""

from __future__ import annotations

import csv
import functools
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import TypeVar

import typer

from wheel_screener.composition import build_service
from wheel_screener.config import Settings
from wheel_screener.core.errors import AuthExpiredError, ProviderError, RateLimitedError
from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.logging_config import configure_logging

_F = TypeVar("_F", bound=Callable[..., object])
_state = {"debug": False}  # set by the --debug global flag; read by the top-level catch-all


def _provider_error_exit(e: ProviderError) -> None:
    """Map a data-provider failure to a clear CLI message + non-zero exit."""
    if isinstance(e, AuthExpiredError):
        typer.echo("error: Schwab auth missing/expired — run `wheel-screener auth-login`.")
    elif isinstance(e, RateLimitedError):
        typer.echo("error: data-provider rate limit hit — wait a minute and retry.")
    else:
        typer.echo(f"error: data-provider failure: {e}")
    raise typer.Exit(code=1)


def handle_provider_errors(func: _F) -> _F:
    """Wrap a command so any ProviderError becomes a friendly message + exit (not a trace).

    ``functools.wraps`` preserves the signature so Typer still sees the command's options.
    """

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except ProviderError as e:
            _provider_error_exit(e)

    return wrapper  # type: ignore[return-value]

app = typer.Typer(
    help="Cash-secured-put / wheel options screener.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main_callback(
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="-v progress, -vv per-symbol debug."
    ),
    debug: bool = typer.Option(False, "--debug", help="Show full tracebacks on unexpected errors."),
) -> None:
    _state["debug"] = debug
    configure_logging(verbose, Settings().log)


def _write_csv(names: list[Underlying], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["rank", "symbol", "sector", "price", "market_cap",
             "fundamental_score", "valuation", "efficiency", "sustainability"]
        )
        for i, u in enumerate(names, start=1):
            cats = u.rating.category_scores if u.rating else {}
            writer.writerow([
                i, u.symbol, u.sector or "", u.price or "", u.market_cap or "",
                round(u.fundamental_score or 0.0, 4),
                round(cats.get("valuation", 0.0), 4),
                round(cats.get("efficiency", 0.0), 4),
                round(cats.get("sustainability", 0.0), 4),
            ])


@app.command()
@handle_provider_errors
def screen(
    min_price: float = typer.Option(20.0, help="Minimum share price."),
    max_price: float = typer.Option(200.0, help="Maximum share price."),
    min_market_cap: float = typer.Option(0.0, help="Minimum market cap (0 = off)."),
    top_n: int = typer.Option(50, help="Keep the top N by fundamental rank."),
    source: str = typer.Option("local", help="Source: 'local' (bulk CSVs) or 'live' (FMP)."),
    output: str = typer.Option("candidates.csv", help="CSV output path."),
) -> None:
    """Rank the universe on fundamentals and write the ranked names to CSV.

    (M1 — fundamentals only; option selection by yield arrives in M2.)
    """
    settings = Settings()
    settings.fundamentals_source = source

    if source == "live":
        if not settings.fmp.api_key.get_secret_value():
            typer.echo("error: --source live needs FMP__API_KEY in your environment or .env.")
            raise typer.Exit(code=1)
        prerank_keep = 150  # bound the expensive per-symbol deep fetch
    else:  # local
        if not list(Path(settings.data_dir).glob("profile-bulk_part*.csv")):
            typer.echo(f"error: no bulk store in {settings.data_dir}; run tools/fmp_bulk_import.py")
            raise typer.Exit(code=1)
        if not settings.fmp.api_key.get_secret_value():
            typer.echo("note: no FMP__API_KEY → earnings blackout disabled.")
        prerank_keep = 1_000_000  # local is free; rank the whole filtered universe

    criteria = ScreenCriteria(
        min_price=min_price,
        max_price=max_price,
        min_market_cap=min_market_cap,
        top_n=top_n,
        prerank_keep=prerank_keep,
    )
    ranked = build_service(settings).screen_fundamentals(criteria, date.today())
    _write_csv(ranked, output)
    typer.echo(f"Wrote {len(ranked)} ranked names to {output}")


def _write_candidates_csv(results, path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rank", "symbol", "strike", "expiration", "dte", "delta", "iv", "bid", "mid",
            "open_interest", "annualized_yield", "collateral", "fundamental_score", "score",
        ])
        for i, r in enumerate(results, start=1):
            c = r.contract
            writer.writerow([
                i, r.symbol, c.strike, c.expiration.isoformat() if c.expiration else "", c.dte,
                round(c.delta, 3) if c.delta is not None else "",
                round(c.implied_volatility, 3) if c.implied_volatility is not None else "",
                c.bid if c.bid is not None else "",  # credited (conservative)
                round(c.mid, 2) if c.mid is not None else "",  # midpoint, reference only
                c.open_interest if c.open_interest is not None else "",
                round(r.annualized_yield, 4) if r.annualized_yield else "",
                r.collateral or "",
                round(r.fundamental_score, 4) if r.fundamental_score is not None else "",
                round(r.score, 4) if r.score is not None else "",
            ])


@app.command("refresh-earnings")
@handle_provider_errors
def refresh_earnings(
    days: int = typer.Option(120, help="Days ahead to fetch earnings for."),
) -> None:
    """Refresh the local earnings calendar from FMP (one cheap call) — powers the blackout."""
    from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider

    settings = Settings()
    if not settings.fmp.api_key.get_secret_value():
        typer.echo("error: set FMP__API_KEY in .env first.")
        raise typer.Exit(code=1)
    today = date.today()
    calendar = FmpFundamentalsProvider(settings.fmp).earnings_calendar(
        today, today + timedelta(days=days)
    )
    path = Path(settings.earnings_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "date"])
        for symbol, when in sorted(calendar.items()):
            writer.writerow([symbol, when.isoformat()])
    typer.echo(f"Wrote {len(calendar)} symbols' next earnings to {path}")


@app.command("refresh-fundamentals")
@handle_provider_errors
def refresh_fundamentals(
    days: int = typer.Option(7, help="Refresh names that reported in the last N days."),
) -> None:
    """Re-fetch TTM fundamentals for names that just reported (per the FMP earnings calendar)
    into the local overlay — a cheap incremental alternative to a full bulk reload."""
    from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
    from wheel_screener.adapters.local.overlay import write_overlay
    from wheel_screener.adapters.local.provider import LocalFundamentalsProvider

    settings = Settings()
    if not settings.fmp.api_key.get_secret_value():
        typer.echo("error: set FMP__API_KEY in .env first.")
        raise typer.Exit(code=1)
    if not list(Path(settings.data_dir).glob("profile-bulk_part*.csv")):
        typer.echo(f"error: no bulk store in {settings.data_dir}; run tools/fmp_bulk_import.py")
        raise typer.Exit(code=1)

    today = date.today()
    fmp = FmpFundamentalsProvider(settings.fmp)
    reporters = fmp.earnings_calendar(today - timedelta(days=days), today)
    targets = sorted(set(reporters) & LocalFundamentalsProvider(settings.data_dir).known_symbols())
    if not targets:
        typer.echo("no recent reporters found in the local store; nothing to refresh.")
        return
    typer.echo(f"Refreshing {len(targets)} reporters from the last {days}d…")
    fresh = fmp.fetch_metrics(targets)
    total = write_overlay(settings.data_dir, fresh)
    typer.echo(f"Refreshed {len(fresh)} symbols into the overlay ({total} rows total).")


@app.command()
@handle_provider_errors
def search(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL."),
    top_n: int = typer.Option(5, help="How many puts to show (nearest the target delta)."),
    min_dte: int = typer.Option(7, help="Minimum days to expiration."),
    max_dte: int = typer.Option(45, help="Maximum days to expiration."),
    target_delta: float = typer.Option(-0.20, help="Target put delta."),
) -> None:
    """Top-N cash-secured puts to sell on ONE ticker (any optionable symbol; skips the funnel)."""
    settings = Settings()
    token = Path(settings.schwab.token_path).expanduser()
    if settings.chain_source == "schwab" and not token.exists():
        typer.echo("error: no Schwab token; run `wheel-screener auth-login` first.")
        raise typer.Exit(code=1)
    criteria = ScreenCriteria(min_dte=min_dte, max_dte=max_dte, target_delta=target_delta)
    _print_search(build_service(settings).search_ticker(ticker, criteria, date.today(), n=top_n))


def _print_search(r: object) -> None:
    fund = (
        "n/a" if r.passes_fundamentals is None
        else "pass" if r.passes_fundamentals else "FAIL (" + ", ".join(r.gate_reasons) + ")"
    )
    parts = []
    if r.metrics:
        if r.metrics.pe is not None:
            parts.append(f"P/E {r.metrics.pe:.1f}")
        if r.metrics.roe is not None:
            parts.append(f"ROE {r.metrics.roe * 100:.0f}%")
        if r.metrics.fcf_yield is not None:
            parts.append(f"FCF {r.metrics.fcf_yield * 100:.1f}%")
    metr = " · " + " · ".join(parts) if parts else ""
    earn = f" · next earnings {r.next_earnings}" if r.next_earnings else ""
    typer.echo(f"{r.symbol}: {len(r.puts)} sellable put(s) · fundamentals {fund}{metr}{earn}")
    if not r.puts:
        typer.echo("  nothing in the DTE / delta / liquidity window.")
        return
    typer.echo(
        f"  {'strike':>7} {'exp':>10} {'dte':>4} {'delta':>6} {'iv':>5} "
        f"{'bid':>5} {'yield':>6} {'oi':>7} {'b/e':>8}"
    )
    for c in r.puts:
        k = c.contract
        iv = f"{k.implied_volatility * 100:.0f}%" if k.implied_volatility is not None else "-"
        be = k.strike - (c.premium or 0.0)
        earns_before = r.next_earnings and r.next_earnings <= k.expiration
        flag = " <- earnings before exp" if earns_before else ""
        yld = (c.annualized_yield or 0.0) * 100
        typer.echo(
            f"  {k.strike:>7.2f} {k.expiration!s:>10} {k.dte:>4} {k.delta:>6.2f} {iv:>5} "
            f"{k.bid:>5.2f} {yld:>5.1f}% {k.open_interest or 0:>7} {be:>8.2f}{flag}"
        )


@app.command("refresh-screen")
@handle_provider_errors
def refresh_screen(
    top_n: int = typer.Option(250, help="Fundamental survivors to pull chains for."),
    min_dollar_volume: float = typer.Option(25_000_000.0, help="Avg daily $-volume floor."),
    fundamental_weight: float = typer.Option(0.5, help="Rank blend: 1=fundamentals, 0=yield."),
    min_yield: float = typer.Option(0.0, help="Drop candidates below this annualized yield."),
) -> None:
    """Run a screen and store it where the web dashboard reads 'latest results' — so a cron'd
    run keeps the UI instant (precompute), instead of the user waiting on a live pull."""
    from wheel_screener.api.jobs import JobRunner, JobStore

    settings = Settings()
    criteria = ScreenCriteria(
        top_n=top_n, prerank_keep=1_000_000, min_dollar_volume=min_dollar_volume,
        fundamental_weight=fundamental_weight,
        min_annualized_yield=(min_yield if min_yield > 0 else None),
    )
    runner = JobRunner(build_service(settings), JobStore(settings.jobs_db_path))
    job = runner.get(runner.run_blocking(criteria))
    n = len(job.get("result") or [])
    typer.echo(f"Stored screen ({job['status']}, {n} candidates) — the dashboard now shows it.")


@app.command("auth-login")
def auth_login() -> None:
    """Run the Schwab OAuth login in your browser (refresh token expires every 7 days)."""
    from wheel_screener.adapters.schwab.auth import login

    settings = Settings()
    if not settings.schwab.client_id or not settings.schwab.client_secret.get_secret_value():
        typer.echo("error: set SCHWAB__CLIENT_ID and SCHWAB__CLIENT_SECRET in .env first.")
        raise typer.Exit(code=1)
    typer.echo("Opening Schwab login… approve access; you'll be redirected to the callback.")
    login(settings.schwab)
    typer.echo(f"Saved token to {settings.schwab.token_path} (re-run weekly).")


@app.command()
@handle_provider_errors
def candidates(
    min_price: float = typer.Option(20.0, help="Minimum share price."),
    max_price: float = typer.Option(200.0, help="Maximum share price."),
    min_market_cap: float = typer.Option(0.0, help="Minimum market cap (0 = off)."),
    min_dollar_volume: float = typer.Option(
        25_000_000.0, help="Skip stocks below this avg daily $-volume (0 = off)."
    ),
    top_n: int = typer.Option(250, help="Fundamental survivors to pull chains for."),
    min_yield: float = typer.Option(0.0, help="Drop candidates below this annualized yield."),
    fundamental_weight: float = typer.Option(
        0.5, help="Rank blend: 1.0 = all fundamentals, 0.0 = all yield."
    ),
    timeout: float = typer.Option(
        0.0, help="Wall-clock budget (s) for the chain pull; 0 = unbounded. Partial on timeout."
    ),
    output: str = typer.Option("candidates.csv", help="CSV output path."),
) -> None:
    """Full pipeline: fundamentals (local store) → Schwab chains → ~−0.20Δ put → blended rank."""
    settings = Settings()  # local fundamentals + Schwab chains
    if not list(Path(settings.data_dir).glob("profile-bulk_part*.csv")):
        typer.echo(f"error: no bulk store in {settings.data_dir}; run tools/fmp_bulk_import.py")
        raise typer.Exit(code=1)
    if not Path(settings.schwab.token_path).expanduser().exists():
        typer.echo("error: no Schwab token; run `wheel-screener auth-login` first.")
        raise typer.Exit(code=1)

    criteria = ScreenCriteria(
        min_price=min_price, max_price=max_price, min_market_cap=min_market_cap,
        min_dollar_volume=min_dollar_volume,
        top_n=top_n, prerank_keep=1_000_000,
        min_annualized_yield=(min_yield if min_yield > 0 else None),
        fundamental_weight=fundamental_weight,
        max_runtime_seconds=(timeout if timeout > 0 else None),
    )
    results = build_service(settings).run_screen(criteria, date.today())
    _write_candidates_csv(results, output)
    typer.echo(f"Wrote {len(results)} candidates to {output}")


def _report_unexpected(e: Exception, *, debug: bool) -> None:
    """Top-level safety net: turn an unexpected error into a one-line message + exit 1
    (no raw traceback), unless --debug asked to see it."""
    typer.echo(f"error: unexpected failure: {e}", err=True)
    if debug:
        raise e
    raise SystemExit(1)


def main() -> None:
    try:
        app()
    except (KeyboardInterrupt, SystemExit):
        raise  # normal control flow / Typer exits — let them through unchanged
    except Exception as e:  # noqa: BLE001 - deliberate top-level catch-all (no raw tracebacks)
        _report_unexpected(e, debug=_state["debug"])


if __name__ == "__main__":
    main()
