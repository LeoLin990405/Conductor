"""
Shared encryption key helper for pgcrypto-based encryption at rest.

Used by api_keys.py and oauth_tokens.py for pgp_sym_encrypt/decrypt.
"""

import os

#: The setting that holds the at-rest encryption key.
ENCRYPTION_KEY_ENV = "BYOK_ENCRYPTION_KEY"

#: The refusal code a route answers with when the key is missing.
ENCRYPTION_KEY_MISSING_CODE = "byok_key_missing"

#: A fixed sentence naming the missing setting; it never holds a value.
ENCRYPTION_KEY_MISSING_MESSAGE = (
    f"{ENCRYPTION_KEY_ENV} is not set on the server, so encrypted stores "
    "(vault secrets, BYOK keys, OAuth tokens) cannot hold data"
)


class EncryptionKeyMissingError(RuntimeError):
    """``BYOK_ENCRYPTION_KEY`` is unset. A ``RuntimeError`` for old callers;
    routes that can name the problem check for it (or call
    :func:`encryption_configured` first) and answer 503 instead of 500."""

    setting = ENCRYPTION_KEY_ENV
    code = ENCRYPTION_KEY_MISSING_CODE

    def __init__(self, message: str = "") -> None:
        super().__init__(
            message
            or (
                f"{ENCRYPTION_KEY_ENV} environment variable is not set. "
                "Required for encrypting sensitive data at rest."
            )
        )


def get_encryption_key() -> str:
    """Return the symmetric encryption key for data stored at rest."""
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        raise EncryptionKeyMissingError()
    return key


def encryption_configured() -> bool:
    """True when the at-rest encryption key is set — i.e. encrypted stores
    (vault secrets, BYOK keys, OAuth tokens) can hold data at all."""
    return bool(os.getenv(ENCRYPTION_KEY_ENV))
