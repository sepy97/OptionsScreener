"""Users, sessions, OAuth state, and the gates between a visitor and an account."""

from __future__ import annotations

import pathlib
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from _softkey import SoftKey

from wheel_screener.api.users import UserStore


def _store(tmp_path) -> UserStore:
    return UserStore(str(tmp_path / "users.sqlite"))


def _later(days=7) -> datetime:
    return datetime.now(tz=UTC) + timedelta(days=days)


# --- sessions -------------------------------------------------------------------------------

def test_a_session_round_trips_to_its_user(tmp_path) -> None:
    s = _store(tmp_path)
    sam = s.create_user("Sam", is_admin=True)
    token, _ = s.create_session(sam.id, timedelta(days=1))
    got = s.session(token)
    assert got is not None and got.user.id == sam.id and got.user.is_admin


def test_the_cookie_value_is_unguessable(tmp_path) -> None:
    """No signing secret is used, so the id itself has to be the security property."""
    s = _store(tmp_path)
    sam = s.create_user("Sam")
    tokens = {s.create_session(sam.id, timedelta(days=1))[0] for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 40 for t in tokens)  # 256 bits, url-safe


def test_an_expired_session_is_refused_and_dropped(tmp_path) -> None:
    s = _store(tmp_path)
    token, _ = s.create_session(s.create_user("Sam").id, timedelta(seconds=-1))
    assert s.session(token) is None
    assert s.session(token) is None  # and stays gone


