"""Sessions, OAuth state, and the gate that stands between a stranger and an account."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wheel_screener.api.sessions import SessionStore


def _store(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "sessions.sqlite"))


def _later(days=7) -> datetime:
    return datetime.now(tz=UTC) + timedelta(days=days)


# --- the store ------------------------------------------------------------------------------

def test_a_session_round_trips(tmp_path) -> None:
    s = _store(tmp_path)
    token = s.create("schwab", "fp", _later())
    got = s.get(token)
    assert got is not None and got.broker == "schwab" and got.account_fingerprint == "fp"


def test_the_cookie_value_is_unguessable(tmp_path) -> None:
    """No signing secret is used, so the id itself has to be the security property."""
    s = _store(tmp_path)
    tokens = {s.create("schwab", "fp", _later()) for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 40 for t in tokens)  # 256 bits, url-safe


def test_an_expired_session_is_refused_and_dropped(tmp_path) -> None:
    s = _store(tmp_path)
    token = s.create("schwab", "fp", datetime.now(tz=UTC) - timedelta(seconds=1))
    assert s.get(token) is None
    assert s.get(token) is None  # and stays gone


def test_unknown_and_empty_tokens_are_refused(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.get("nope") is None and s.get(None) is None and s.get("") is None


def test_revoke_ends_the_session_immediately(tmp_path) -> None:
    """The point of a server-side store: Disconnect must actually end it, not wait for expiry."""
    s = _store(tmp_path)
    token = s.create("schwab", "fp", _later())
    s.revoke(token)
    assert s.get(token) is None


def test_revoking_a_broker_ends_all_of_its_sessions(tmp_path) -> None:
    s = _store(tmp_path)
    a, b = s.create("schwab", "fp", _later()), s.create("schwab", "fp", _later())
    other = s.create("tastytrade", "fp", _later())
    s.revoke_broker("schwab")
    assert s.get(a) is None and s.get(b) is None
    assert s.get(other) is not None, "another broker's sessions are untouched"


# --- OAuth state ----------------------------------------------------------------------------

def test_state_is_single_use(tmp_path) -> None:
    """A replayed redirect — the same callback URL opened twice — must not mint a second session."""
    s = _store(tmp_path)
    state = s.issue_state("schwab")
    assert s.consume_state(state) == "schwab"
    assert s.consume_state(state) is None


def test_state_expires(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.consume_state(s.issue_state("schwab", ttl_seconds=-1)) is None


def test_unknown_state_is_refused(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.consume_state("forged") is None and s.consume_state(None) is None


def test_state_is_bound_to_its_broker(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.consume_state(s.issue_state("tastytrade")) == "tastytrade"


# --- the gate -------------------------------------------------------------------------------

pytest.importorskip("fastapi")

from wheel_screener.api.app import _needs_portfolio_session  # noqa: E402


@pytest.mark.parametrize("path", [
    "/portfolio/positions", "/portfolio/oauth/schwab/disconnect", "/portfolio/",
    "/portfolio/anything/else",
])
def test_portfolio_routes_are_gated_by_default(path: str) -> None:
    """Deny by default. An exempt-by-prefix rule is how the callback ends up unprotected."""
    assert _needs_portfolio_session(path) is True


@pytest.mark.parametrize("path", [
    "/portfolio", "/portfolio/oauth/schwab/connect", "/portfolio/oauth/schwab/callback",
])
def test_only_the_entry_points_are_open(path: str) -> None:
    assert _needs_portfolio_session(path) is False


@pytest.mark.parametrize("path", ["/", "/search", "/fundamentals", "/health", "/portfoliox"])
def test_the_rest_of_the_site_is_untouched(path: str) -> None:
    assert _needs_portfolio_session(path) is False


def test_the_callback_is_rate_limited() -> None:
    from wheel_screener.api.ratelimit import is_expensive

    assert is_expensive("GET", "/portfolio/oauth/schwab/callback")
    assert is_expensive("GET", "/portfolio/oauth/schwab/connect")


# --- the routes -----------------------------------------------------------------------------


from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.core.models import BrokerLinkStatus  # noqa: E402


class _FakeLink:
    broker = "schwab"

    def __init__(self, connected=True):
        self.connected, self.revoked = connected, False

    def status(self):
        return BrokerLinkStatus(
            broker="schwab", configured=True, connected=self.connected,
            expires_at=_later() if self.connected else None,
        )

    def authorize_url(self, state):
        return f"https://schwab.example/authorize?state={state}"

    def complete(self, received_url, state):
        return self.status()

    def revoke(self):
        self.connected, self.revoked = False, True


def _client(link=None):
    c = TestClient(app)
    c.__enter__()
    app.state.settings.portfolio.cookie_secure = False  # TestClient speaks http
    app.state.links = {"schwab": link or _FakeLink()}
    return c


def _sign_in(c) -> str:
    loc = c.get("/portfolio/oauth/schwab/connect", follow_redirects=False).headers["location"]
    state = loc.split("state=")[-1]
    c.get(f"/portfolio/oauth/schwab/callback?code=X&state={state}", follow_redirects=False)
    return state


def test_a_stranger_sees_a_way_in_and_nothing_else() -> None:
    c = _client()
    try:
        body = c.get("/portfolio").text
        assert "Sign in with Schwab" in body
        assert "Disconnect" not in body and "Connected" not in body
    finally:
        c.__exit__(None, None, None)


def test_signing_in_grants_access_and_signing_out_removes_it() -> None:
    link = _FakeLink()
    c = _client(link)
    try:
        _sign_in(c)
        assert "Connected" in c.get("/portfolio").text
        c.post("/portfolio/oauth/schwab/disconnect", follow_redirects=False)
        assert link.revoked, "disconnect must delete the credential, not just the session"
        assert "Sign in with Schwab" in c.get("/portfolio").text
    finally:
        c.__exit__(None, None, None)


def test_a_forged_or_replayed_callback_mints_nothing() -> None:
    c = _client()
    try:
        r = c.get("/portfolio/oauth/schwab/callback?code=X&state=forged", follow_redirects=False)
        assert r.status_code == 400 and "ws_portfolio" not in r.headers.get("set-cookie", "")
        state = _sign_in(c)
        replay = c.get(f"/portfolio/oauth/schwab/callback?code=X&state={state}",
                       follow_redirects=False)
        assert replay.status_code == 400, "state is single use"
    finally:
        c.__exit__(None, None, None)


def test_the_session_cookie_is_locked_down() -> None:
    c = _client()
    try:
        loc = c.get("/portfolio/oauth/schwab/connect", follow_redirects=False).headers["location"]
        r = c.get(f"/portfolio/oauth/schwab/callback?code=X&state={loc.split('state=')[-1]}",
                  follow_redirects=False)
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie, "script must not be able to read the session"
        assert "SameSite=lax" in cookie, "Lax, not Strict: the callback is a cross-site redirect"
        assert "Path=/portfolio" in cookie, "scoped to the feature that needs it"
    finally:
        c.__exit__(None, None, None)


def test_relinking_ends_the_previous_session() -> None:
    """A relink may be a different account, so old sessions must not survive it."""
    c = _client()
    try:
        _sign_in(c)
        first = c.cookies.get("ws_portfolio")
        _sign_in(c)
        assert app.state.sessions.get(first) is None
    finally:
        c.__exit__(None, None, None)


def test_an_expired_link_offers_reconnect_rather_than_an_error() -> None:
    link = _FakeLink()
    c = _client(link)
    try:
        _sign_in(c)
        link.connected = False  # the weekly condition
        body = c.get("/portfolio").text
        assert "Reconnect" in body and "expired" in body.lower()
    finally:
        c.__exit__(None, None, None)


# --- balances on the tab --------------------------------------------------------------------

from wheel_screener.api.app import _money  # noqa: E402
from wheel_screener.api.deps import get_service  # noqa: E402
from wheel_screener.core.errors import AuthExpiredError  # noqa: E402
from wheel_screener.core.models import AccountBalances, AccountType, BrokerageAccount  # noqa: E402


class _AccountService:
    def __init__(self, accounts=None, error=None):
        self._accounts, self._error, self.calls = accounts or [], error, 0

    def brokerage_accounts(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._accounts


def _account(**kw):
    balances = AccountBalances(total_value=1000.0, cash=400.0, invested=600.0, buying_power=800.0)
    return BrokerageAccount(
        broker="schwab", account_id="HASH", display_name="••••1337",
        account_type=AccountType.MARGIN, balances=kw.get("balances", balances),
    )


def _signed_in(service):
    c = _client()
    app.dependency_overrides[get_service] = lambda: service
    app.state.balances_cache = None
    _sign_in(c)
    return c


def test_the_connected_tab_shows_the_money() -> None:
    c = _signed_in(_AccountService([_account()]))
    try:
        body = c.get("/portfolio").text
        assert "••••1337" in body and "margin" in body
        assert "$1,000.00" in body and "$400.00" in body and "$600.00" in body
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_a_failed_balance_fetch_degrades_inside_the_page() -> None:
    """A balance we cannot fetch is a message, not a 500 — the session is still valid."""
    service = _AccountService(error=AuthExpiredError("Schwab rejected our credentials"))
    c = _signed_in(service)
    try:
        r = c.get("/portfolio")
        assert r.status_code == 200
        assert "Schwab rejected our credentials" in r.text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_balances_are_cached_so_a_refresh_does_not_re_ask_the_broker() -> None:
    service = _AccountService([_account()])
    c = _signed_in(service)
    try:
        for _ in range(4):
            c.get("/portfolio")
        assert service.calls == 1
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_disconnect_drops_the_cached_numbers() -> None:
    """Cached balances must not outlive the session that was allowed to see them."""
    service = _AccountService([_account()])
    c = _signed_in(service)
    try:
        c.get("/portfolio")
        c.post("/portfolio/oauth/schwab/disconnect", follow_redirects=False)
        assert app.state.balances_cache is None
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_an_anonymous_visitor_never_reaches_the_broker() -> None:
    service = _AccountService([_account()])
    c = _client()
    app.dependency_overrides[get_service] = lambda: service
    try:
        body = c.get("/portfolio").text
        assert service.calls == 0, "no session, no upstream call"
        assert "$1,000.00" not in body
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_money_renders_unknown_as_a_dash_not_zero() -> None:
    """A missing balance must never read as $0.00 — that is a claim the data does not make."""
    assert _money(None) == "—" and _money("x") == "—"
    assert _money(0) == "$0.00"
    assert _money(1234.5) == "$1,234.50"
    assert _money(-50.0) == "-$50.00"


# --- an unconfigured deployment --------------------------------------------------------------

class _UnconfiguredLink(_FakeLink):
    def status(self):
        return BrokerLinkStatus(broker="schwab", configured=False, connected=False)

    def authorize_url(self, state):
        from wheel_screener.core.errors import ProviderUnavailableError

        raise ProviderUnavailableError("This deployment has no Schwab application configured yet")


def test_an_unconfigured_deployment_says_so_instead_of_offering_a_dead_button() -> None:
    """'Nobody has signed in' and 'there is nothing to sign in to' are different answers, and only
    the second is the operator's problem — so the visitor is told rather than handed a failure."""
    c = _client(_UnconfiguredLink())
    try:
        body = c.get("/portfolio").text
        assert "Sign in with Schwab" not in body
        assert "nothing to" in body
        assert "SCHWAB__CLIENT_ID" not in body, "server config names are not for visitors"
    finally:
        c.__exit__(None, None, None)


