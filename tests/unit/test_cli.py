from __future__ import annotations

import inspect

import pytest
import typer
from typer.testing import CliRunner

from wheel_screener.cli.main import app, handle_provider_errors
from wheel_screener.core.errors import AuthExpiredError, ProviderUnavailableError

runner = CliRunner()


def test_decorator_maps_auth_error_to_friendly_exit(capsys) -> None:
    @handle_provider_errors
    def boom() -> None:
        raise AuthExpiredError("token gone")

    with pytest.raises(typer.Exit) as ei:
        boom()
    assert ei.value.exit_code == 1
    assert "auth-login" in capsys.readouterr().out  # actionable message, not a traceback


def test_decorator_maps_generic_provider_error(capsys) -> None:
    @handle_provider_errors
    def boom() -> None:
        raise ProviderUnavailableError("down")

    with pytest.raises(typer.Exit):
        boom()
    assert "data-provider failure" in capsys.readouterr().out


def test_decorator_preserves_signature() -> None:
    # functools.wraps keeps the signature so Typer still builds the CLI options
    def f(days: int = 7, name: str = "x") -> None:
        return None

    wrapped = handle_provider_errors(f)
    assert list(inspect.signature(wrapped).parameters) == ["days", "name"]


def test_decorated_commands_still_expose_options() -> None:
    assert runner.invoke(app, ["--help"]).exit_code == 0
    out = runner.invoke(app, ["candidates", "--help"])
    assert out.exit_code == 0 and "--top-n" in out.output
    assert runner.invoke(app, ["refresh-fundamentals", "--help"]).exit_code == 0
