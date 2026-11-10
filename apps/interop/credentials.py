"""Partner credential formats: random secrets shown once, SHA-256 digests stored.

Client secrets and access tokens carry 256 bits of fresh entropy, so a plain
SHA-256 digest is a sufficient verifier; the database never stores either value.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

CLIENT_SECRET_PREFIX: Final = "cs_"  # noqa: S105 - a public prefix, not a value
ACCESS_TOKEN_PREFIX: Final = "at_"  # noqa: S105 - a public prefix, not a value
TOKEN_LIFETIME_SECONDS: Final = 900
_CLIENT_SECRET: Final = re.compile(r"cs_[0-9a-f]{64}")
_ACCESS_TOKEN: Final = re.compile(r"at_[0-9a-f]{64}")


def new_client_secret() -> str:
    """Return one fresh client secret for a single display."""
    return CLIENT_SECRET_PREFIX + secrets.token_hex(32)


def new_access_token() -> str:
    """Return one fresh opaque bearer token."""
    return ACCESS_TOKEN_PREFIX + secrets.token_hex(32)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def client_secret_digest(value: object) -> str | None:
    """Digest a well-formed client secret; anything else has no digest."""
    if isinstance(value, str) and _CLIENT_SECRET.fullmatch(value):
        return _digest(value)
    return None


def access_token_digest(value: object) -> str | None:
    """Digest a well-formed bearer token; anything else has no digest."""
    if isinstance(value, str) and _ACCESS_TOKEN.fullmatch(value):
        return _digest(value)
    return None
