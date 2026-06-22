"""Typer entry point. Commands call ScreenerService; rendering is the only logic here."""

from __future__ import annotations

import csv
from datetime import date

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
             "fundamental_score", "value", "quality", "safety"]
        )
        for i, u in enumerate(names, start=1):
            cats = u.rating.category_scores if u.rating else {}
            writer.writerow([
                i, u.symbol, u.sector or "", u.price or "", u.market_cap or "",
                round(u.fundamental_score or 0.0, 4),
                round(cats.get("value", 0.0), 4),
                round(cats.get("quality", 0.0), 4),
                round(cats.get("safety", 0.0), 4),
            ])


@app.command()
def screen(
    min_price: float = typer.Option(20.0, help="Minimum share price."),
    max_price: float = typer.Option(200.0, help="Maximum share price."),
    min_market_cap: float = typer.Option(2_000_000_000.0, help="Minimum market cap."),
    top_n: int = typer.Option(50, help="Keep the top N by fundamental rank."),
    universe_limit: int = typer.Option(
        50, help="Deep-fetch cap (by market cap) when bulk pre-rank is unavailable."
    ),
    output: str = typer.Option("candidates.csv", help="CSV output path."),
) -> None:
    """Rank the universe on fundamentals and write the ranked names to CSV.

    (M1 — fundamentals only; option selection by yield arrives in M2.)
    """
    settings = Settings()
    if not settings.fmp.api_key.get_secret_value():
        typer.echo("error: set FMP__API_KEY in your environment or .env first.")
        raise typer.Exit(code=1)

    criteria = ScreenCriteria(
        min_price=min_price,
        max_price=max_price,
        min_market_cap=min_market_cap,
        top_n=top_n,
        universe_limit=universe_limit,
    )
    ranked = build_service(settings).screen_fundamentals(criteria, date.today())
    _write_csv(ranked, output)
    typer.echo(f"Wrote {len(ranked)} ranked names to {output}")


@app.command("auth-login")
def auth_login() -> None:
    """Re-run the Schwab OAuth login (the refresh token expires every 7 days)."""
    typer.echo("auth-login: not yet implemented (Schwab OAuth lands in M2).")
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