def test_connecting_anyway_fails_without_naming_environment_variables() -> None:
    c = _client(_UnconfiguredLink())
    try:
        body = c.get("/portfolio/oauth/schwab/connect").text
        assert "no Schwab application configured" in body
        assert "SCHWAB__CLIENT_SECRET" not in body
    finally:
        c.__exit__(None, None, None)


def test_a_configured_deployment_still_offers_the_button() -> None:
    c = _client(_FakeLink(connected=False))
    try:
        assert "Sign in with Schwab" in c.get("/portfolio").text
    finally:
        c.__exit__(None, None, None)


def test_a_loopback_callback_disables_the_web_sign_in() -> None:
    """The callback defaults to 127.0.0.1 for the CLI's local login. Left that way on a server,
    Schwab redirects the VISITOR'S browser to their own machine with the code attached — the
    sign-in appears to work and lands nowhere. Treated as not configured instead."""
    from wheel_screener.adapters.schwab.link import SchwabOAuthLink
    from wheel_screener.config import SchwabSettings

    def link(cb):
        return SchwabOAuthLink(SchwabSettings(client_id="k", client_secret="s", callback_url=cb))

    assert link("https://127.0.0.1:8182").status().configured is False
    assert link("https://localhost:9000/x").status().configured is False
    assert link("").status().configured is False
    assert link("https://steadybull.net/portfolio/oauth/schwab/callback").status().configured