def test_unknown_and_empty_tokens_are_refused(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.session("nope") is None and s.session(None) is None and s.session("") is None


def test_ending_a_session_ends_it_immediately(tmp_path) -> None:
    """The point of a server-side store: signing out must actually end it, not wait for expiry."""
    s = _store(tmp_path)
    token, _ = s.create_session(s.create_user("Sam").id, timedelta(days=1))
    s.end_session(token)
    assert s.session(token) is None


def test_ending_one_persons_sessions_leaves_everyone_elses(tmp_path) -> None:
    """What `revoke_broker` could not do: it ended every session a broker had minted, which with
    more than one person would sign everybody out when one of them relinked."""
    s = _store(tmp_path)
    sam, alex = s.create_user("Sam"), s.create_user("Alex")
    phone, laptop = (s.create_session(sam.id, timedelta(days=1))[0] for _ in range(2))
    theirs, _ = s.create_session(alex.id, timedelta(days=1))
    s.end_sessions_for(sam.id)
    assert s.session(phone) is None and s.session(laptop) is None
    assert s.session(theirs) is not None


# --- OAuth state ----------------------------------------------------------------------------

def test_state_is_single_use(tmp_path) -> None:
    """A replayed redirect — the same callback URL opened twice — must not link twice."""
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


# --- who owns a link ------------------------------------------------------------------------

def test_a_link_has_one_owner_and_can_change_hands(tmp_path) -> None:
    s = _store(tmp_path)
    sam, alex = s.create_user("Sam"), s.create_user("Alex")
    assert s.link_owner("schwab") is None
    s.set_link_owner("schwab", sam.id)
    s.set_link_owner("schwab", alex.id)
    assert s.link_owner("schwab") == alex.id
    s.clear_link_owner("schwab")
    assert s.link_owner("schwab") is None


# --- the gate -------------------------------------------------------------------------------

pytest.importorskip("fastapi")

from wheel_screener.api.app import _needs_portfolio_session, _safe_next  # noqa: E402


@pytest.mark.parametrize("path", [
    "/portfolio", "/portfolio/", "/portfolio/positions", "/portfolio/anything/else",
    "/portfolio/oauth/schwab/connect", "/portfolio/oauth/schwab/callback",
    "/portfolio/oauth/schwab/disconnect",
])
def test_every_portfolio_route_needs_a_session(path: str) -> None:
    """No exceptions any more. The connect and callback routes used to be open because the broker
    sign-in WAS the site sign-in — which is how any visitor with a Schwab account could claim the
    deployment's broker slot."""
    assert _needs_portfolio_session(path) is True


@pytest.mark.parametrize("path", [
    "/", "/search", "/fundamentals", "/health", "/portfoliox", "/login", "/invite/x",
])
def test_the_rest_of_the_site_is_untouched(path: str) -> None:
    assert _needs_portfolio_session(path) is False


def test_the_broker_and_sign_in_routes_are_rate_limited() -> None:
    from wheel_screener.api.ratelimit import is_expensive

    assert is_expensive("GET", "/portfolio/oauth/schwab/callback")
    assert is_expensive("GET", "/portfolio/oauth/schwab/connect")
    for path in ("/auth/login/options", "/auth/login/verify", "/auth/register/options",
                 "/auth/register/verify"):
        assert is_expensive("POST", path), path
    assert is_expensive("GET", "/invite/sometoken")


@pytest.mark.parametrize("raw, expected", [
    ("/portfolio/swap?position=X", "/portfolio/swap?position=X"),
    ("/search", "/search"),
    ("//evil.example/steal", "/portfolio"),  # a URL to another host, to a browser
    ("/\\evil.example", "/portfolio"),  # browsers read a backslash as a slash here
    ("https://evil.example", "/portfolio"),
    ("", "/portfolio"),
    (None, "/portfolio"),
])
def test_after_signing_in_you_only_ever_land_on_this_site(raw, expected) -> None:
    assert _safe_next(raw) == expected


# --- the routes -----------------------------------------------------------------------------


from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.passkeys import Passkeys  # noqa: E402
from wheel_screener.core.models import BrokerLinkStatus  # noqa: E402

ORIGIN = "http://testserver"  # what TestClient's requests present as their origin


class _FakeLink:
    broker = "schwab"

    def __init__(self, connected=True):
        self.connected, self.revoked, self.completed = connected, False, 0

    def status(self):
        return BrokerLinkStatus(
            broker="schwab", configured=True, connected=self.connected,
            expires_at=_later() if self.connected else None,
        )

    def authorize_url(self, state):
        return f"https://schwab.example/authorize?state={state}"

    def complete(self, received_url, state):
        self.completed += 1
        self.connected = True
        return self.status()

    def revoke(self):
        self.connected, self.revoked = False, True


def _client(link=None):
    """A client over the real app, with its OWN user store: the lifespan would otherwise open the
    repo's data/sessions.sqlite, and users and link owners would leak between tests."""
    c = TestClient(app)
    c.__enter__()
    settings = app.state.settings
    settings.portfolio.cookie_secure = False  # TestClient speaks http
    app.state.links = {"schwab": link or _FakeLink()}
    app.state.users = UserStore(tempfile.mkdtemp() + "/users.sqlite")
    app.state.passkeys = Passkeys(app.state.users, "testserver", "Steady Bull", ORIGIN)
    return c


def _as(c, name="Sam", *, admin=True, owns_link=True):
    """Sign `c` in as a new user, straight through the store (the passkey ceremony has tests of
    its own below). Returns the user."""
    store = app.state.users
    user = store.create_user(name, is_admin=admin)
    token, _ = store.create_session(user.id, timedelta(days=1))
    c.cookies.set(app.state.settings.portfolio.cookie_name, token)
    if owns_link:
        store.set_link_owner("schwab", user.id)
    return user


def _sign_in(c):
    """Signed in as an admin who has linked Schwab — the state every account-display test needs."""
    return _as(c)


def _link_through_the_broker(c):
    """Link Schwab the way a person does: connect, then the broker's redirect back."""
    loc = c.get("/portfolio/oauth/schwab/connect", follow_redirects=False).headers["location"]
    state = loc.split("state=")[-1]
    c.get(f"/portfolio/oauth/schwab/callback?code=X&state={state}", follow_redirects=False)
    return state


def test_a_stranger_is_sent_to_sign_in() -> None:
    c = _client()
    try:
        r = c.get("/portfolio", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/portfolio"
        body = c.get("/portfolio").text
        assert "Sign in with a passkey" in body
        assert "Connect Schwab" not in body and "Disconnect Schwab" not in body
    finally:
        c.__exit__(None, None, None)


def test_a_stranger_cannot_start_or_finish_a_broker_link() -> None:
    """The v3.0.0 hole, closed at the source: these two routes are what claimed the slot."""
    link = _FakeLink(connected=False)
    c = _client(link)
    try:
        start = c.get("/portfolio/oauth/schwab/connect", follow_redirects=False)
        assert start.status_code == 303 and start.headers["location"].startswith("/login")
        state = app.state.users.issue_state("schwab")  # even holding a genuine state
        finish = c.get(f"/portfolio/oauth/schwab/callback?code=X&state={state}",
                       follow_redirects=False)
        assert finish.status_code == 303 and finish.headers["location"].startswith("/login")
        assert link.completed == 0 and app.state.users.link_owner("schwab") is None
    finally:
        c.__exit__(None, None, None)


def test_an_admin_links_the_broker_and_it_is_recorded_as_theirs() -> None:
    link = _FakeLink(connected=False)
    c = _client(link)
    try:
        sam = _as(c, owns_link=False)
        assert "Connect Schwab" in c.get("/portfolio").text
        _link_through_the_broker(c)
        assert link.completed == 1 and app.state.users.link_owner("schwab") == sam.id
        assert "Disconnect Schwab" in c.get("/portfolio").text
    finally:
        c.__exit__(None, None, None)


def test_the_callback_signs_nobody_in() -> None:
    """It used to mint the session. Now it only links a broker to the session that exists."""
    c = _client(_FakeLink(connected=False))
    try:
        _as(c, owns_link=False)
        loc = c.get("/portfolio/oauth/schwab/connect", follow_redirects=False).headers["location"]
        r = c.get(f"/portfolio/oauth/schwab/callback?code=X&state={loc.split('state=')[-1]}",
                  follow_redirects=False)
        assert "set-cookie" not in r.headers
    finally:
        c.__exit__(None, None, None)


def test_a_forged_or_replayed_callback_links_nothing() -> None:
    link = _FakeLink(connected=False)
    c = _client(link)
    try:
        _as(c, owns_link=False)
        r = c.get("/portfolio/oauth/schwab/callback?code=X&state=forged", follow_redirects=False)
        assert r.status_code == 400 and link.completed == 0
        state = _link_through_the_broker(c)
        replay = c.get(f"/portfolio/oauth/schwab/callback?code=X&state={state}",
                       follow_redirects=False)
        assert replay.status_code == 400 and link.completed == 1, "state is single use"
    finally:
        c.__exit__(None, None, None)


def test_a_friend_is_never_shown_the_owners_account(monkeypatch) -> None:
    """The rule that stands in for per-user credentials until Phase 3. The deployment's Schwab
    token does not know whose it is; the recorded owner does, and nobody else gets the account —
    neither on the page, nor from the dependency the account routes are built on."""
    import wheel_screener.api.deps as deps
    from wheel_screener.api.deps import get_portfolio

    built = []
    real = deps.build_portfolio

    def spy(settings, service, *, linked=True):
        built.append(linked)
        return real(settings, service, linked=linked)

    monkeypatch.setattr(deps, "build_portfolio", spy)
    c = _client()
    try:
        _as(c, "Sam")  # the owner links Schwab…
        _as(c, "Alex", admin=False, owns_link=False)  # …then a friend signs in on this client
        body = c.get("/portfolio").text
        assert "Disconnect Schwab" not in body and ">connected</span>" not in body
        assert "No brokerage account is linked to your sign-in" in body
        assert built and not any(built), "the friend's request was built with the credential"

        class _Req:
            app = c.app
            cookies = {app.state.settings.portfolio.cookie_name:
                       c.cookies.get(app.state.settings.portfolio.cookie_name)}

        assert get_portfolio(_Req()).accounts is None
    finally:
        c.__exit__(None, None, None)


def test_a_friend_can_neither_link_nor_unlink_the_broker() -> None:
    link = _FakeLink()
    c = _client(link)
    try:
        _as(c, "Sam")  # owns the link
        _as(c, "Alex", admin=False, owns_link=False)
        assert c.get("/portfolio/oauth/schwab/connect", follow_redirects=False).status_code == 403
        state = app.state.users.issue_state("schwab")
        r = c.get(f"/portfolio/oauth/schwab/callback?code=X&state={state}", follow_redirects=False)
        assert r.status_code == 403 and link.completed == 0
        assert c.post("/portfolio/oauth/schwab/disconnect",
                      follow_redirects=False).status_code == 404
        assert not link.revoked and app.state.users.link_owner("schwab") is not None
    finally:
        c.__exit__(None, None, None)


def test_disconnecting_unlinks_the_broker_but_keeps_you_signed_in() -> None:
    link = _FakeLink()
    c = _client(link)
    try:
        _sign_in(c)
        assert "Disconnect Schwab" in c.get("/portfolio").text
        c.post("/portfolio/oauth/schwab/disconnect", follow_redirects=False)
        assert link.revoked, "disconnect must delete the credential"
        assert app.state.users.link_owner("schwab") is None
        body = c.get("/portfolio").text
        assert "Connect Schwab" in body and "Signed in as" in body
    finally:
        c.__exit__(None, None, None)


def test_signing_out_ends_the_session_and_leaves_the_link() -> None:
    link = _FakeLink()
    c = _client(link)
    try:
        sam = _sign_in(c)
        token = c.cookies.get(app.state.settings.portfolio.cookie_name)
        r = c.post("/auth/logout", follow_redirects=False)
        assert r.status_code == 303 and app.state.users.session(token) is None
        assert not link.revoked and app.state.users.link_owner("schwab") == sam.id
        c.cookies.clear()
        assert c.get("/portfolio", follow_redirects=False).status_code == 303
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


# --- the passkey ceremony, over HTTP --------------------------------------------------------
# The ceremony's own rules are tested in test_passkeys.py; these check the routes around it — the
# JSON in and out, and above all the cookie that comes back.


def _register_over_http(c, token: str, key: SoftKey | None = None):
    key = key or SoftKey(ORIGIN)
    options = c.post("/auth/register/options", json={"invite": token}).json()
    r = c.post("/auth/register/verify", json={"credential": key.create(options)})
    return r, key


def test_an_invite_link_signs_you_in_with_a_new_passkey() -> None:
    c = _client()
    try:
        token = app.state.users.create_invite("Sam", is_admin=True)
        page = c.get(f"/invite/{token}")
        assert page.status_code == 200 and "Welcome, Sam" in page.text
        assert 'data-passkey="register"' in page.text and "/static/passkey.js" in page.text
        r, _ = _register_over_http(c, token)
        assert r.status_code == 200 and r.json() == {"redirect": "/portfolio"}
        assert "Signed in as <b>Sam</b>" in c.get("/portfolio").text
    finally:
        c.__exit__(None, None, None)


def test_the_session_cookie_is_locked_down() -> None:
    c = _client()
    try:
        r, _ = _register_over_http(c, app.state.users.create_invite("Sam"))
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie, "script must not be able to read the session"
        assert "SameSite=lax" in cookie, "Lax: the broker's redirect back is cross-site"
        assert "Path=/" in cookie and "Path=/portfolio" not in cookie
    finally:
        c.__exit__(None, None, None)


def test_a_used_or_unknown_invite_shows_a_dead_end_not_a_button() -> None:
    c = _client()
    try:
        token = app.state.users.create_invite("Sam")
        _register_over_http(c, token)
        c.cookies.clear()
        for t in (token, "made-up"):
            page = c.get(f"/invite/{t}")
            assert page.status_code == 404 and "expired" in page.text
            assert "data-passkey" not in page.text
    finally:
        c.__exit__(None, None, None)


def test_signing_in_with_a_passkey_lands_where_you_were_going() -> None:
    c = _client()
    try:
        _, key = _register_over_http(c, app.state.users.create_invite("Sam", is_admin=True))
        c.cookies.clear()
        options = c.post("/auth/login/options").json()
        r = c.post("/auth/login/verify",
                   json={"credential": key.get(options), "next": "/portfolio/swap?position=X"})
        assert r.status_code == 200 and r.json() == {"redirect": "/portfolio/swap?position=X"}
        assert "Signed in as" in c.get("/portfolio").text
    finally:
        c.__exit__(None, None, None)


def test_a_refused_passkey_sets_no_cookie_and_says_why() -> None:
    c = _client()
    try:
        _, key = _register_over_http(c, app.state.users.create_invite("Sam"))
        c.cookies.clear()
        options = c.post("/auth/login/options").json()
        r = c.post("/auth/login/verify",
                   json={"credential": key.get(options, origin="https://evil.example")})
        assert r.status_code == 400 and "not recognised" in r.json()["error"]
        assert "set-cookie" not in r.headers
        garbled = c.post("/auth/login/verify", json={"credential": "nope"})
        assert garbled.status_code == 400
    finally:
        c.__exit__(None, None, None)


def test_the_sign_in_page_sends_you_on_if_you_are_already_signed_in() -> None:
    c = _client()
    try:
        _sign_in(c)
        r = c.get("/login?next=/portfolio", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/portfolio"
        evil = c.get("/login?next=//evil.example", follow_redirects=False)
        assert evil.headers["location"] == "/portfolio"
    finally:
        c.__exit__(None, None, None)


# --- balances on the tab --------------------------------------------------------------------

from wheel_screener.api.app import _money  # noqa: E402
from wheel_screener.api.deps import get_portfolio, get_service  # noqa: E402
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
        broker="schwab", account_id=kw.get("account_id", "HASH"),
        display_name=kw.get("display_name", "••••1337"),
        account_type=AccountType.MARGIN, balances=kw.get("balances", balances),
    )


def _signed_in(service):
    c = _client()
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_portfolio] = lambda: service
    _reset_caches()
    _sign_in(c)
    return c


def _reset_caches() -> None:
    """Drop the per-user caches between tests. They are keyed by session token, and a test that
    signs in twice would otherwise read the previous run's numbers."""
    app.state.balances_cache = None
    app.state.swap_cache = None


def test_the_connected_tab_shows_the_money() -> None:
    c = _signed_in(_AccountService([_account()]))
    try:
        body = c.get("/portfolio").text
        assert "••••1337" in body and "margin" in body
        assert "$1,000.00" in body and "$400.00" in body   # total value, cash
        assert "$800.00" in body                            # buying power
        # "Invested" was removed: on a wheel account it nets short-put liability against assets,
        # so a growing put book made it shrink — reading as owning less rather than owing more.
        assert "Invested" not in body and "$600.00" not in body
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
    """Cached balances must not outlive the session that was allowed to see them.

    The cache is partitioned by session token now, so the assertion is that the partition is gone
    rather than that the whole cache object is: one person disconnecting must not empty anybody
    else's, and "no partitions at all" is what that looks like with a single signed-in user.
    """
    service = _AccountService([_account()])
    c = _signed_in(service)
    try:
        c.get("/portfolio")
        assert len(app.state.balances_cache) == 1  # cached for the session that read them
        c.post("/portfolio/oauth/schwab/disconnect", follow_redirects=False)
        assert len(app.state.balances_cache) == 0
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
    """'Nothing is linked' and 'there is nothing to link to' are different answers, and only the
    second is the operator's problem — so the admin is told rather than handed a failure."""
    c = _client(_UnconfiguredLink())
    try:
        _as(c, owns_link=False)
        body = c.get("/portfolio").text
        assert "Connect Schwab" not in body
        assert "nothing to" in body
        assert "SCHWAB__CLIENT_ID" not in body, "server config names are not for visitors"
    finally:
        c.__exit__(None, None, None)


def test_connecting_anyway_fails_without_naming_environment_variables() -> None:
    c = _client(_UnconfiguredLink())
    try:
        _as(c, owns_link=False)
        body = c.get("/portfolio/oauth/schwab/connect").text
        assert "no Schwab application configured" in body
        assert "SCHWAB__CLIENT_SECRET" not in body
    finally:
        c.__exit__(None, None, None)


def test_a_configured_deployment_still_offers_the_button() -> None:
    c = _client(_FakeLink(connected=False))
    try:
        _as(c, owns_link=False)
        assert "Connect Schwab" in c.get("/portfolio").text
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
                     kind=PositionKind.SHORT_PUT, option_type="put", quantity=2, strike=190.0,
                     expiration=_date(2026, 9, 18), dte=20, collateral=38_000.0,
                     market_value=-420.0, underlying_price=185.0),   # in the money
            Position(symbol="MSFT  261016P00400000", underlying="MSFT",
                     kind=PositionKind.SHORT_PUT, option_type="put", quantity=1, strike=400.0,
                     expiration=_date(2026, 10, 16), dte=48, collateral=40_000.0,
                     market_value=-310.0, underlying_price=455.0),   # safe
            Position(symbol="NVDA  260918P00100000", underlying="NVDA",
                     kind=PositionKind.SHORT_PUT, option_type="put", quantity=1, strike=100.0,
                     expiration=_date(2026, 9, 18), dte=20, collateral=10_000.0),  # no quote
            Position(symbol="TSLA", underlying="TSLA", kind=PositionKind.SHARES,
                     asset_type="EQUITY", quantity=250, average_price=210.0,
                     market_value=60_000.0),
            Position(symbol="F", underlying="F", kind=PositionKind.SHARES,
                     asset_type="EQUITY", quantity=40, average_price=11.0, market_value=460.0),
            Position(symbol="SPY", underlying="SPY", kind=PositionKind.SHARES,
                     asset_type="COLLECTIVE_INVESTMENT", quantity=150, average_price=520.0,
                     market_value=81_000.0),
            # a bond: the symbol IS the CUSIP, so only the description is readable
            Position(symbol="912810FB9", underlying="912810FB9", kind=PositionKind.OTHER,
                     asset_type="FIXED_INCOME", symbol_is_cusip=True,
                     description="US TREASURY BOND 4.5% 2044", quantity=7,
                     market_value=7_104.03),
            Position(symbol="AAPL  261016C00300000", underlying="AAPL",
                     kind=PositionKind.SHORT_CALL, asset_type="OPTION", option_type="call",
                     quantity=1,
                     strike=300.0, expiration=_date(2026, 10, 16), dte=48,
                     market_value=-150.0),
        ],
    )


