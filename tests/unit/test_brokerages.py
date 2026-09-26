"""Linking brokerages through SnapTrade: the routes, the encrypted secret, and keeping people apart.

The fake SnapTrade below checks, on every call, that the secret it was handed is the one it issued
to THAT person — so a request built with the wrong person's identity fails loudly here, instead of
quietly showing someone else's account.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("fastapi")

from cryptography.fernet import Fernet  # noqa: E402
from test_portfolio_sessions import _as, _client, _reset_caches  # noqa: E402

from wheel_screener.api.app import app  # noqa: E402
from wheel_screener.api.secretbox import SecretBox  # noqa: E402
from wheel_screener.core.errors import AuthExpiredError  # noqa: E402
from wheel_screener.core.portfolio import AllAccounts  # noqa: E402

PORTAL = "https://app.snaptrade.com/portal/abc"


class FakeSnapTrade:
    def __init__(self) -> None:
        self.issued: dict[str, str] = {}
        self.connections_of: dict[str, list[dict]] = {}
        self.accounts_of: dict[str, list[dict]] = {}
        self.portal_calls: list[tuple] = []
        self.removed: list[tuple] = []

    def _check(self, user) -> None:
        assert self.issued.get(user.user_id) == user.secret, "called with someone else's secret"

    def register_user(self, user_id):
        assert user_id not in self.issued, "registered twice"
        self.issued[user_id] = f"secret-of-{user_id}"
        return self.issued[user_id]

    def portal_url(self, user, *, redirect, reconnect=None):
        self._check(user)
        self.portal_calls.append((user.user_id, redirect, reconnect))
        return PORTAL

    def connections(self, user):
        self._check(user)
        return self.connections_of.get(user.user_id, [])

    def remove_connection(self, user, connection_id):
        self._check(user)
        self.removed.append((user.user_id, connection_id))

    def accounts(self, user):
        self._check(user)
        return self.accounts_of.get(user.user_id, [])

    def balances(self, user, account_id):
        self._check(user)
        return [{"currency": {"code": "USD"}, "cash": 1000.0, "buying_power": 1000.0}]

    def positions(self, user, account_id):
        self._check(user)
        return []

    def activities(self, user, account_id, start, end):
        self._check(user)
        return []


def _account(number: str, institution="Fidelity"):
    return {"id": f"acct-{number}", "institution_name": institution, "number": number,
            "raw_type": "Cash", "status": "open",
            "balance": {"total": {"amount": 5000.0, "currency": "USD"}}}


def _connection(cid, name="Fidelity", disabled=False):
    return {"id": cid, "brokerage": {"display_name": name}, "disabled": disabled}


@pytest.fixture
def snap():
    c = _client()
    fake = FakeSnapTrade()
    app.state.snaptrade = fake
    app.state.secretbox = SecretBox(Fernet.generate_key().decode())
    _reset_caches()
    yield c, fake
    app.state.snaptrade = app.state.secretbox = None
    c.__exit__(None, None, None)


def _link(c, fake, user):
    """What the person does: press Link, go through SnapTrade's portal, come back."""
    r = c.post("/portfolio/brokerages/link", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PORTAL
    back = c.get("/portfolio/brokerages/return", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/portfolio"


# --- linking ---------------------------------------------------------------------------------

def test_the_first_link_registers_under_the_internal_id_and_goes_to_the_portal(snap) -> None:
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    assert "Link a brokerage" in c.get("/portfolio").text
    _link(c, fake, alex)
    assert list(fake.issued) == [alex.id], "registered by internal id — never a name or email"
    (who, redirect, reconnect) = fake.portal_calls[0]
    assert who == alex.id and reconnect is None
    # From the configured site address — never from the request's Host header, which a client
    # chooses, so a forged one cannot steer where SnapTrade sends the person afterwards.
    origin = app.state.settings.passkeys.origin
    assert redirect == f"{origin}/portfolio/brokerages/return"
    c.post("/portfolio/brokerages/link", headers={"Host": "evil.example"},
           follow_redirects=False)
    assert fake.portal_calls[-1][1].startswith(origin)


def test_a_second_link_does_not_register_again(snap) -> None:
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    _link(c, fake, alex)
    _link(c, fake, alex)  # FakeSnapTrade.register_user refuses a second registration
    assert len(fake.portal_calls) == 2


def test_the_stored_secret_is_encrypted(snap) -> None:
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    _link(c, fake, alex)
    sealed = app.state.users.snaptrade_secret(alex.id)
    assert sealed and fake.issued[alex.id].encode() not in sealed
    path = app.state.users._path
    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_checkpoint(FULL)")
    con.close()
    assert fake.issued[alex.id].encode() not in path.read_bytes()


def test_linked_accounts_appear_on_the_page(snap) -> None:
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    _link(c, fake, alex)
    fake.connections_of[alex.id] = [_connection("conn-a")]
    fake.accounts_of[alex.id] = [_account("Z-1111")]
    _reset_caches()
    body = c.get("/portfolio").text
    assert "Fidelity ••••1111" in body and "Link another brokerage" in body
    assert "badge--ok\">connected</span>" in body


# --- two people ------------------------------------------------------------------------------

def test_each_person_sees_only_their_own_linked_accounts(snap) -> None:
    c, fake = snap
    alice = _as(c, "Alice", admin=False, owns_link=False)
    _link(c, fake, alice)
    fake.connections_of[alice.id] = [_connection("conn-alice")]
    fake.accounts_of[alice.id] = [_account("A-1111")]
    bob = _as(c, "Bob", admin=False, owns_link=False)  # now signed in as Bob on this client
    _link(c, fake, bob)
    fake.connections_of[bob.id] = [_connection("conn-bob", name="Robinhood")]
    fake.accounts_of[bob.id] = [_account("B-2222", institution="Robinhood")]
    _reset_caches()
    body = c.get("/portfolio").text
    assert "Robinhood ••••2222" in body
    assert "••••1111" not in body and "conn-alice" not in body


def test_nobody_can_reconnect_or_unlink_someone_elses_connection(snap) -> None:
    c, fake = snap
    alice = _as(c, "Alice", admin=False, owns_link=False)
    _link(c, fake, alice)
    fake.connections_of[alice.id] = [_connection("conn-alice")]
    bob = _as(c, "Bob", admin=False, owns_link=False)
    _link(c, fake, bob)
    for route in ("/portfolio/brokerages/remove", "/portfolio/brokerages/reconnect"):
        r = c.post(route, data={"connection_id": "conn-alice"}, follow_redirects=False)
        assert r.status_code == 404, route
    assert fake.removed == [] and all(call[2] is None for call in fake.portal_calls)


# --- repairing and removing -------------------------------------------------------------------

def test_a_broken_connection_offers_a_reconnect_that_repairs_it_in_place(snap) -> None:
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    _link(c, fake, alex)
    fake.connections_of[alex.id] = [_connection("conn-a", disabled=True)]
    _reset_caches()
    body = c.get("/portfolio").text
    assert "needs reconnecting" in body and 'value="conn-a"' in body
    r = c.post("/portfolio/brokerages/reconnect", data={"connection_id": "conn-a"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PORTAL
    assert fake.portal_calls[-1][2] == "conn-a"


def test_unlinking_removes_the_connection_and_the_cached_numbers(snap) -> None:
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    _link(c, fake, alex)
    fake.connections_of[alex.id] = [_connection("conn-a")]
    fake.accounts_of[alex.id] = [_account("Z-1111")]
    _reset_caches()
    c.get("/portfolio")
    assert len(app.state.balances_cache) == 1
    r = c.post("/portfolio/brokerages/remove", data={"connection_id": "conn-a"},
               follow_redirects=False)
    assert r.status_code == 303 and fake.removed == [(alex.id, "conn-a")]
    assert len(app.state.balances_cache) == 0


def test_an_unlink_question_cannot_be_turned_into_script(snap) -> None:
    """A brokerage's name reaches a confirm() prompt; it must never be able to close the string."""
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    _link(c, fake, alex)
    fake.connections_of[alex.id] = [_connection("conn-a", name="Evil');alert(1);('")]
    _reset_caches()
    body = c.get("/portfolio").text
    assert "confirm(this.dataset.question)" in body
    assert "Evil');alert(1)" not in body, "the name must never appear unescaped"
    assert 'data-question="Unlink Evil&#39;);alert(1);(&#39;?' in body


# --- when it is not available ---------------------------------------------------------------

def test_without_snaptrade_keys_there_is_no_link_button_and_no_route(snap) -> None:
    c, _ = snap
    app.state.snaptrade = app.state.secretbox = None
    _as(c, "Alex", admin=False, owns_link=False)
    body = c.get("/portfolio").text
    assert "Link a brokerage" not in body and "No brokerage account is linked" in body
    assert c.post("/portfolio/brokerages/link", follow_redirects=False).status_code == 404


def test_a_secret_sealed_under_another_key_reads_as_not_linked(snap) -> None:
    """A rotated key, or a backup from elsewhere: offer the link again rather than crash."""
    c, fake = snap
    alex = _as(c, "Alex", admin=False, owns_link=False)
    other = SecretBox(Fernet.generate_key().decode())
    app.state.users.set_snaptrade_secret(alex.id, other.seal("from-elsewhere"))
    body = c.get("/portfolio").text
    assert c.get("/portfolio").status_code == 200 and "Link a brokerage" in body


# --- several sources at once --------------------------------------------------------------------

class _Source:
    def __init__(self, broker, accounts=(), error=None):
        self.broker, self._accounts, self._error = broker, list(accounts), error

    def accounts(self):
        if self._error:
            raise self._error
        return self._accounts


def test_one_broken_source_does_not_hide_the_others() -> None:
    both = AllAccounts([_Source("schwab", error=AuthExpiredError("expired")),
                        _Source("snaptrade", accounts=["fidelity-account"])])
    assert both.accounts() == ["fidelity-account"]


def test_every_source_failing_is_an_error() -> None:
    with pytest.raises(AuthExpiredError):
        AllAccounts([_Source("schwab", error=AuthExpiredError("expired")),
                     _Source("snaptrade", error=AuthExpiredError("gone"))]).accounts()
