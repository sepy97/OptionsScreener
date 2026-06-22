"""Typer entry point. Commands call ScreenerService; rendering is the only logic here."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import typer

from wheel_screener.composition import build_service
from wheel_screener.config import Settings
from wheel_screener.core.models import ScreenCriteria, Underlying

app = typer.Typer(
    help="Cash-secured-put / wheel options screener.",
    no_args_is_help=True,
    add_completion=False,
)


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
def screen(
    min_price: float = typer.Option(20.0, help="Minimum share price."),
    max_price: float = typer.Option(200.0, help="Maximum share price."),
    min_market_cap: float = typer.Option(2_000_000_000.0, help="Minimum market cap."),
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
            "rank", "symbol", "strike", "expiration", "dte", "delta", "iv", "bid",
            "open_interest", "premium", "annualized_yield", "collateral", "fundamental_score",
        ])
        for i, r in enumerate(results, start=1):
            c = r.contract
            writer.writerow([
                i, r.symbol, c.strike, c.expiration.isoformat() if c.expiration else "", c.dte,
                round(c.delta, 3) if c.delta is not None else "",
                round(c.implied_volatility, 3) if c.implied_volatility is not None else "",
                c.bid if c.bid is not None else "",
                c.open_interest if c.open_interest is not None else "",
                round(r.premium, 2) if r.premium else "",
                round(r.annualized_yield, 4) if r.annualized_yield else "",
                r.collateral or "",
                round(r.fundamental_score, 4) if r.fundamental_score is not None else "",
            ])


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
def candidates(
    min_price: float = typer.Option(20.0, help="Minimum share price."),
    max_price: float = typer.Option(200.0, help="Maximum share price."),
    min_market_cap: float = typer.Option(2_000_000_000.0, help="Minimum market cap."),
    top_n: int = typer.Option(50, help="Fundamental survivors to pull chains for."),
    min_yield: float = typer.Option(0.0, help="Drop candidates below this annualized yield."),
    output: str = typer.Option("candidates.csv", help="CSV output path."),
) -> None:
    """Full pipeline: fundamentals (local store) → Schwab chains → ~−0.20Δ put → yield rank."""
    settings = Settings()  # local fundamentals + Schwab chains
    if not list(Path(settings.data_dir).glob("profile-bulk_part*.csv")):
        typer.echo(f"error: no bulk store in {settings.data_dir}; run tools/fmp_bulk_import.py")
        raise typer.Exit(code=1)
    if not Path(settings.schwab.token_path).expanduser().exists():
        typer.echo("error: no Schwab token; run `wheel-screener auth-login` first.")
        raise typer.Exit(code=1)

    criteria = ScreenCriteria(
        min_price=min_price, max_price=max_price, min_market_cap=min_market_cap,
        top_n=top_n, prerank_keep=1_000_000,
        min_annualized_yield=(min_yield if min_yield > 0 else None),
    )
    results = build_service(settings).run_screen(criteria, date.today())
    _write_candidates_csv(results, output)
    typer.echo(f"Wrote {len(results)} candidates to {output}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