def _portfolio_page() -> str:
    """The Portfolio tab, signed in, with a populated account."""
    from wheel_screener.api.deps import get_service

    class _Svc:
        def brokerage_accounts(self):
            return [_account_with_positions()]

    c = _client()
    svc = _Svc()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    try:
        _sign_in(c)
        _reset_caches()  # the route caches for 30s; this test wants a fresh read
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


def test_open_options_is_one_table_covering_every_contract() -> None:
    """Puts and "other options" used to be two lists, so there was no single answer to "what am
    I in right now"."""
    body = _portfolio_page()
    assert "Open options" in body
    assert "Open short puts" not in body and "Other options" not in body
    assert "short put" in body and "short call" in body  # direction and side are on each row
    order = [body.index(s) for s in ("AAPL", "NVDA", "MSFT")]
    assert order == sorted(order), "the near expiry needs the decision, so it goes first"
    assert "$38,000" in body and "18 Sep" in body


def test_the_collateral_total_agrees_with_capacity() -> None:
    """The total is read from the account rather than summed over the rendered rows, so this
    figure and the Capacity cell can never disagree on screen."""
    body = _portfolio_page()
    assert "Total collateral committed" in body
    assert body.count("$88,000") >= 2, "the footer total and the capacity note are one number"
    assert "$12,000" in body  # 100k cash - 88k committed


def test_assignment_is_blank_for_anything_but_a_short_put() -> None:
    """A short call is answered by the shares behind it and a long option cannot be assigned, so
    neither gets a watch — and neither should borrow the reassuring green of a safe put."""
    from wheel_screener.core.models import PositionKind

    account = _account_with_positions()
    calls = [p for p in account.positions if p.kind is PositionKind.SHORT_CALL]
    assert calls and all(p.in_the_money is None for p in calls)


def test_the_assignment_watch_distinguishes_itm_safe_and_unknown() -> None:
    """Three states, and the third must not read like the second: a put with no quote is not a
    put that is safe."""
    body = _portfolio_page()
    assert "ITM &middot; $185.00" in body   # AAPL: spot 185 < strike 190
    assert "OTM &middot; $455.00" in body   # MSFT: above the strike, and labelled as such
    assert "no quote" in body               # NVDA: unknown, and said so
    assert "in the money" not in body, "the long form was noise in a narrow column"


def test_holdings_list_every_asset_class_not_just_stocks() -> None:
    """"What do I hold" has to mean everything — a bond swept into a footnote of raw CUSIPs is
    not an answer."""
    body = _portfolio_page()
    assert "Holdings" in body
    for name in ("TSLA", "SPY", "US TREASURY BOND 4.5% 2044"):
        assert name in body
    assert "Stock" in body and "ETF" in body and "Bond" in body
    assert "912810FB9" in body, "the CUSIP stays visible under the readable name"


def test_covered_call_lots_distinguish_none_from_not_applicable() -> None:
    """A bond supports no covered calls, and that is a different statement from zero."""
    body = _portfolio_page()
    assert "2 contracts" in body, "250 TSLA covers two calls, not two and a half"
    assert "1 contract" in body, "150 SPY covers one — ETFs are writable too"
    assert "under 100" in body, "40 shares covers nothing, whatever it is worth"
    assert "n/a" in body, "a bond is not writable at all"


def test_no_position_is_invisible() -> None:
    """Every row the broker returns must appear somewhere. The previous layout showed short puts
    and shares, and quietly dropped short calls and bonds into a one-line footnote."""
    body = _portfolio_page()
    account = _account_with_positions()
    for p in account.positions:
        needle = p.description if p.symbol_is_cusip else p.underlying
        assert needle in body, f"{p.symbol} is not rendered anywhere"
    assert "Open options" in body and "short call" in body


def test_the_value_total_is_withheld_rather_than_understated() -> None:
    """One unpriced contract makes a sum of the rest a wrong number, not an approximate one: it
    reads as the whole book. The collateral total is unaffected — it is derived, not quoted."""
    body = _portfolio_page()          # the fixture has one option with no mark
    assert "Total collateral committed" in body and "$88,000" in body
    assert "-$880" not in body, "a partial value total must not be presented as the total"


def test_no_html_entity_is_double_escaped_on_the_page() -> None:
    """An entity written inside a {{ }} expression is escaped a second time and reaches the
    reader as the literal text "&mdash;". The character belongs in the expression; the entity
    only in the markup around it."""
    body = _portfolio_page()
    assert "&amp;mdash;" not in body and "&amp;middot;" not in body


# ── exit comparison ────────────────────────────────────────────────────────────────────────

def _exits_client(rows=None, spot=185.0, error=None, after=None, grid=None, early=None):
    from wheel_screener.api.deps import get_service
    from wheel_screener.core.exits import ExitOption

    # Shaped to the fixture's own AAPL $190 put, 2 contracts, spot $185 — a fake that answers
    # for a different position than the one clicked is a fake that will be believed.
    # ALTERNATIVES only — keep and rolls. Writing calls is a continuation, so it belongs in the
    # second list, exactly as the service returns it.
    default = [
        ExitOption(kind="keep", label="Keep to expiry", credit=600.0, days=20,
                   collateral=38000.0, extrinsic=600.0),
        ExitOption(kind="roll", label="Roll to 16 Oct", credit=420.0, days=28,
                   collateral=38000.0, extrinsic=420.0, strike=190.0),
        ExitOption(kind="roll", label="Roll to 25 Sep", credit=2300.0, days=7,
                   collateral=40000.0, extrinsic=-600.0, strike=200.0,
                   collateral_delta=2000.0,
                   warnings=("$200 is in the money at $185.00 — part of this credit"
                             " is intrinsic",)),
    ]
    # a continuation, not an alternative — the service returns it as its own list
    default_after = [
        ExitOption(kind="assign_cc", label="28-day $190 call", credit=1150.0,
                   days=28, collateral=37000.0, extrinsic=1150.0, strike=190.0),
    ]

    default_after = [
        ExitOption(kind="assign_cc", label="28-day $190 call", credit=1150.0,
                   days=28, collateral=37000.0, extrinsic=1150.0, strike=190.0),
    ]

    class _Svc:
        def brokerage_accounts(self):
            return [_account_with_positions()]

        def exit_options(self, symbol, strike, expiration, contracts, today, **kw):
            self.seen = dict(symbol=symbol, strike=strike, expiration=expiration,
                             contracts=contracts, **kw)
            if error:
                raise error
            # (alternatives, after-assignment, grid, spot, early assignment) — the second list
            # is not an alternative to anything in the first, which is why it comes back apart
            return (default if rows is None else rows), (
                default_after if after is None else after), grid, spot, early

    svc = _Svc()
    c = _client()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    _sign_in(c)
    return c, svc


