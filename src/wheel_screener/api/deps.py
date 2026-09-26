"""FastAPI dependencies — expose the process-singleton ScreenerService + Settings.

The singleton is built ONCE in the app lifespan (see app.py) and stashed on ``app.state``;
these read it per request (no per-request rebuild of the data store). Tests swap them out
via ``app.dependency_overrides``.
"""

from __future__ import annotations

from fastapi import Request

from wheel_screener.api.jobs import JobRunner
from wheel_screener.composition import build_portfolio
from wheel_screener.config import Settings
from wheel_screener.core.portfolio import PortfolioService
from wheel_screener.core.service import ScreenerService


def get_service(request: Request) -> ScreenerService:
    return request.app.state.service


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_job_runner(request: Request) -> JobRunner:
    return request.app.state.job_runner


def get_portfolio(request: Request) -> PortfolioService:
    """The account-facing service for THIS request.

    Built per request, unlike the screener singleton, because it carries a broker credential and
    that belongs to one person. Today a deployment has a single credential, so every request gets
    an equivalent object; building it here anyway is what makes the seam real — when the credential
    becomes per-user, this is the one place a session turns into one, and there is nowhere else for
    a route to get it from.
    """
    return build_portfolio(request.app.state.settings, request.app.state.service)
