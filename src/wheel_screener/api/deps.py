"""FastAPI dependencies — expose the process-singleton ScreenerService + Settings.

The singleton is built ONCE in the app lifespan (see app.py) and stashed on ``app.state``;
these read it per request (no per-request rebuild of the data store). Tests swap them out
via ``app.dependency_overrides``.
"""

from __future__ import annotations

from fastapi import Request

from wheel_screener.adapters.snaptrade.account import SnapTradeAccountProvider
from wheel_screener.adapters.snaptrade.client import SnapTradeUser
from wheel_screener.api.jobs import JobRunner
from wheel_screener.api.secretbox import SecretBoxError
from wheel_screener.api.users import Session
from wheel_screener.composition import build_portfolio
from wheel_screener.config import Settings
from wheel_screener.core.portfolio import AllAccounts, PortfolioService
from wheel_screener.core.service import ScreenerService


def get_service(request: Request) -> ScreenerService:
    return request.app.state.service


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_job_runner(request: Request) -> JobRunner:
    return request.app.state.job_runner


def current_session(request: Request) -> Session | None:
    """The signed-in session for this request, or None. Never raises."""
    store = getattr(request.app.state, "users", None)
    settings = getattr(request.app.state, "settings", None)
    if store is None or settings is None:
        return None
    return store.session(request.cookies.get(settings.portfolio.cookie_name))


def get_portfolio(request: Request) -> PortfolioService:
    """The account-facing service for THIS request, holding THIS person's broker credential.

    Built per request, unlike the screener singleton, because a credential belongs to one person.
    This is the only place a session turns into one, and there is nowhere else for a route to get
    it from.

    While a deployment has a single Schwab token, "this person's credential" means: the token, if
    they are the one who linked it, and nothing otherwise. That rule is what stops a signed-in
    friend being shown the owner's account — the token file itself does not know whose it is.
    """
    session = current_session(request)
    store = request.app.state.users
    settings = request.app.state.settings
    owns_link = session is not None and store.link_owner("schwab") == session.user.id
    schwab = build_portfolio(settings, request.app.state.service, linked=owns_link).accounts
    sources = [schwab] if schwab is not None else []
    snaptrade = snaptrade_for(request)
    if snaptrade is not None:
        sources.append(snaptrade)
    accounts = sources[0] if len(sources) == 1 else (AllAccounts(sources) if sources else None)
    return PortfolioService(accounts=accounts, screener=request.app.state.service)


def snaptrade_user(request: Request) -> SnapTradeUser | None:
    """THIS person's SnapTrade identity, decrypted — or None if they have never linked through
    it, or this deployment has no SnapTrade keys. Built from the session and nothing else."""
    session = current_session(request)
    box = getattr(request.app.state, "secretbox", None)
    if session is None or box is None or getattr(request.app.state, "snaptrade", None) is None:
        return None
    sealed = request.app.state.users.snaptrade_secret(session.user.id)
    if sealed is None:
        return None
    try:
        return SnapTradeUser(session.user.id, box.open(sealed))
    except SecretBoxError:
        # Sealed with another key (it was rotated, or a backup came from elsewhere). The link has
        # to be made again; treating it as "not linked" offers exactly that.
        return None


def snaptrade_for(request: Request):
    """The SnapTrade account source for this person, or None."""
    user = snaptrade_user(request)
    if user is None:
        return None
    return SnapTradeAccountProvider(request.app.state.snaptrade, user)