def test_a_short_put_row_is_itself_the_control() -> None:
    """The row is the affordance — no separate button to find. Only short puts open: the
    comparison is written for an obligation with collateral behind it, which a long option and a
    covered call do not have."""
    c, _ = _exits_client()
    try:
        body = c.get("/portfolio").text
        assert 'class="row-open"' in body and "/portfolio/exits" in body
        assert 'hx-swap="afterend"' in body, "the panel opens under the row it belongs to"
        assert "ways out</button>" not in body, "the button it replaced is gone"
        # EVERY option opens — three short puts and a short call. All four kinds face the same
        # question; only the sign of the answer differs, so restricting it to short puts was an
        # arbitrary line.
        assert body.count('class="row-open"') == 4
        assert body.count('"option_type"') == 4 and body.count('"is_short"') == 4
        # ...and nothing that is not an option: a bond has no strike to roll
        assert "912810FB9" in body and body.count('class="row-open"') == 4
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_panel_is_a_table_row_so_the_table_stays_valid() -> None:
    c, _ = _exits_client()
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "2026-09-25"}).text
        assert body.lstrip().startswith("{#") or body.lstrip().startswith("<tr")
        assert '<tr class="detail exits-row">' in body and "colspan=" in body
        assert 'hx-target="closest tr"' in body, "repricing replaces the row, not a stray div"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_ways_out_prices_the_position_that_was_clicked() -> None:
    from datetime import date as _date

    c, svc = _exits_client()
    try:
        r = c.get("/portfolio/exits", params={
            "symbol": "AAPL", "strike": 190.0, "expiry": "2026-09-18", "contracts": 2})
        assert r.status_code == 200
        assert svc.seen["symbol"] == "AAPL" and svc.seen["strike"] == 190.0
        assert svc.seen["expiration"] == _date(2026, 9, 18)
        assert "Or keep it:" in r.text, "the baseline moved into the header with the grid"
        assert "$190 call" in r.text and "If assigned on 18 Sep" in r.text
        assert "$390" not in r.text, "the panel must answer for the position that was clicked"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_strike_and_dte_range_reach_the_service() -> None:
    """The controls are the point: same strike is only the DEFAULT."""
    c, svc = _exits_client()
    try:
        c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "2026-09-25", "contracts": 1,
            "roll_strike": "380", "min_dte": 20, "max_dte": 60})
        assert svc.seen["roll_strike"] == 380.0
        assert svc.seen["min_dte"] == 20 and svc.seen["max_dte"] == 60
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_a_blank_roll_strike_means_the_positions_own_strike() -> None:
    c, svc = _exits_client()
    try:
        c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "2026-09-25", "roll_strike": ""})
        assert svc.seen["roll_strike"] is None, "None lets the domain default to the held strike"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_an_inverted_dte_range_is_swapped_not_rejected() -> None:
    c, svc = _exits_client()
    try:
        c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "2026-09-25",
            "min_dte": 90, "max_dte": 10})
        assert svc.seen["min_dte"] == 10 and svc.seen["max_dte"] == 90
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_a_bad_expiry_is_a_422_not_a_traceback() -> None:
    c, _ = _exits_client()
    try:
        r = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "not-a-date"})
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_a_quote_failure_degrades_inside_the_panel() -> None:
    c, _ = _exits_client(error=AuthExpiredError("provider auth failed"))
    try:
        r = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "2026-09-25"})
        assert r.status_code == 200 and "provider auth failed" in r.text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_exit_panel_needs_a_session_like_the_rest_of_the_tab() -> None:
    c = _client()
    try:
        r = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": "2026-09-25"},
            follow_redirects=False)
        assert r.status_code in (303, 401, 403), "a stranger must not reach live account data"
    finally:
        c.__exit__(None, None, None)


def test_the_panel_lets_its_prose_wrap() -> None:
    """`th, td { white-space: nowrap }` is global and white-space inherits, so every sentence in
    the panel ran off the side of the page."""
    css = (pathlib.Path("src/wheel_screener/api/static/custom.css")).read_text()
    assert ".exits-row > td { white-space: normal; }" in css
    wrap = ".exits table td:first-child, .exits table th:first-child"
    assert wrap + " { white-space: normal; }" in css


def test_the_exit_table_declares_its_column_widths() -> None:
    """`table { width: 100% }` is global; under auto layout the surplus goes to the widest
    column, stranding a short action label at one end of a half-panel-wide cell."""
    css = pathlib.Path("src/wheel_screener/api/static/custom.css").read_text()
    assert ".exit-table { table-layout: fixed; max-width: 58rem; }" in css
    assert css.count(".exit-table th:nth-child(") == 5, "one declared width per column"
    # Scoped to that table by class. Unscoped, `.exits table th:nth-child(1) { width: 32% }`
    # also matched the roll grid and gave a third of an eight-column table to its row labels.
    assert ".exits table th:nth-child(" not in css


def test_post_assignment_calls_sit_apart_from_the_alternatives() -> None:
    """Assignment happens when the put expires, so writing calls follows keeping rather than
    competing with it. Ranked together, a near-dated call annualised to a huge rate and sat on
    top of the table as the best available action."""
    from wheel_screener.core.exits import ExitOption

    after = [ExitOption(kind="assign_cc", label="28-day $190 call", credit=900.0,
                        days=28, collateral=37000.0, extrinsic=900.0, strike=190.0)]
    c, _ = _exits_client(after=after)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AAPL", "strike": 190.0, "expiry": "2026-09-18", "contracts": 2}).text
        assert "If assigned on 18 Sep, writing a call would" in body
        assert "28-day $190 call" in body
        assert "Est. premium" in body and "Days held" in body and "Shares worth" in body
        # the ranked table above it must not have absorbed the row
        above = body[:body.index("If assigned on")]
        assert "28-day $190 call" not in above, "the continuation stays in its own section"
        assert "Or keep it:" in above
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_call_strike_control_sits_on_the_table_it_governs() -> None:
    """It changes the post-assignment rows and nothing else. Placed above the roll ladder it read
    as inert — change it, press Reprice, and the rows it does not touch stay put."""
    from wheel_screener.core.exits import ExitOption

    after = [ExitOption(kind="assign_cc", label="26-day $195 call", credit=900.0, days=26,
                        collateral=37000.0, extrinsic=900.0, strike=195.0)]
    c, _ = _exits_client(after=after)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AAPL", "strike": 190.0, "expiry": "2026-09-18",
            "call_strike": "195"}).text
        assert body.index('name="call_strike"') > body.index("If assigned on"), \
            "the control belongs below the heading of the table it drives"
        assert 'value="195"' in body, "the chosen strike is echoed back, not reset"
        # CONTAINMENT, not a reference. It previously claimed membership with form="exits-form",
        # an id the form never had — so the browser associated it with no form and never sent it.
        # Asserting the attribute was present passed happily while the control did nothing.
        opened, closed = body.index("<form"), body.index("</form>")
        assert opened < body.index('name="call_strike"') < closed
        assert "form=" not in body, "no control may depend on an id resolving"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_no_call_strike_control_when_there_is_nothing_for_it_to_do() -> None:
    """An out-of-the-money put has no post-assignment table, so the control would govern
    nothing at all."""
    c, _ = _exits_client(after=[])
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "QCOM", "strike": 150.0, "expiry": "2026-09-04"}).text
        assert "If assigned on" not in body
        assert 'name="call_strike"' not in body
        # the grid replaced the roll-strike box: you pick a cell rather than typing a strike
        assert 'name="roll_strike"' not in body
        assert 'name="min_dte"' in body, "the window controls still bound the grid"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_every_control_in_the_panel_is_inside_its_form() -> None:
    """The whole class of bug in one assertion: a control outside the form, or pointing at one by
    id, is a control whose value never reaches the server. Containment cannot silently fail."""
    import re

    c, _ = _exits_client()
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AAPL", "strike": 190.0, "expiry": "2026-09-18"}).text
        assert body.count("<form") == 1 and body.count("</form>") == 1
        opened, closed = body.index("<form"), body.index("</form>")
        for m in re.finditer(r'<(?:input|button|select|textarea)\b', body):
            assert opened < m.start() < closed, f"control at {m.start()} is outside the form"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_grid_replaced_the_ladder_rather_than_joining_it() -> None:
    """A roll is one decision over two axes, and the grid shows both. Leaving the old
    one-strike-at-a-time table beside it would list the same rolls twice."""
    from datetime import date as _d
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid
    from wheel_screener.core.models import OptionContract, OptionType

    today, exp = _d.today(), _d.today() + _td(days=26)
    chain = [
        OptionContract(underlying_symbol="AVGO", option_symbol=f"A{k}{d}",
                       option_type=OptionType.PUT, expiration=today + _td(days=d), strike=k,
                       dte=d, bid=b, ask=b + 0.35, delta=-0.6, open_interest=800,
                       volume=40, bid_size=25)
        for k, b in ((390.0, 33.4), (385.0, 30.3), (380.0, 27.4))
        for d, b in ((26, b), (33, b + 1.6), (40, b + 2.9))
    ]
    grid = rollgrid.build(chain, strike=390.0, expiration=exp, contracts=1, spot=368.75,
                          today=today, collected=36.10)
    c, _ = _exits_client(grid=grid)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": exp.isoformat(),
            "collected": "36.10"}).text
        assert 'class="roll-grid"' in body
        # scoped above the post-assignment table, which legitimately keeps its own rate column
        above = body.split("If assigned on")[0]
        assert "Roll to " not in above, "the ladder's rows are gone"
        assert "Rate/yr" not in above, "and so is its rate column"
        assert "Or keep it:" in above, "the baseline it carried lives in the header now"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


# ── ops: the link expires on a clock, so it has to be visible before it lapses ──────────────

def _health_with(link) -> dict:
    c = _client(link)
    try:
        return c.get("/health").json()
    finally:
        c.__exit__(None, None, None)


class _Link(_FakeLink):
    def __init__(self, connected=True, hours=None, configured=True):
        super().__init__(connected)
        self._hours, self._configured = hours, configured

    def status(self):
        expires = (datetime.now(tz=UTC) + timedelta(hours=self._hours)
                   if self._hours is not None else None)
        return BrokerLinkStatus(broker="schwab", configured=self._configured,
                                connected=self.connected, expires_at=expires)


