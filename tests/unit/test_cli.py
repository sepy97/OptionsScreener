from __future__ import annotations

import inspect

import pytest
import typer
from typer.testing import CliRunner

from wheel_screener.cli.main import (
    _report_unexpected,
    app,
    handle_provider_errors,
    main,
)
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
    # introspect the registered commands' params (robust vs. rich-rendered help text)
    from typer.main import get_command

    commands = get_command(app).commands  # name -> click Command
    assert "top_n" in {p.name for p in commands["candidates"].params}
    assert "days" in {p.name for p in commands["refresh-fundamentals"].params}
    assert runner.invoke(app, ["--help"]).exit_code == 0  # app still builds + renders


def test_report_unexpected_clean_exit(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        _report_unexpected(ValueError("boom"), debug=False)
    assert ei.value.code == 1
    assert "unexpected failure" in capsys.readouterr().err  # message, no traceback


def test_report_unexpected_debug_reraises() -> None:
    with pytest.raises(ValueError):  # --debug surfaces the original for diagnosis
        _report_unexpected(ValueError("boom"), debug=True)


def test_main_catches_unexpected_exception(monkeypatch, capsys) -> None:
    def _boom() -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("wheel_screener.cli.main.app", _boom)
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 1
    assert "unexpected failure" in capsys.readouterr().err


def test_main_passes_through_system_exit(monkeypatch) -> None:
    def _exit() -> None:
        raise SystemExit(2)

    monkeypatch.setattr("wheel_screener.cli.main.app", _exit)
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 2  # Typer/Click exits are not swallowed or remapped


def test_debug_is_a_global_option() -> None:
    from typer.main import get_command

    assert "debug" in {p.name for p in get_command(app).params}
