"""The two passkey ceremonies: registering one from an invite, and signing in with one.

The cryptography is py_webauthn's; this module owns the rules around it — which invite a
registration belongs to, which challenge a response answers, and whose account a signature opens.

**How a response is tied to its challenge without a second cookie.** Every response carries the
challenge it signed, inside ``clientDataJSON``. That copy is only *used to find* the challenge in
the store; it is never trusted on its own. The store's copy is single-use and short-lived, and
py_webauthn then checks the signed ``clientDataJSON`` against it — so a response can only ever
answer a challenge this server issued, for this purpose, once.

**User verification is required,** not merely preferred: the passkey must be unlocked by Face ID,
Touch ID or a device PIN. A passkey is the only thing between a visitor and brokerage data, so
"someone picked up the unlocked laptop" should not be enough.

**The passkey must be discoverable** (a resident key), so signing in needs no username: the browser
offers the passkeys it holds for this site and the person picks one.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from wheel_screener.api.users import User, UserStore, new_handle

REGISTER = "register"
LOGIN = "login"


class PasskeyError(Exception):
    """A ceremony that did not succeed. The message is safe to show the person."""


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _challenge_in(credential: dict) -> str | None:
    """The challenge a response claims to answer — used only to LOOK UP the real one."""
    try:
        client_data = json.loads(_b64decode(credential["response"]["clientDataJSON"]))
        challenge = client_data["challenge"]
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    return challenge if isinstance(challenge, str) else None


@dataclass(frozen=True)
class Passkeys:
    store: UserStore
    rp_id: str  # the site's hostname — passkeys are bound to it
    rp_name: str  # what the browser's passkey prompt calls this site
    origin: str  # scheme + host (+ port) the browser reports; must match exactly

    # --- registering ----------------------------------------------------------------------

    def registration_options(self, invite_token: str) -> str:
        """Options for the browser's ``navigator.credentials.create``, as JSON.

        The account does not exist yet for a new user, so its handle is minted here and carried
        in the challenge's record; the account is created only once a passkey has been proven.
        An invite that adds a device to an existing account uses that account's handle, and
        excludes the passkeys it already has so the same one is not registered twice.
        """
        invite = self.store.invite(invite_token)
        if invite is None:
            raise PasskeyError("This invite link has expired or was already used.")
        existing = self.store.user(invite.for_user) if invite.for_user else None
        if invite.for_user and existing is None:
            raise PasskeyError("The account this invite was for no longer exists.")
        handle = existing.handle if existing else new_handle()
        name = existing.name if existing else invite.name
        exclude = [
            PublicKeyCredentialDescriptor(id=c.id)
            for c in (self.store.credentials_for(existing.id) if existing else [])
        ]
        challenge = self.store.issue_challenge(
            REGISTER, {"invite": invite.token, "handle": _b64encode(handle)}
        )
        options = generate_registration_options(
            rp_id=self.rp_id, rp_name=self.rp_name,
            user_id=handle, user_name=name, user_display_name=name,
            challenge=challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude or None,
        )
        return options_to_json(options)

    def register(self, credential: dict) -> User:
        """Verify a new passkey and attach it to its account, creating the account if the invite
        was for a new person. Uses the invite up; returns whose account it now opens."""
        found = self.store.use_challenge(_challenge_in(credential), REGISTER)
        if found is None:
            raise PasskeyError("That sign-up attempt expired. Please open the invite link again.")
        challenge, data = found
        invite = self.store.invite(data.get("invite"))
        if invite is None:
            raise PasskeyError("This invite link has expired or was already used.")
        try:
            verified = verify_registration_response(
                credential=credential, expected_challenge=challenge,
                expected_rp_id=self.rp_id, expected_origin=self.origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as e:
            raise PasskeyError("The passkey could not be verified. Please try again.") from e
        if self.store.credential(verified.credential_id) is not None:
            raise PasskeyError("That passkey is already registered — sign in with it instead.")
        # Last, and atomic: two tabs completing the same invite at once make one account, not two.
        if not self.store.use_invite(invite.token):
            raise PasskeyError("This invite link has expired or was already used.")
        if invite.for_user:
            user = self.store.user(invite.for_user)
            if user is None:
                raise PasskeyError("The account this invite was for no longer exists.")
        else:
            user = self.store.create_user(
                invite.name, is_admin=invite.is_admin, handle=_b64decode(data["handle"])
            )
        transports = (credential.get("response") or {}).get("transports") or []
        self.store.add_credential(
            user.id, verified.credential_id, verified.credential_public_key,
            verified.sign_count, [t for t in transports if isinstance(t, str)],
        )
        return user

    # --- signing in -----------------------------------------------------------------------

    def login_options(self) -> str:
        """Options for ``navigator.credentials.get``, as JSON. No list of allowed passkeys: the
        browser offers whichever ones it holds for this site."""
        options = generate_authentication_options(
            rp_id=self.rp_id, challenge=self.store.issue_challenge(LOGIN),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json(options)

    def login(self, credential: dict) -> User:
        """Verify a sign-in and return whose account it opens."""
        failed = PasskeyError("That passkey was not recognised. Please try again.")
        found = self.store.use_challenge(_challenge_in(credential), LOGIN)
        if found is None:
            raise PasskeyError("That sign-in attempt expired. Please try again.")
        challenge, _ = found
        try:
            stored = self.store.credential(_b64decode(credential["rawId"]))
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise failed from None
        if stored is None:
            raise failed
        user = self.store.user(stored.user_id)
        if user is None:
            raise failed
        # A discoverable passkey names its account; the spec requires that to be the account
        # the passkey is registered to. The account itself always comes from the credential's
        # own record above, never from this field — this refuses a self-contradicting response.
        handle = (credential.get("response") or {}).get("userHandle")
        if handle:
            try:
                if _b64decode(handle) != user.handle:
                    raise failed
            except (TypeError, ValueError, binascii.Error):
                raise failed from None
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge,
                expected_rp_id=self.rp_id, expected_origin=self.origin,
                credential_public_key=stored.public_key,
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as e:
            raise failed from e
        self.store.credential_used(stored.id, verified.new_sign_count)
        return user
