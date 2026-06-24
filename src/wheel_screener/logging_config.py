"""Diagnostic logging setup for the CLI.

Results and user-facing errors go through ``typer.echo`` (not logging). This wires the
*diagnostic* channel for the ``wheel_screener`` package logger:

- a **console** handler (stderr) whose level follows ``-v``/``-vv`` (WARNING / INFO / DEBUG)
- an always-on **rotating file** handler that captures ``file_level`` and up, so even a quiet
  (or cron'd) run leaves a recoverable history on disk

``propagate`` is left at its default so pytest's ``caplog`` keeps working and library users
who configure the root logger still see our records; with our own handlers attached, Python's
last-resort handler won't double-emit.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from wheel_screener.config import LogSettings

_PKG = "wheel_screener"
_CONSOLE_LEVELS = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}


def configure_logging(verbosity: int, settings: LogSettings) -> None:
    """Idempotently (re)configure the package logger's handlers."""
    logger = logging.getLogger(_PKG)
    logger.setLevel(logging.DEBUG)  # handlers do the level filtering
    logger.handlers.clear()

    console = logging.StreamHandler()  # stderr
    console.setLevel(_CONSOLE_LEVELS.get(verbosity, logging.DEBUG))
    console.setFormatter(_console_formatter(verbosity))
    logger.addHandler(console)

    if settings.enable_file:
        file_handler = _file_handler(settings)
        if file_handler is not None:
            logger.addHandler(file_handler)


def _console_formatter(verbosity: int) -> logging.Formatter:
    if verbosity <= 0:
        return logging.Formatter("%(levelname)-8s %(message)s")
    return logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")


def _file_handler(settings: LogSettings) -> logging.Handler | None:
    try:
        Path(settings.dir).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            Path(settings.dir) / settings.file,
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            delay=True,  # open the file lazily, on the first record
        )
    except OSError:
        return None  # a non-writable logs dir must not break the run; console still works
    handler.setLevel(getattr(logging, settings.file_level.upper(), logging.INFO))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    return handler