def test_health_reports_how_long_the_broker_link_has_left() -> None:
    """It is the one part of the deployment that expires on a clock rather than breaking, so it
    is the one part an operator cannot find by waiting for an error."""
    body = _health_with(_Link(hours=100))
    schwab = next(b for b in body["brokers"] if b["broker"] == "schwab")
    assert schwab["connected"] is True and 99 <= schwab["expires_in_hours"] <= 100
    assert body["warnings"] == [], "four days out is not worth a warning"


def test_health_warns_before_the_link_lapses_without_moving_the_status() -> None:
    """Degrading here would fail the container healthcheck and roll back a release over a
    credential that was always going to expire, which redeploying cannot renew. Compared
    against a healthy link rather than to a literal, so the assertion is about the BROKER's
    effect and not about whatever else the test app's providers are doing."""
    healthy = _health_with(_Link(hours=100))
    expiring = _health_with(_Link(hours=12))
    assert expiring["status"] == healthy["status"], "the link must not move the status"
    assert any("expires in 12h" in w for w in expiring["warnings"])
    assert healthy["warnings"] == []


def test_health_says_when_a_configured_broker_has_nobody_signed_in() -> None:
    body = _health_with(_Link(connected=False, configured=True))
    assert any("configured but not connected" in w for w in body["warnings"])


def test_a_broker_that_cannot_be_read_does_not_break_health() -> None:
    class _Angry(_FakeLink):
        def status(self):
            raise RuntimeError("token file unreadable")

    body = _health_with(_Angry())
    assert body["status"] == _health_with(_Link(hours=100))["status"]
    assert next(b for b in body["brokers"] if b["broker"] == "schwab")["error"]


def test_the_roll_grid_is_read_not_clicked() -> None:
    """Moving assignment odds into the row labels left the click-through with nothing the grid
    did not already show, so it was a round trip to the chain provider for a redraw."""
    from datetime import date as _d
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid
    from wheel_screener.core.models import OptionContract, OptionType

    today, exp = _d.today(), _d.today() + _td(days=26)
    chain = [
        OptionContract(underlying_symbol="AVGO", option_symbol=f"A{k}{d}",
                       option_type=OptionType.PUT, expiration=today + _td(days=d), strike=k,
                       dte=d, bid=b, ask=b + 0.35, delta=-0.6, open_interest=800,
                       volume=40, bid_size=25)
        for k, b in ((390.0, 33.4), (385.0, 30.3), (380.0, 27.4))
        for d, b in ((26, b), (33, b + 1.6))
    ]
    grid = rollgrid.build(chain, strike=390.0, expiration=exp, contracts=1, spot=368.75,
                          today=today, collected=36.10)
    c, _ = _exits_client(grid=grid)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": exp.isoformat(),
            "collected": "36.10"}).text
        cells = body[body.index('class="roll-grid"'):body.index("</table>")]
        assert "<a " not in cells and "hx-get" not in cells, "cells are data, not controls"
        assert "assignment odds" in body, "what the click used to add now labels the rows"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_roll_cells_are_coloured_by_sign_and_gaps_are_left_blank() -> None:
    """A roll that pays and one that costs are opposite decisions; tinting every cell the same
    green said neither. A dash is a strike the exchange does not list for that expiry — the
    absence of a result rather than a result, so it carries no tint at all."""
    from datetime import date as _d
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid
    from wheel_screener.core.models import OptionContract, OptionType

    today, exp = _d.today(), _d.today() + _td(days=26)
    # $400 pays to roll into, $380 costs; $385 simply is not listed at 40 days
    chain = [
        OptionContract(underlying_symbol="AVGO", option_symbol=f"A{k}{d}",
                       option_type=OptionType.PUT, expiration=today + _td(days=d), strike=k,
                       dte=d, bid=b, ask=b + 0.35, delta=-0.6, open_interest=800,
                       volume=40, bid_size=25)
        for k, base in ((390.0, 33.4), (385.0, 30.3), (380.0, 27.4), (400.0, 40.5))
        for d, b in ((26, base), (40, base + 2.9))
        if not (k == 385.0 and d == 40)
    ]
    grid = rollgrid.build(chain, strike=390.0, expiration=exp, contracts=1, spot=368.75,
                          today=today, collected=36.10)
    c, _ = _exits_client(grid=grid)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AVGO", "strike": 390.0, "expiry": exp.isoformat()}).text
        assert "rg-pos" in body and "rg-neg" in body
        assert "rg-empty" in body, "an unlisted strike is a gap, not a zero"
        assert "does not list for that expiry" in body, "and the note says so"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_grid_marks_a_quote_nobody_trades_instead_of_showing_it_as_a_bargain() -> None:
    """KGC, 23 Aug 2026: the $30.5 put quoted 1.06 at 18 Sep, 0.74 at 25 Sep, 0.68 at 2 Oct —
    the bid FALLING as expiry extends, which no real option can do. Zero open interest, a 90%
    spread, no volume. The grid read it as the cheapest roll on the board and tinted it green,
    because it applied none of the four liquidity measures the screener applies. The adjacent
    $31.5 strike, which people actually trade, behaves properly and must stay tinted."""
    from datetime import date as _d
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid
    from wheel_screener.core.models import OptionContract, OptionType

    today, exp = _d.today(), _d.today() + _td(days=26)

    def put(k, d, bid, ask, oi, vol, bs):
        return OptionContract(
            underlying_symbol="KGC", option_symbol=f"K{k}{d}", option_type=OptionType.PUT,
            expiration=today + _td(days=d), strike=k, dte=d, bid=bid, ask=ask, delta=-0.45,
            open_interest=oi, volume=vol, bid_size=bs,
        )

    chain = [
        # the held leg, and the strike that trades: bids rise with time, as they must
        put(31.5, 26, 1.61, 1.72, 900, 10, 40), put(31.5, 40, 1.83, 1.95, 640, 5, 30),
        # the ghost: bid falls with time, 90% spread, nothing outstanding, nothing traded
        put(30.5, 26, 1.06, 1.67, 0, 0, 15), put(30.5, 40, 0.68, 1.79, 0, 1, 76),
    ]
    grid = rollgrid.build(chain, strike=31.5, expiration=exp, contracts=5, spot=31.1,
                          today=today, collected=1.50)
    assert grid is not None
    ghost = grid.cell(30.5, today + _td(days=40))
    assert ghost is not None and ghost.untradeable, "0 OI at a 90% spread is not a price"
    assert "spread" in ghost.untradeable
    real = grid.cell(31.5, today + _td(days=40))
    assert real is not None and real.untradeable is None, "the strike people trade must survive"

    c, _ = _exits_client(grid=grid)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "KGC", "strike": 31.5, "expiry": exp.isoformat()}).text
        assert "rg-thin" in body, "the ghost is marked"
        assert "Untradeable:" in body, "and says which measure it failed, on hover"
        assert "rg-pos" in body, "while the tradeable roll keeps its tint"
        # the mark REPLACES the sign tint: a fictional credit shown in green is the whole bug
        assert 'rg-thin"' in body or "rg-thin\"" in body
        assert "rg-pos rg-thin" not in body and "rg-thin rg-pos" not in body
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_grid_applies_the_screeners_measures_at_a_floor_suited_to_one_board() -> None:
    """The screen's four measures, but a looser open-interest floor, and the gap is deliberate.
    The screen picks a few names from ~800 and can demand 100 contracts outstanding; the grid
    describes ONE board. Measured at the screen's floor, AAPL's at-the-money 32-day put (88 open
    interest) came back untradeable and half of every board hatched — a mark that lands on
    everything is one the reader stops seeing."""
    import inspect
    from datetime import date as _d
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid
    from wheel_screener.core.models import OptionContract, OptionType, ScreenCriteria
    from wheel_screener.core.rollgrid import _liquidity_problem

    GRID_MIN_OI = inspect.signature(rollgrid.build).parameters["min_oi"].default

    def c(**kw):
        base = dict(bid=2.00, ask=2.20, open_interest=800, volume=40, bid_size=25)
        return OptionContract(
            underlying_symbol="X", option_symbol="X1", option_type=OptionType.PUT,
            expiration=_d.today() + _td(days=30), strike=100.0, dte=30, **{**base, **kw},
        )

    d = ScreenCriteria()
    kw = dict(max_spread=d.max_bid_ask_spread_pct, spread_exempt=d.spread_abs_exempt,
              min_oi=GRID_MIN_OI, min_volume=d.min_volume, min_bid_size=d.min_bid_size)
    # three of the four are the screen's own, unchanged
    assert (kw["max_spread"], kw["spread_exempt"]) == (0.30, 0.05)
    assert (kw["min_volume"], kw["min_bid_size"]) == (1, 10)
    # the fourth is looser here, and only here
    assert GRID_MIN_OI == 10 < d.min_open_interest == 100
    assert _liquidity_problem(c(), **kw) is None
    assert _liquidity_problem(c(open_interest=88), **kw) is None, "AAPL at the money is not thin"
    assert "no bid" in _liquidity_problem(c(bid=0.0), **kw)
    assert "spread" in _liquidity_problem(c(bid=0.40, ask=1.60), **kw)
    assert "open interest" in _liquidity_problem(c(open_interest=2), **kw), "KGC's ghost"
    assert "traded" in _liquidity_problem(c(volume=0), **kw)
    assert "contracts bid" in _liquidity_problem(c(bid_size=1), **kw)
    # the absolute exemption: a penny-wide spread on a cheap contract is not illiquidity
    assert _liquidity_problem(c(bid=0.05, ask=0.09), **kw) is None


