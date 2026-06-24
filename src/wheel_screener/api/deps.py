"""FastAPI dependencies — expose the process-singleton ScreenerService + Settings.

The singleton is built ONCE in the app lifespan (see app.py) and stashed on ``app.state``;
these read it per request (no per-request rebuild of the data store). Tests swap them out
via ``app.dependency_overrides``.
"""

from __future__ import annotations

from fastapi import Request

from wheel_screener.config import Settings
from wheel_screener.core.service import ScreenerService


def get_service(request: Request) -> ScreenerService:
    return request.app.state.service


def get_settings(request: Request) -> Settings:
    return request.app.state.settings