# ── token file shape ───────────────────────────────────────────────────────────────────────
# The Portfolio tab once read "Connected" with a live expiry while every Schwab call failed
# with `unsupported_token_type: Unsupported token_type: 'access_token'`. Both halves were true:
# status() only reads the OUTER creation_timestamp, which survives the mistake below.

def _link(tmp_path, **kw):
    from pydantic import SecretStr

    from wheel_screener.adapters.schwab.link import SchwabOAuthLink
    from wheel_screener.config import SchwabSettings

    return SchwabOAuthLink(SchwabSettings(
        client_id="id", client_secret=SecretStr("secret"),
        callback_url="https://example.test/portfolio/oauth/schwab/callback",
        token_path=str(tmp_path / "schwab_token.json"), **kw))


def _capture_writer(link):
    """The function schwab-py is handed, without running the OAuth exchange."""
    import json as _json

    def write_token(payload, *_args):
        link._token_path.parent.mkdir(parents=True, exist_ok=True)
        link._token_path.write_text(_json.dumps(payload))
    return write_token


def test_the_token_is_written_exactly_as_schwab_py_wraps_it(tmp_path) -> None:
    """schwab-py's TokenMetadata.wrapped_token_write_func has ALREADY applied the
    {creation_timestamp, token} envelope before calling us. Adding a second one produced a file
    that authlib read as a token whose type was the literal string 'access_token'."""
    import inspect
    import json as _json

    from wheel_screener.adapters.schwab.link import SchwabOAuthLink

    src = inspect.getsource(SchwabOAuthLink.complete)
    assert "json.dumps(payload)" in src
    assert '"token": token' not in src, "re-wrapping schwab-py's envelope is the bug"

    link = _link(tmp_path)
    wrapped = {"creation_timestamp": 1_700_000_000,
               "token": {"access_token": "A", "refresh_token": "R", "token_type": "Bearer"}}
    _capture_writer(link)(wrapped)
    assert _json.loads(link._token_path.read_text()) == wrapped