def test_a_thin_leg_to_buy_back_warns_that_every_figure_is_uncertain() -> None:
    """The closing ask is subtracted from every cell at once, so when the leg being bought back
    is itself untraded the fault is not in any one cell — no per-cell mark can carry it."""
    from datetime import date as _d
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid
    from wheel_screener.core.models import OptionContract, OptionType

    today, exp = _d.today(), _d.today() + _td(days=26)
    chain = [
        # the held leg quotes 0.40/1.90 on nothing outstanding: a 130% spread
        OptionContract(underlying_symbol="ZZZ", option_symbol="Z1", option_type=OptionType.PUT,
                       expiration=exp, strike=50.0, dte=26, bid=0.40, ask=1.90, delta=-0.3,
                       open_interest=0, volume=0, bid_size=1),
        OptionContract(underlying_symbol="ZZZ", option_symbol="Z2", option_type=OptionType.PUT,
                       expiration=today + _td(days=40), strike=50.0, dte=40, bid=2.60,
                       ask=2.75, delta=-0.3, open_interest=700, volume=30, bid_size=20),
    ]
    grid = rollgrid.build(chain, strike=50.0, expiration=exp, contracts=1, spot=52.0,
                          today=today, collected=1.00)
    assert grid is not None and grid.close_untradeable
    c, _ = _exits_client(grid=grid)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "ZZZ", "strike": 50.0, "expiry": exp.isoformat()}).text
        assert "Every figure above is uncertain" in body
        assert "expect to pay less" in body
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_roll_grid_is_laid_out_to_fit_rather_than_to_scroll() -> None:
    css = pathlib.Path("src/wheel_screener/api/static/custom.css").read_text()
    assert ".roll-grid { table-layout: fixed; width: 100%;" in css
    assert ".roll-grid td.rg-empty { color: var(--pico-muted-color); background: none; }" in css


# ── early assignment + ex-dividend on held positions ────────────────────────────────────────

def _page_for(account) -> str:
    from wheel_screener.api.deps import get_service

    class _Svc:
        def brokerage_accounts(self):
            return [account]

    c = _client()
    svc = _Svc()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    try:
        _sign_in(c)
        app.state.balances_cache = None
        return c.get("/portfolio").text
    finally:
        app.dependency_overrides.pop(get_service, None)
        c.__exit__(None, None, None)


def _covered_call_account(risk: str):
    from datetime import date as _date

    from wheel_screener.core.models import (
        AssignmentCause,
        BrokerageAccount,
        Dividend,
        EarlyAssignment,
        Position,
        PositionKind,
    )

    ex = _date(2026, 10, 9)
    div = Dividend(ex_date=ex, amount=0.71, frequency="quarterly")
    early = EarlyAssignment(
        risk=risk, cause=AssignmentCause.DIVIDEND if risk != "low" else None,
        on=_date(2026, 10, 8), in_the_money=risk != "low", intrinsic=2.0,
        time_value=0.13, threshold=0.71, dividends=[div],
    )
    return BrokerageAccount(
        broker="schwab", account_id="h", display_name="...123",
        positions=[Position(
            symbol="VZ    261016C00050000", underlying="VZ", kind=PositionKind.SHORT_CALL,
            asset_type="OPTION", option_type="call", quantity=2, strike=50.0,
            expiration=_date(2026, 10, 16), dte=35, market_value=-426.0, underlying_price=52.0,
            dividends=[div], early_assignment=early,
        )],
    )


def test_a_covered_call_set_for_early_assignment_is_flagged_on_its_row() -> None:
    """Before, a short call's assignment cell was always blank: its assignment is the plan. An
    EARLY one is not — it takes the dividend with it — so the row now says so."""
    import re

    body = _page_for(_covered_call_account("likely"))
    assert "early assignment likely" in body and "08 Oct" in body
    assert "badge--neg" in body and "badge--div" in body  # the verdict + the ex-date marker
    tip = re.search(r'badge--assign"\s+title="([^"]*)"', body).group(1)
    assert "$0.13 of time value against the $0.71 dividend" in tip
    assert "broker&#39;s mark" in tip or "broker's mark" in tip  # the row is an estimate


def test_a_quiet_covered_call_keeps_its_blank_cell_but_still_shows_the_ex_date() -> None:
    body = _page_for(_covered_call_account("low"))
    assert "badge--assign" not in body  # no verdict badge: not worth one in the list
    assert "badge--div" in body, "the dividend ahead is still worth knowing"


def test_the_exits_panel_explains_the_early_assignment_verdict() -> None:
    from wheel_screener.core.models import AssignmentCause, EarlyAssignment

    early = EarlyAssignment(
        risk="likely", cause=AssignmentCause.INTEREST, in_the_money=True, intrinsic=5.0,
        time_value=0.12, threshold=0.41,
    )
    c, _ = _exits_client(early=early)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AAPL", "strike": 190, "expiry": "2026-09-18", "contracts": 2,
        }).text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert "early assignment likely" in body and "Likely, any day." in body
    assert "~$0.41 of interest" in body and "$190.00" in body
    assert "Not modelled" in body and "tender offers" in body
    assert body.index("assign-watch") < body.index("exit-controls"), "said before the ways out"


def test_the_exits_panel_names_the_day_a_covered_call_goes() -> None:
    account = _covered_call_account("likely")
    c, _ = _exits_client(early=account.positions[0].early_assignment, spot=52.0)
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "VZ", "strike": 50, "expiry": "2026-10-16", "contracts": 2,
            "option_type": "call",
        }).text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert "Likely on 08 Oct." in body and "called away" in body
    assert "$142.00 on" in body  # $0.71 x 100 x 2 contracts of dividend lost
    assert "Ex-dividend 2026-10-09" in body


def test_no_verdict_no_section() -> None:
    c, _ = _exits_client()
    try:
        body = c.get("/portfolio/exits", params={
            "symbol": "AAPL", "strike": 190, "expiry": "2026-09-18", "contracts": 2,
        }).text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert "assign-watch" not in body


# ── the put swap rule: the Close? column ────────────────────────────────────────────────────

def _swap_account(*verdicts):
    """An account of open short puts, each carrying a prepared verdict."""
    from datetime import date as _date

    from wheel_screener.core.models import BrokerageAccount, Position, PositionKind

    positions = []
    for i, _ in enumerate(verdicts):
        positions.append(Position(
            symbol=f"SYM{i}  261016P00090000", underlying=f"SYM{i}",
            kind=PositionKind.SHORT_PUT, asset_type="OPTION", option_type="put", quantity=2,
            strike=90.0, expiration=_date(2026, 10, 16), dte=25, underlying_price=110.0,
            collateral=18_000.0, market_value=-70.0,
        ))
    return BrokerageAccount(broker="schwab", account_id="s", display_name="...123",
                            positions=positions)


def _swap_review(action: str, **kw):
    from datetime import date as _date

    from wheel_screener.core.models import SwapReview, SwapSuggestion

    base = dict(
        old_yield=0.0579, fresh_yield=0.2454, fresh_source="same ticker",
        extra_premium=220.0, cash=18_000.0, days=25, rule1_passed=True, rule2_passed=True,
        min_ratio=2.0, min_extra=100.0, swap_cost=10.0,
        suggestions=[SwapSuggestion(
            symbol="SYM0", strike=85.0, expiration=_date(2026, 10, 30), dte=39, delta=-0.2,
            bid=2.0, annualized_yield=0.2454, collateral=8_500.0, same_ticker=True,
        )],
    )
    base.update(kw)
    return SwapReview(action=action, reason=kw.pop("reason", "both rules passed"), **{
        k: v for k, v in base.items() if k != "reason"})


def _swap_client(account, reviews):
    """A service whose swap_reviews stamps `reviews` (one per position) and counts its calls."""
    from wheel_screener.api.deps import get_service

    class _Svc:
        calls = 0

        def brokerage_accounts(self):
            return [account]

        def swap_reviews(self, positions, candidates, today, criteria=None):
            type(self).calls += 1
            for p, r in zip(positions, reviews, strict=False):
                p.swap = r

    _Svc.calls = 0
    svc = _Svc()
    c = _client()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    _sign_in(c)
    app.state.balances_cache = None
    app.state.swap_cache = None
    return c, svc


def test_the_close_column_says_yes_or_no_and_opens_the_reasoning() -> None:
    account = _swap_account("swap")
    c, _ = _swap_client(account, [_swap_review("swap")])
    try:
        body = c.get("/portfolio").text
        assert "Close?" in body and ">Yes<" in body
        # the cell opens a panel of its own, and must not also trigger the row's ways-out panel
        assert "/portfolio/swap?position=" in body and "click consume" in body
        panel = c.get("/portfolio/swap", params={"position": account.positions[0].symbol}).text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert "swap it" in panel and "Rule 1" in panel and "Rule 2" in panel
    assert "4.2x" in panel  # 0.2454 / 0.0579, the ratio rule 1 turned on
    assert "$220.00" in panel and "$100.00" in panel  # the extra, against the floor
    assert "What to open instead" in panel and "same ticker" in panel
    assert "Draft rule" in panel  # it has not been backtested; the panel says so


def test_a_keep_verdict_still_opens_and_names_the_rule_that_held_it() -> None:
    review = _swap_review(
        "keep", reason="rule 1: a fresh put pays 1.2x this one, under the 2x the rule asks for",
        rule1_passed=False, rule2_passed=None, extra_premium=None,
    )
    account = _swap_account("keep")
    c, _ = _swap_client(account, [review])
    try:
        body = c.get("/portfolio").text
        assert ">No<" in body
        panel = c.get("/portfolio/swap", params={"position": account.positions[0].symbol}).text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert "keep it" in panel and "under the 2x the rule asks for" in panel
    assert "not reached" in panel  # rule 2 was never evaluated, and does not read as passed
    assert "What it was measured against" in panel


def test_an_out_of_scope_put_shows_a_dash_rather_than_a_verdict() -> None:
    account = _swap_account("n/a")
    review = _swap_review("n/a", reason="the stock is at or below the strike", old_yield=None)
    c, _ = _swap_client(account, [review])
    try:
        body = c.get("/portfolio").text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert ">Yes<" not in body and ">No<" not in body
    assert "at or below the strike" in body  # as the cell's tooltip


