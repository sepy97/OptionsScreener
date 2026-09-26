"""Encrypting the per-person secrets this app has to store (the SnapTrade user secret).

Fernet — AES in CBC mode with an HMAC, from the ``cryptography`` package — so a stored value is
both unreadable and tamper-evident. The key lives in the environment (``SNAPTRADE__SECRET_KEY``),
never in the database: a copy of the database, or a backup of it, is then useless on its own.

Generating a key, once, into the droplet's .env:

    docker compose exec -T app python -c \\
      "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Keep a copy in a password manager. Losing it does not lose anyone's account — only their broker
links, which they would re-link in one click — but a backup restored without it cannot use the
links it carries.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxError(Exception):
    """A stored secret that the current key cannot open — tampered with, or sealed with another
    key. The person's link has to be made again; nothing else is affected."""


class SecretBox:
    def __init__(self, key: str) -> None:
        # Fernet rejects a malformed key immediately, so a bad setting fails at startup rather
        # than at the first person's click.
        self._fernet = Fernet(key.encode())

    def seal(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def open(self, sealed: bytes) -> str:
        try:
            return self._fernet.decrypt(sealed).decode()
        except InvalidToken:
            raise SecretBoxError("a stored secret could not be decrypted with the current key") \
                from None