def test_a_refresh_does_not_slide_the_seven_day_authorisation_clock(tmp_path) -> None:
    """This writer is also the update_token hook, so it runs on every ~30-minute access-token
    refresh. Stamping our own timestamp there reset the refresh token's 7-day life each time —
    the tab would promise a week of authorisation forever while the credential died silently."""
    import json as _json

    link = _link(tmp_path)
    write = _capture_writer(link)
    granted = 1_700_000_000
    write({"creation_timestamp": granted, "token": {"access_token": "A", "token_type": "Bearer"}})
    # ...half an hour later schwab-py refreshes the access token and writes again
    write({"creation_timestamp": granted, "token": {"access_token": "B", "token_type": "Bearer"}})
    on_disk = _json.loads(link._token_path.read_text())
    assert on_disk["creation_timestamp"] == granted, "the grant time must not move on refresh"
    assert on_disk["token"]["access_token"] == "B"


def test_a_double_wrapped_token_is_repaired_in_place(tmp_path) -> None:
    """The credential underneath is valid — the envelope is wrong, not the grant — so a deploy
    must not cost someone their broker link."""
    import json as _json

    from wheel_screener.adapters.schwab.auth import repair_token_file

    real = {"access_token": "A", "refresh_token": "R", "token_type": "Bearer"}
    path = tmp_path / "t.json"
    path.write_text(_json.dumps(
        {"creation_timestamp": 999, "token": {"creation_timestamp": 111, "token": real}}))

    assert repair_token_file(path) is True
    fixed = _json.loads(path.read_text())
    assert fixed == {"creation_timestamp": 111, "token": real}
    assert fixed["creation_timestamp"] == 111, "the real grant time, not the outer re-stamp"
    assert repair_token_file(path) is False, "must be idempotent"


def test_repair_leaves_a_correct_token_and_junk_alone(tmp_path) -> None:
    import json as _json

    from wheel_screener.adapters.schwab.auth import repair_token_file

    good = {"creation_timestamp": 111,
            "token": {"access_token": "A", "refresh_token": "R", "token_type": "Bearer"}}
    p = tmp_path / "good.json"
    p.write_text(_json.dumps(good))
    assert repair_token_file(p) is False and _json.loads(p.read_text()) == good

    junk = tmp_path / "junk.json"
    junk.write_text("not json at all")
    assert repair_token_file(junk) is False  # unreadable, but must never raise
    assert repair_token_file(tmp_path / "missing.json") is False


