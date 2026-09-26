"""A software passkey, for tests: a real P-256 key producing byte-exact WebAuthn responses.

It exists so the server's verification runs for real — attestation parsed, signatures checked,
challenges and origins compared — rather than being mocked out. A mocked verifier would pass a test
for code that accepts anything, which for sign-in is the one outcome that matters.

Attestation is "none", which is what a synced passkey (iCloud Keychain, Google Password Manager)
sends and what the server asks for. The knobs (``origin``, ``uv``, ``key``) exist to build the
responses a real authenticator never would, so the tests can prove the server refuses them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

_UP, _UV, _AT = 0x01, 0x04, 0x40  # authenticator data flags: user present, user verified, attested


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class SoftKey:
    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.user_handle: str | None = None  # learned at registration, as a real one would

    def _cose_public_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({
            1: 2,  # kty: EC2
            3: -7,  # alg: ES256
            -1: 1,  # crv: P-256
            -2: nums.x.to_bytes(32, "big"),
            -3: nums.y.to_bytes(32, "big"),
        })

    def create(self, options: dict, *, origin: str | None = None, uv: bool = True) -> dict:
        """Answer ``navigator.credentials.create`` for these (JSON-decoded) options."""
        self.user_handle = options["user"]["id"]
        rp_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        flags = _UP | (_UV if uv else 0) | _AT
        attested = (
            bytes(16)  # AAGUID: zero, as synced passkeys report
            + len(self.credential_id).to_bytes(2, "big") + self.credential_id
            + self._cose_public_key()
        )
        auth_data = rp_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big") + attested
        client_data = json.dumps({
            "type": "webauthn.create", "challenge": options["challenge"],
            "origin": origin or self.origin, "crossOrigin": False,
        }).encode()
        return {
            "id": b64(self.credential_id), "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(cbor2.dumps(
                    {"fmt": "none", "attStmt": {}, "authData": auth_data}
                )),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def get(self, options: dict, *, origin: str | None = None, uv: bool = True,
            signer: ec.EllipticCurvePrivateKey | None = None,
            user_handle: str | None = None) -> dict:
        """Answer ``navigator.credentials.get``. ``signer`` signs with a different private key —
        what an attacker holding only the credential id would have to do."""
        rp_hash = hashlib.sha256(options["rpId"].encode()).digest()
        auth_data = rp_hash + bytes([_UP | (_UV if uv else 0)]) + self.sign_count.to_bytes(4, "big")
        client_data = json.dumps({
            "type": "webauthn.get", "challenge": options["challenge"],
            "origin": origin or self.origin, "crossOrigin": False,
        }).encode()
        signature = (signer or self.key).sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64(self.credential_id), "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": user_handle if user_handle is not None else self.user_handle,
            },
            "clientExtensionResults": {},
        }