def test_verdicts_are_cached_between_page_loads_and_the_button_re_prices() -> None:
    """A verdict costs a chain pull per put, so reopening the tab must not spend them again —
    but the Refresh button has to, or it would be a no-op dressed as an action."""
    account = _swap_account("swap")
    c, svc = _swap_client(account, [_swap_review("swap")])
    try:
        c.get("/portfolio")
        assert svc.calls == 1
        app.state.balances_cache = None  # force a fresh broker read; the verdicts stay cached
        c.get("/portfolio")
        assert svc.calls == 1, "the second page load re-used the cached verdict"
        refreshed = c.post("/portfolio/swaps/refresh")
        assert refreshed.status_code == 200 and svc.calls == 2
        assert "Close?" in refreshed.text and ">Yes<" in refreshed.text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_the_refresh_button_is_rate_limited() -> None:
    from wheel_screener.api.ratelimit import is_expensive

    assert is_expensive("POST", "/portfolio/swaps/refresh")
    assert not is_expensive("GET", "/portfolio/swap")  # reads the cache; cheap


def test_the_swap_endpoints_need_a_session() -> None:
    c = _client()
    try:
        page = c.get("/portfolio/swap?position=X", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login?next=/portfolio/swap%3Fposition%3DX"
        post = c.post("/portfolio/swaps/refresh", follow_redirects=False)
        assert post.status_code == 303 and post.headers["location"] == "/login?next=/portfolio"
    finally:
        c.__exit__(None, None, None)


def test_a_fragment_request_after_the_session_ends_moves_the_whole_page() -> None:
    """Refresh or a Close? cell, clicked on a page whose session has since ended. A redirect would
    be followed invisibly by the browser's XHR and htmx would swap the entire sign-in page into a
    table cell; HX-Redirect makes it navigate instead."""
    c = _client()
    try:
        for method, path in (("POST", "/portfolio/swaps/refresh"),
                             ("GET", "/portfolio/swap?position=X")):
            r = c.request(method, path, headers={"HX-Request": "true"}, follow_redirects=False)
            assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login?next=/portfolio"
            assert "location" not in r.headers
    finally:
        c.__exit__(None, None, None)


def test_the_panel_survives_the_values_the_row_puts_on_every_request() -> None:
    """The row this link sits in carries hx-vals for the ways-out panel, and htmx merges an
    ancestor's hx-vals into the child's request. When the panel took a `symbol` parameter, the
    row's own `symbol` (the underlying) arrived last and won, the lookup missed, and the 404
    left htmx with nothing to swap — so clicking Yes did nothing at all.
    """
    account = _swap_account("swap")
    osi = account.positions[0].symbol
    c, _ = _swap_client(account, [_swap_review("swap")])
    try:
        c.get("/portfolio")
        # exactly what htmx sends: the link's own parameter, then the row's inherited values
        r = c.get(f"/portfolio/swap?position={osi.replace(' ', '%20')}"
                  "&symbol=SYM0&strike=90.0&expiry=2026-10-16&contracts=2.0"
                  "&option_type=put&is_short=true&collected=&opened=")
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert r.status_code == 200 and "swap it" in r.text


def test_a_covered_calls_verdict_reaches_the_page_and_its_panel_opens() -> None:
    """Both were app-layer filters that said SHORT_PUT: the service could answer for calls, but
    the web layer never passed them in, and the panel could not find one to render."""
    from datetime import date as _date

    from wheel_screener.api.deps import get_service
    from wheel_screener.core.models import (
        BrokerageAccount,
        OptionType,
        Position,
        PositionKind,
        SwapAction,
    )
    from wheel_screener.core.swap import OpenCall, review_covered_call

    call = Position(
        symbol="KO    261030C00075000", underlying="KO", kind=PositionKind.SHORT_CALL,
        asset_type="OPTION", option_type=OptionType.CALL, quantity=1, strike=75.0,
        expiration=_date(2026, 10, 30), dte=30, underlying_price=66.20, market_value=-5.0,
    )
    account = BrokerageAccount(broker="schwab", account_id="a", display_name="...1",
                               positions=[call])
    verdict = review_covered_call(OpenCall("KO", 75.0, 30, 1, 66.20, 0.05))
    assert verdict.action is SwapAction.IDLE

    class _Svc:
        def brokerage_accounts(self):
            return [account]

        def swap_reviews(self, positions, candidates, today, criteria=None):
            assert [p.symbol for p in positions] == [call.symbol], "the call must reach the service"
            for p in positions:
                p.swap = verdict

    c = _client()
    svc = _Svc()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    try:
        _sign_in(c)
        app.state.balances_cache = None
        app.state.swap_cache = None
        body = c.get("/portfolio").text
        assert ">idle<" in body and "OTM &middot; $66.20" in body
        panel = c.get("/portfolio/swap", params={"position": call.symbol}).text
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
    assert "unknown position" not in panel
    assert "earning almost nothing" in panel and "Not a recommendation" in panel
    assert "frees no capital" in panel  # why it is not the put rule with the sides swapped


def test_the_password_gate_can_cover_the_portfolio_alone() -> None:
    """The v3 phase-0 posture: anyone may screen, only the owner may link a broker.

    The connect route is the one that matters. It is exempt from the SESSION gate by necessity — a
    visitor cannot hold a session before signing in — so if the password did not cover it, any
    visitor with a brokerage account of their own could complete the OAuth exchange, overwrite the
    stored credential and end the owner's sessions.
    """
    from wheel_screener.api.app import _Auth

    c = _client()
    app.state.auth = _Auth("admin", "s3cret")
    app.state.auth_scope = "portfolio"
    try:
        # 401 without following redirects: the password gate runs OUTSIDE the session gate, so an
        # unauthenticated request is challenged rather than bounced to the Connect page first.
        for path in ("/portfolio", "/portfolio/oauth/schwab/connect",
                     "/portfolio/oauth/schwab/callback?state=x", "/portfolio/positions"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert r.headers.get("www-authenticate", "").startswith("Basic")
        assert c.post("/portfolio/swaps/refresh", follow_redirects=False).status_code == 401
        # the screener is untouched: a 404 means the request passed the gate and reached routing
        assert c.get("/nope").status_code == 404
        assert c.get("/health").status_code != 401
        # with the password, the gate passes and the tab renders
        assert c.get("/portfolio", auth=("admin", "s3cret")).status_code == 200
    finally:
        app.state.auth = None
        app.state.auth_scope = "site"
        c.__exit__(None, None, None)


# --- keeping two people apart ---------------------------------------------------------------
# The leak the per-user caches exist to close. With one Schwab token per deployment only one person
# can see an account at a time — which is itself the stronger protection — so these tests hand the
# link from one person to the other between page loads. That is a real scenario (an admin relinking
# as someone else), and exactly when a cache keyed by anything but the person would leak: the
# account on the other side of the hand-over is a different one, and the old numbers are still warm.
# The owner is set straight in the store, deliberately bypassing the callback, whose clearing of
# the previous owner's entries would otherwise hide a partition that does not work.


def _two_people() -> tuple[tuple, tuple]:
    store = app.state.users
    people = []
    for name in ("Alice", "Bob"):
        user = store.create_user(name, is_admin=True)
        token, _ = store.create_session(user.id, timedelta(days=1))
        people.append((user, token))
    return people[0], people[1]


def _be(c, person) -> None:
    """Use the page as this person, who now holds the broker link."""
    user, token = person
    c.cookies.set(app.state.settings.portfolio.cookie_name, token)
    app.state.users.set_link_owner("schwab", user.id)


def test_one_person_is_never_served_another_persons_balances() -> None:
    """Before the partition, the balances cache was a single 30-second entry with no key at all:
    whoever loaded the tab second inside the window was handed the first one's account."""
    class _TwoPeople:
        """A different account on each upstream read, so a cache hit is visible on the page."""

        def __init__(self) -> None:
            self.calls = 0

        def brokerage_accounts(self):
            self.calls += 1
            return [_account(display_name="••••AAAA" if self.calls == 1 else "••••BBBB")]

        def swap_reviews(self, *args, **kwargs) -> None:
            pass

    c = _client()
    people = _TwoPeople()
    app.dependency_overrides[get_service] = lambda: people
    app.dependency_overrides[get_portfolio] = lambda: people
    _reset_caches()
    a, b = _two_people()
    try:
        _be(c, a)
        assert "••••AAAA" in c.get("/portfolio").text
        assert people.calls == 1

        _be(c, b)
        second = c.get("/portfolio").text
        assert "••••AAAA" not in second, "the second person was served the first one's balances"
        assert "••••BBBB" in second
        assert people.calls == 2, "the second person must cost their own upstream read"

        _be(c, a)
        assert "••••AAAA" in c.get("/portfolio").text  # its own partition, still warm
        assert people.calls == 2
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_one_person_is_never_served_another_persons_verdict() -> None:
    """Same story for the Close? column, whose key was the contract and the size — identical for
    two people holding the same put, so they shared an entry."""
    account = _swap_account("swap")
    reviews = [_swap_review("swap"), _swap_review("keep", reason="rule 1 did not pass",
                                                 rule1_passed=False)]

    class _Svc:
        def __init__(self) -> None:
            self.calls = 0

        def brokerage_accounts(self):
            return [account]

        def swap_reviews(self, positions, candidates, today, criteria=None) -> None:
            verdict = reviews[min(self.calls, len(reviews) - 1)]
            self.calls += 1
            for position in positions:
                position.swap = verdict

    c = _client()
    svc = _Svc()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    _reset_caches()
    a, b = _two_people()
    try:
        _be(c, a)
        assert ">Yes<" in c.get("/portfolio").text
        assert svc.calls == 1

        _be(c, b)
        second = c.get("/portfolio").text
        assert ">No<" in second, "the second person was served the first one's verdict"
        assert ">Yes<" not in second
        assert svc.calls == 2
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_one_persons_refresh_does_not_cost_everybody_their_cache() -> None:
    """Refresh used to replace the single process-wide dict, so it emptied every user's."""
    account = _swap_account("swap")

    class _Svc:
        def __init__(self) -> None:
            self.reads = 0

        def brokerage_accounts(self):
            self.reads += 1
            return [account]

        def swap_reviews(self, positions, candidates, today, criteria=None) -> None:
            for position in positions:
                position.swap = _swap_review("swap")

    c = _client()
    svc = _Svc()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_portfolio] = lambda: svc
    _reset_caches()
    a, b = _two_people()
    try:
        _be(c, a)
        c.get("/portfolio")
        _be(c, b)
        c.get("/portfolio")
        assert svc.reads == 2 and len(app.state.balances_cache) == 2

        _be(c, b)
        c.post("/portfolio/swaps/refresh")
        assert len(app.state.balances_cache) == 2, "A's partition survived B pressing Refresh"

        before = svc.reads
        _be(c, a)
        c.get("/portfolio")
        assert svc.reads == before, "A's balances were still cached"
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)