def test_the_repaired_token_loads_with_a_usable_token_type(tmp_path) -> None:
    """The end of the chain: a repaired file must produce a Bearer token, which is the thing
    authlib refused to do with the double-wrapped one."""
    import json as _json

    from schwab.auth import client_from_token_file

    from wheel_screener.adapters.schwab.auth import repair_token_file

    real = {"access_token": "A", "refresh_token": "R", "token_type": "Bearer",
            "expires_in": 1800, "expires_at": 9_999_999_999}
    path = tmp_path / "t.json"
    path.write_text(_json.dumps(
        {"creation_timestamp": 999, "token": {"creation_timestamp": 111, "token": real}}))
    repair_token_file(path)
    client = client_from_token_file(str(path), "key", "secret")
    assert client.session.token["token_type"] == "Bearer"


# ── positions on the page ──────────────────────────────────────────────────────────────────

def _account_with_positions():
    from datetime import date as _date

    from wheel_screener.core.models import (
        AccountBalances,
        AccountType,
        BrokerageAccount,
        Position,
        PositionKind,
    )

    return BrokerageAccount(
        broker="schwab", account_id="hash", display_name="••••6789",
        account_type=AccountType.MARGIN,
        balances=AccountBalances(total_value=150_000.0, cash=100_000.0, invested=50_000.0,
                                 buying_power=120_000.0),
        positions=[
            Position(symbol="AAPL  260918P00190000", underlying="AAPL",
                     kind=PositionKind.SHORT_PUT, quantity=2, strike=190.0,
                     expiration=_date(2026, 9, 18), dte=20, collateral=38_000.0,
                     market_value=-420.0, underlying_price=185.0),   # in the money
            Position(symbol="MSFT  261016P00400000", underlying="MSFT",
                     kind=PositionKind.SHORT_PUT, quantity=1, strike=400.0,
                     expiration=_date(2026, 10, 16), dte=48, collateral=40_000.0,
                     market_value=-310.0, underlying_price=455.0),   # safe
            Position(symbol="NVDA  260918P00100000", underlying="NVDA",
                     kind=PositionKind.SHORT_PUT, quantity=1, strike=100.0,
                     expiration=_date(2026, 9, 18), dte=20, collateral=10_000.0),  # no quote
            Position(symbol="TSLA", underlying="TSLA", kind=PositionKind.SHARES,
                     quantity=250, average_price=210.0, market_value=60_000.0),
            Position(symbol="F", underlying="F", kind=PositionKind.SHARES,
                     quantity=40, average_price=11.0, market_value=460.0),
        ],
    )


def _portfolio_page() -> str:
    """The Portfolio tab, signed in, with a populated account."""
    from wheel_screener.api.deps import get_service

    class _Svc:
        def brokerage_accounts(self):
            return [_account_with_positions()]

    c = _client()
    app.dependency_overrides[get_service] = lambda: _Svc()
    try:
        _sign_in(c)
        app.state.balances_cache = None  # the route caches for 30s; this test wants a fresh read
        return c.get("/portfolio").text
    finally:
        app.dependency_overrides.pop(get_service, None)
        c.__exit__(None, None, None)


def test_capacity_is_cash_minus_committed_collateral() -> None:
    """The wheel question the balance grid cannot answer: how much more can I sell?"""
    body = _portfolio_page()
    assert "Capacity" in body
    assert "$12,000" in body, "100k cash - 88k committed"
    assert "$88,000" in body and "committed to open puts" in body


def test_short_puts_are_listed_soonest_first_with_collateral() -> None:
    body = _portfolio_page()
    assert "Open short puts" in body
    order = [body.index(s) for s in ("AAPL", "NVDA", "MSFT")]
    assert order == sorted(order), "the near expiry needs the decision, so it goes first"
    assert "$38,000" in body and "18 Sep" in body


def test_the_assignment_watch_distinguishes_itm_safe_and_unknown() -> None:
    """Three states, and the third must not read like the second: a put with no quote is not a
    put that is safe."""
    body = _portfolio_page()
    assert "in the money" in body      # AAPL: spot 185 < strike 190
    assert "$455" in body              # MSFT: spot above strike, shown plainly
    assert "no quote" in body          # NVDA: unknown, and said so


def test_share_lots_flag_how_many_calls_they_cover() -> None:
    body = _portfolio_page()
    assert "Shares held" in body
    assert "2 contracts" in body, "250 shares covers two calls, not two and a half"
    assert "under 100 shares" in body, "40 shares covers nothing, whatever it is worth"
