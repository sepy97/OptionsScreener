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
import re
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


# --- secrets in request lines ----------------------------------------------------------------
# uvicorn's access log records every request's full path, query string included. Two kinds of URL
# here carry a live credential: an invite link (the token IS the path, a bearer credential until
# used) and the broker's OAuth callback (a one-time authorization code in the query). Both are
# redacted rather than the log turned off, because it is the only request log the app keeps.
_SECRET_PATHS = (
    # (pattern, replacement) applied to the path as logged
    (re.compile(r"^(/invite/)[^/?#]+"), r"\1<redacted>"),
    (re.compile(r"^(/portfolio/oauth/[^/?#]+/callback)\?.*$"), r"\1?<redacted>"),
)


def redact_path(path: str) -> str:
    for pattern, replacement in _SECRET_PATHS:
        path = pattern.sub(replacement, path)
    return path


class RedactSecretsFilter(logging.Filter):
    """Rewrites uvicorn access-log records so no credential in a URL reaches the log.

    uvicorn passes the path as the third positional argument —
    ``(client_addr, method, full_path, http_version, status_code)`` — and formats it late, so the
    argument is rewritten before any handler sees it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_path(args[2]), *args[3:])
        return True


def redact_access_log() -> None:
    """Install the filter on uvicorn's access logger. Idempotent."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecretsFilter) for f in access.filters):
        access.addFilter(RedactSecretsFilter())