def test_production_passkeys_name_the_site_people_actually_use() -> None:
    """A passkey is bound to a hostname and checked against the exact origin. Deployed with the
    localhost defaults, every sign-in would be refused — and nothing would say why until someone
    tried. The broker's callback already names the production site, so the two must agree."""
    compose = (pathlib.Path(__file__).parents[2] / "docker-compose.yml").read_text()
    env = dict(
        line.strip().split(": ", 1) for line in compose.splitlines()
        if line.strip().startswith(("PASSKEYS__", "SCHWAB__CALLBACK_URL"))
    )
    assert env["PASSKEYS__RP_ID"] == "steadybull.net"
    assert env["PASSKEYS__ORIGIN"] == "https://steadybull.net"
    assert env["SCHWAB__CALLBACK_URL"].startswith(env["PASSKEYS__ORIGIN"] + "/")


def test_the_www_name_redirects_rather_than_serving_the_site() -> None:
    """A passkey ceremony on www reports origin https://www.steadybull.net, which is refused, and a
    session cookie set there does not reach the bare domain. So www must only ever redirect."""
    caddy = (pathlib.Path(__file__).parents[2] / "deploy" / "caddy" / "Caddyfile").read_text()
    blocks = {}
    for chunk in caddy.split("\n}\n"):
        header = next((ln for ln in chunk.splitlines() if ln.rstrip().endswith("{")
                       and not ln.startswith(("\t", " "))), None)
        if header:
            blocks[header.rstrip(" {")] = chunk
    assert set(blocks) == {"www.steadybull.net", "steadybull.net"}, blocks.keys()
    assert "redir https://steadybull.net{uri} permanent" in blocks["www.steadybull.net"]
    assert "reverse_proxy" not in blocks["www.steadybull.net"]
    assert "reverse_proxy app:8000" in blocks["steadybull.net"]


def test_production_has_no_password_prompt_in_front_of_the_portfolio() -> None:
    """The passkey sign-in page is the only way in. The Basic-Auth gate put there in v3.0.0 was
    the browser's grey pop-up, and it offered no way in of its own if passkeys failed. .env still
    holds a password, so compose must blank it explicitly or the prompt comes back on deploy."""
    compose = (pathlib.Path(__file__).parents[2] / "docker-compose.yml").read_text()
    env = dict(
        line.strip().split(": ", 1) for line in compose.splitlines()
        if line.strip().startswith("AUTH__")
    )
    assert env == {"AUTH__REQUIRED": '"false"', "AUTH__PASSWORD": '""'}


# --- the invites page ------------------------------------------------------------------------

def _link_in(html: str) -> str:
    import re

    found = re.search(r'value="(http://[^"]+/invite/[^"]+)"', html)
    assert found, "no invite link on the page"
    return found.group(1)


def test_an_admin_makes_an_invite_and_the_link_creates_a_working_account() -> None:
    c = _client()
    try:
        _as(c, "Sam")
        page = c.get("/portfolio/invites")
        assert page.status_code == 200 and "Create invite link" in page.text
        made = c.post("/portfolio/invites", data={"name": "Alex"})
        link = _link_in(made.text)
        token = link.rsplit("/", 1)[-1]
        assert "Invite for Alex" in made.text and "only time it is shown" in made.text

        c.cookies.clear()  # Alex, on their own device
        r, _ = _register_over_http(c, token)
        assert r.status_code == 200
        alex = next(u for u in app.state.users.users() if u.name == "Alex")
        assert not alex.is_admin, "an admin only when the box is ticked"
    finally:
        c.__exit__(None, None, None)


def test_ticking_admin_makes_an_admin() -> None:
    c = _client()
    try:
        _as(c, "Sam")
        token = _link_in(c.post("/portfolio/invites", data={"name": "Pat", "admin": "1"}).text)
        c.cookies.clear()
        _register_over_http(c, token.rsplit("/", 1)[-1])
        assert next(u for u in app.state.users.users() if u.name == "Pat").is_admin
    finally:
        c.__exit__(None, None, None)


def test_a_link_is_shown_once_and_never_again() -> None:
    """The pending list works by a non-secret reference, so reloading the page cannot re-print
    a live token for someone looking over a shoulder, or a screenshot, to use."""
    c = _client()
    try:
        _as(c, "Sam")
        token = _link_in(c.post("/portfolio/invites", data={"name": "Alex"}).text).rsplit("/")[-1]
        later = c.get("/portfolio/invites").text
        assert "Alex" in later and "creates an account" in later
        assert token not in later
    finally:
        c.__exit__(None, None, None)


def test_cancelling_an_invite_kills_the_link() -> None:
    c = _client()
    try:
        _as(c, "Sam")
        token = _link_in(c.post("/portfolio/invites", data={"name": "Alex"}).text).rsplit("/")[-1]
        (pending,) = app.state.users.pending_invites()
        after = c.post("/portfolio/invites/cancel", data={"ref": pending.ref}).text
        assert "No invites outstanding" in after
        assert c.get(f"/invite/{token}").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_a_new_passkey_link_adds_to_the_same_account() -> None:
    c = _client()
    try:
        sam = _as(c, "Sam")
        made = c.post("/portfolio/invites", data={"for_user": sam.id}).text
        assert "New passkey link for Sam" in made
        token = _link_in(made).rsplit("/", 1)[-1]
        c.cookies.clear()
        assert "Add a passkey" in c.get(f"/invite/{token}").text
        r, _ = _register_over_http(c, token)
        assert r.status_code == 200 and len(app.state.users.users()) == 1
        assert len(app.state.users.credentials_for(sam.id)) == 1
    finally:
        c.__exit__(None, None, None)


def test_an_invite_needs_a_name() -> None:
    c = _client()
    try:
        _as(c, "Sam")
        for name in ("", "   ", "x" * 61):
            r = c.post("/portfolio/invites", data={"name": name})
            assert "Give the invite a name" in r.text
        assert app.state.users.pending_invites() == []
    finally:
        c.__exit__(None, None, None)


def test_only_an_admin_can_invite() -> None:
    c = _client()
    try:
        _as(c, "Alex", admin=False, owns_link=False)
        assert c.get("/portfolio/invites").status_code == 403
        assert c.post("/portfolio/invites", data={"name": "Mallory"}).status_code == 403
        assert c.post("/portfolio/invites", data={"name": "M", "admin": "1"}).status_code == 403
        assert app.state.users.pending_invites() == []
        assert "Invite people" not in c.get("/portfolio").text
        c.cookies.clear()
        stranger = c.get("/portfolio/invites", follow_redirects=False)
        assert stranger.status_code == 303 and stranger.headers["location"].startswith("/login")
    finally:
        c.__exit__(None, None, None)


def test_a_member_cannot_cancel_invites_either() -> None:
    c = _client()
    try:
        _as(c, "Sam")
        c.post("/portfolio/invites", data={"name": "Alex"})
        (pending,) = app.state.users.pending_invites()
        _as(c, "Eve", admin=False, owns_link=False)
        assert c.post("/portfolio/invites/cancel", data={"ref": pending.ref}).status_code == 403
        assert len(app.state.users.pending_invites()) == 1
    finally:
        c.__exit__(None, None, None)


def test_the_proxy_config_is_mounted_as_a_directory() -> None:
    """A single-file bind mount is pinned to the file's inode, and `git checkout` replaces files
    rather than editing them — so the proxy kept reading the old Caddyfile after every deploy, and
    v3.3.0's www redirect shipped without taking effect. A directory mount sees the new file."""
    compose = (pathlib.Path(__file__).parents[2] / "docker-compose.yml").read_text()
    assert "- ./deploy/caddy:/etc/caddy:ro" in compose
    assert "Caddyfile:/etc/caddy/Caddyfile" not in compose


def test_an_expired_contract_says_expired_instead_of_a_negative_day_count() -> None:
    from datetime import date as _date

    from wheel_screener.core.models import OptionType, Position, PositionKind

    account = _account()
    account.positions = [Position(
        symbol="LRCX  260925P00270000", underlying="LRCX", kind=PositionKind.SHORT_PUT,
        asset_type="OPTION", option_type=OptionType.PUT, quantity=1, strike=270.0,
        expiration=_date(2026, 9, 25), dte=-1, collateral=27_000.0, underlying_price=315.19,
        market_value=-1.0,
    )]
    c = _signed_in(_AccountService([account]))
    try:
        body = c.get("/portfolio").text
        assert ">expired</span>" in body and "expired worthless" in body
        assert "<td>-1</td>" not in body
        assert "cash free after $0.00" in body  # its $27,000 is free again
    finally:
        app.dependency_overrides.clear()
        c.__exit__(None, None, None)
