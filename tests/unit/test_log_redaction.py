"""Credentials that travel in URLs must not reach the request log."""

from __future__ import annotations

import logging

import pytest

from wheel_screener.logging_config import RedactSecretsFilter, redact_path


@pytest.mark.parametrize("path, logged", [
    ("/invite/VRSnKDqZqvxhcwHM3Sr6", "/invite/<redacted>"),
    ("/invite/VRSnKDqZqvxhcwHM3Sr6?x=1", "/invite/<redacted>?x=1"),
    ("/portfolio/oauth/schwab/callback?code=C0DE&state=S7ATE",
     "/portfolio/oauth/schwab/callback?<redacted>"),
    # everything else is logged as it was
    ("/portfolio", "/portfolio"),
    ("/search?symbol=AAPL", "/search?symbol=AAPL"),
    ("/portfolio/oauth/schwab/connect", "/portfolio/oauth/schwab/connect"),
])
def test_only_the_secret_parts_are_redacted(path, logged) -> None:
    assert redact_path(path) == logged


def test_uvicorns_own_access_line_carries_no_secret() -> None:
    """Formatted by uvicorn's formatter, with the arguments in the order uvicorn passes them."""
    uvicorn_logging = pytest.importorskip("uvicorn.logging")
    formatter = uvicorn_logging.AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    )
    lines = []
    for path in ("/invite/SEKRIT-TOKEN", "/portfolio/oauth/schwab/callback?code=SEKRIT&state=X"):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", path, "1.1", 200), None,
        )
        assert RedactSecretsFilter().filter(record) is True  # redacted, never dropped
        lines.append(formatter.format(record))
    assert all("SEKRIT" not in line for line in lines), lines
    assert "/invite/<redacted>" in lines[0]
