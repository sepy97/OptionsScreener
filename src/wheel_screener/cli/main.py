"""Typer entry point. Commands call ScreenerService; rendering is the only logic here."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Cash-secured-put / wheel options screener.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def screen(
    min_iv_rank: float = typer.Option(70.0, help="Minimum IV rank / percentile."),
    target_delta: float = typer.Option(-0.20, help="Target short-put delta."),
    min_dte: int = typer.Option(30, help="Minimum days to expiration."),
    max_dte: int = typer.Option(45, help="Maximum days to expiration."),
    output: str = typer.Option("candidates.csv", help="CSV output path."),
) -> None:
    """Run the screener and write ranked candidates to CSV."""
    typer.echo("screen: not yet implemented (pipeline lands across M1-M4).")
    raise typer.Exit(code=1)


@app.command("auth-login")
def auth_login() -> None:
    """Re-run the Schwab OAuth login (the refresh token expires every 7 days)."""
    typer.echo("auth-login: not yet implemented (Schwab OAuth lands in M2).")
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
