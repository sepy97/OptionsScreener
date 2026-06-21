"""FastAPI dependencies — provide the same ScreenerService the CLI uses."""

from __future__ import annotations

from wheel_screener.composition import build_service
from wheel_screener.core.service import ScreenerService


def get_service() -> ScreenerService:
    return build_service()
