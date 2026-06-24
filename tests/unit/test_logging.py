from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from wheel_screener.config import LogSettings
from wheel_screener.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _restore_pkg_logger():
    """Snapshot/restore the package logger so configure_logging can't leak across tests."""
    lg = logging.getLogger("wheel_screener")
    snap = (lg.level, lg.propagate, lg.handlers[:])
    yield
    lg.setLevel(snap[0])
    lg.propagate = snap[1]
    lg.handlers[:] = snap[2]


def _console(lg: logging.Logger) -> logging.Handler:
    return next(
        h
        for h in lg.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    )


def test_console_level_follows_verbosity() -> None:
    lg = logging.getLogger("wheel_screener")
    configure_logging(0, LogSettings(enable_file=False))
    assert _console(lg).level == logging.WARNING  # quiet
    configure_logging(1, LogSettings(enable_file=False))
    assert _console(lg).level == logging.INFO  # -v
    configure_logging(2, LogSettings(enable_file=False))
    assert _console(lg).level == logging.DEBUG  # -vv


def test_file_captures_info_even_when_console_quiet(tmp_path: Path) -> None:
    configure_logging(0, LogSettings(dir=str(tmp_path), file="w.log", file_level="INFO"))
    logging.getLogger("wheel_screener.test").info("hello from a quiet run")
    for h in logging.getLogger("wheel_screener").handlers:
        h.flush()
    log_file = tmp_path / "w.log"
    assert log_file.exists() and "hello from a quiet run" in log_file.read_text()


def test_file_rotates_at_max_bytes(tmp_path: Path) -> None:
    configure_logging(
        0, LogSettings(dir=str(tmp_path), file="w.log", max_bytes=200, backup_count=2)
    )
    lg = logging.getLogger("wheel_screener.test")
    for i in range(50):
        lg.info("line %d %s", i, "x" * 50)
    for h in logging.getLogger("wheel_screener").handlers:
        h.flush()
    assert (tmp_path / "w.log.1").exists()  # rolled over at least once


def test_nonwritable_log_dir_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir")  # so blocked/sub can't be created
    configure_logging(0, LogSettings(dir=str(blocker / "sub"), enable_file=True))
    handlers = logging.getLogger("wheel_screener").handlers
    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)  # degraded, no crash
