"""The passkey ceremonies, verified with real signatures.

Every refusal test builds a response a genuine authenticator would not — the wrong site, a replay,
a stolen credential id signed with the wrong key — and asserts the server says no. That is the half
of sign-in worth testing: code that accepts every response passes every test that only checks
acceptance.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from _softkey import SoftKey, b64
from cryptography.hazmat.primitives.asymmetric import ec

from wheel_screener.api.passkeys import PasskeyError, Passkeys
from wheel_screener.api.users import UserStore

ORIGIN = "https://steadybull.example"
RP_ID = "steadybull.example"


@pytest.fixture
def passkeys(tmp_path) -> Passkeys:
    return Passkeys(UserStore(str(tmp_path / "users.sqlite")), RP_ID, "Steady Bull", ORIGIN)


def _register(pk: Passkeys, token: str, key: SoftKey | None = None, **create_kw):
    key = key or SoftKey(ORIGIN)
    options = json.loads(pk.registration_options(token))
    return pk.register(key.create(options, **create_kw)), key


def _sign_in(pk: Passkeys, key: SoftKey, **get_kw):
    return pk.login(key.get(json.loads(pk.login_options()), **get_kw))


# --- registering ----------------------------------------------------------------------------

def test_an_invite_creates_the_account_its_passkey_opens(passkeys) -> None:
    token = passkeys.store.create_invite("Sam", is_admin=True)
    user, key = _register(passkeys, token)
    assert user.name == "Sam" and user.is_admin
    assert [c.id for c in passkeys.store.credentials_for(user.id)] == [key.credential_id]
    assert _sign_in(passkeys, key).id == user.id


def test_the_passkey_is_registered_against_a_random_handle_not_the_account_id(passkeys) -> None:
    user, key = _register(passkeys, passkeys.store.create_invite("Sam"))
    assert key.user_handle != user.id and len(user.handle) == 32


def test_an_invite_works_once(passkeys) -> None:
    token = passkeys.store.create_invite("Sam")
    _register(passkeys, token)
    with pytest.raises(PasskeyError, match="expired or was already used"):
        _register(passkeys, token)
    assert len(passkeys.store.users()) == 1


def test_two_tabs_finishing_the_same_invite_make_one_account(passkeys) -> None:
    """Both got options while the invite was fresh; only the first to finish may use it."""
    token = passkeys.store.create_invite("Sam")
    first = json.loads(passkeys.registration_options(token))
    second = json.loads(passkeys.registration_options(token))
    passkeys.register(SoftKey(ORIGIN).create(first))
    with pytest.raises(PasskeyError, match="already used"):
        passkeys.register(SoftKey(ORIGIN).create(second))
    assert len(passkeys.store.users()) == 1


def test_an_expired_invite_is_refused(passkeys) -> None:
    token = passkeys.store.create_invite("Sam", ttl=timedelta(seconds=-1))
    with pytest.raises(PasskeyError, match="expired"):
        passkeys.registration_options(token)


def test_a_passkey_made_for_another_site_is_refused(passkeys) -> None:
    """A phishing page relaying our options gets a response bound to ITS origin."""
    token = passkeys.store.create_invite("Sam")
    with pytest.raises(PasskeyError, match="could not be verified"):
        _register(passkeys, token, origin="https://steadybu11.example")
    assert passkeys.store.users() == [] and passkeys.store.invite(token) is not None


def test_a_passkey_that_was_not_unlocked_is_refused(passkeys) -> None:
    """User verification is required: presence alone — a tap — is not enough."""
    with pytest.raises(PasskeyError, match="could not be verified"):
        _register(passkeys, passkeys.store.create_invite("Sam"), uv=False)


def test_a_recovery_invite_adds_a_passkey_to_the_same_account(passkeys) -> None:
    """The decided answer to a lost phone: a new invite, for the existing account."""
    user, phone = _register(passkeys, passkeys.store.create_invite("Sam"))
    recovery = passkeys.store.create_invite("Sam", for_user=user.id)
    options = json.loads(passkeys.registration_options(recovery))
    assert options["user"]["id"] == phone.user_handle  # the same account, as far as a passkey knows
    # the browser won't re-register a passkey it already holds for this account
    assert [c["id"] for c in options["excludeCredentials"]] == [b64(phone.credential_id)]
    laptop = SoftKey(ORIGIN)
    again = passkeys.register(laptop.create(options))
    assert again.id == user.id and len(passkeys.store.users()) == 1
    assert _sign_in(passkeys, phone).id == user.id
    assert _sign_in(passkeys, laptop).id == user.id


def test_a_login_challenge_cannot_be_spent_on_a_registration(passkeys) -> None:
    token = passkeys.store.create_invite("Sam")
    options = json.loads(passkeys.registration_options(token))
    login_challenge = json.loads(passkeys.login_options())["challenge"]
    options["challenge"] = login_challenge
    with pytest.raises(PasskeyError, match="expired"):
        passkeys.register(SoftKey(ORIGIN).create(options))


# --- signing in -----------------------------------------------------------------------------

def test_a_captured_sign_in_cannot_be_replayed(passkeys) -> None:
    _, key = _register(passkeys, passkeys.store.create_invite("Sam"))
    response = key.get(json.loads(passkeys.login_options()))
    passkeys.login(response)
    with pytest.raises(PasskeyError, match="expired"):
        passkeys.login(response)


def test_a_sign_in_for_another_site_is_refused(passkeys) -> None:
    _, key = _register(passkeys, passkeys.store.create_invite("Sam"))
    with pytest.raises(PasskeyError, match="not recognised"):
        _sign_in(passkeys, key, origin="https://steadybu11.example")


def test_knowing_a_credential_id_is_not_enough(passkeys) -> None:
    """Credential ids are not secret. Signing with anything but the registered key must fail."""
    _, key = _register(passkeys, passkeys.store.create_invite("Sam"))
    with pytest.raises(PasskeyError, match="not recognised"):
        _sign_in(passkeys, key, signer=ec.generate_private_key(ec.SECP256R1()))


def test_a_sign_in_that_was_not_unlocked_is_refused(passkeys) -> None:
    _, key = _register(passkeys, passkeys.store.create_invite("Sam"))
    with pytest.raises(PasskeyError, match="not recognised"):
        _sign_in(passkeys, key, uv=False)


def test_a_response_naming_another_account_than_its_passkeys_is_refused(passkeys) -> None:
    """A discoverable passkey names its account, and the spec requires checking that name.

    Not what stops Alice becoming Bob — the account is always taken from the passkey's own record,
    so with this check removed the response signs in as Alice (checked by mutation). It refuses a
    response that contradicts itself, rather than quietly trusting half of it.
    """
    _, alice = _register(passkeys, passkeys.store.create_invite("Alice"))
    _, bob = _register(passkeys, passkeys.store.create_invite("Bob"))
    with pytest.raises(PasskeyError, match="not recognised"):
        _sign_in(passkeys, alice, user_handle=bob.user_handle)


def test_an_unknown_passkey_is_refused(passkeys) -> None:
    stranger = SoftKey(ORIGIN)
    stranger.user_handle = "AAAA"
    with pytest.raises(PasskeyError, match="not recognised"):
        _sign_in(passkeys, stranger)


def test_a_garbled_response_is_refused_not_crashed_on(passkeys) -> None:
    with pytest.raises(PasskeyError):
        passkeys.login({"rawId": "!!", "response": {"clientDataJSON": "not base64 json"}})
    with pytest.raises(PasskeyError):
        passkeys.login({})
    with pytest.raises(PasskeyError):
        passkeys.register({"response": None})


def test_a_sign_in_records_its_use(passkeys) -> None:
    user, key = _register(passkeys, passkeys.store.create_invite("Sam"))
    key.sign_count = 5
    _sign_in(passkeys, key)
    assert passkeys.store.credentials_for(user.id)[0].sign_count == 5
