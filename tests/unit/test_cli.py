from __future__ import annotations

import inspect
import logging

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


@pytest.fixture(autouse=True)
def _restore_pkg_logger():
    """Keep the callback's configure_logging from leaking handlers into other tests."""
    lg = logging.getLogger("wheel_screener")
    snap = (lg.level, lg.propagate, lg.handlers[:])
    yield
    lg.setLevel(snap[0])
    lg.propagate = snap[1]
    lg.handlers[:] = snap[2]


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
    result = runner.invoke(app, ["--help"], env={"LOG__ENABLE_FILE": "false"})
    assert result.exit_code == 0  # app still builds + renders


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


def test_debug_and_verbose_are_global_options() -> None:
    from typer.main import get_command

    params = {p.name for p in get_command(app).params}
    assert "debug" in params and "verbose" in params


def test_refresh_screen_populates_the_dashboard_store(tmp_path, monkeypatch) -> None:
    from datetime import date

    from wheel_screener.api.jobs import JobStore
    from wheel_screener.core.models import CandidateResult, OptionContract, OptionType

    class _Svc:
        def run_screen(self, criteria, today, *, cancel=None):
            c = OptionContract(
                underlying_symbol="AAA", option_symbol="x", option_type=OptionType.PUT,
                expiration=date(2026, 8, 15), strike=80.0, dte=40, bid=1.0, ask=1.1,
            )
            return [CandidateResult(symbol="AAA", contract=c, score=0.5)]

    monkeypatch.setenv("JOBS_DB_PATH", str(tmp_path / "jobs.sqlite"))
    monkeypatch.setattr("wheel_screener.cli.main.build_service", lambda *a, **k: _Svc())
    result = runner.invoke(app, ["refresh-screen", "--top-n", "5"])
    assert result.exit_code == 0 and "candidates" in result.output
    latest = JobStore(str(tmp_path / "jobs.sqlite")).latest_done()  # what the web dashboard reads
    assert latest is not None and latest["result"][0]["symbol"] == "AAA"


def test_doctor_names_the_broken_connection(monkeypatch) -> None:
    """`doctor` is how this gets diagnosed on the droplet, where there is no browser."""
    from wheel_screener.cli import main as cli_main
    from wheel_screener.composition import Probe

    class _P:
        def __init__(self, detail):
            self.detail = detail

        def check_auth(self):
            return self.detail

    monkeypatch.setattr(cli_main, "build_service", lambda settings: object())
    monkeypatch.setattr(cli_main, "build_probes", lambda settings, service: [
        Probe("option chains", "alpaca", _P("Alpaca rejected our credentials (HTTP 401)")),
        Probe("fundamentals & earnings", "fmp", _P(None)),
    ])
    result = runner.invoke(app, ["doctor"], env={"LOG__ENABLE_FILE": "false"})
    assert result.exit_code == 1, "a broken connection must fail the command"
    assert "alpaca" in result.output and "401" in result.output
    assert "fmp" in result.output and "credentials accepted" in result.output


def test_doctor_is_clean_when_everything_answers(monkeypatch) -> None:
    from wheel_screener.cli import main as cli_main
    from wheel_screener.composition import Probe

    class _Ok:
        def check_auth(self):
            return None

    monkeypatch.setattr(cli_main, "build_service", lambda settings: object())
    monkeypatch.setattr(cli_main, "build_probes", lambda settings, service: [
        Probe("option chains", "alpaca", _Ok()),
    ])
    result = runner.invoke(app, ["doctor"], env={"LOG__ENABLE_FILE": "false"})
    assert result.exit_code == 0 and "healthy" in result.output


def test_doctor_reports_a_probe_that_raises(monkeypatch) -> None:
    from wheel_screener.cli import main as cli_main
    from wheel_screener.composition import Probe

    class _Boom:
        def check_auth(self):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(cli_main, "build_service", lambda settings: object())
    monkeypatch.setattr(cli_main, "build_probes", lambda settings, service: [
        Probe("option chains", "alpaca", _Boom()),
    ])
    result = runner.invoke(app, ["doctor"], env={"LOG__ENABLE_FILE": "false"})
    assert result.exit_code == 1 and "kaboom" in result.output
