"""Opaque, organization-bound external identifiers for partner-visible records.

An external id is AES-SIV (deterministic authenticated encryption) of the
internal UUID under a per-organization key derived from the platform secret
``interop-external-ids``, with the record kind as associated data. The same
record always maps to the same id inside one organization; an id minted for
another organization or kind, a forged id and an internal UUID all fail
authentication before any database lookup, so every miss is the same 404.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

from apps.core.secrets import secret_store

if TYPE_CHECKING:
    from collections.abc import Callable

type Kind = Literal["appointment", "patient", "invoice", "practitioner", "service"]

SECRET_NAME: Final = "interop-external-ids"  # noqa: S105 - a secret name, not a value
PREFIXES: Final[dict[Kind, str]] = {
    "appointment": "apt",
    "patient": "pat",
    "invoice": "inv",
    "practitioner": "prc",
    "service": "svc",
}
_DOMAIN: Final = b"clinic-external-id-v1\0"
_BODY: Final = re.compile(r"[a-z2-7]{52}")


def _cipher(organization_id: UUID) -> AESSIV:
    master = bytes.fromhex(secret_store().get_secret(SECRET_NAME))
    key = hmac.digest(master, _DOMAIN + organization_id.bytes, hashlib.sha256)
    return AESSIV(key)


def encoder(organization_id: UUID) -> Callable[[Kind, UUID | None], str | None]:
    """Bind one organization's key once and encode many identifiers with it."""
    cipher = _cipher(organization_id)

    def encode(kind: Kind, internal: UUID | None) -> str | None:
        if internal is None:
            return None
        sealed = cipher.encrypt(internal.bytes, [kind.encode("ascii")])
        body = base64.b32encode(sealed).decode("ascii").rstrip("=").lower()
        return f"{PREFIXES[kind]}_{body}"

    return encode


def external_id(kind: Kind, organization_id: UUID, internal: UUID) -> str:
    """Return the stable external id of one internal record."""
    value = encoder(organization_id)(kind, internal)
    if value is None:  # pragma: no cover - a UUID always encodes
        raise ValueError
    return value


def internal_id(kind: Kind, organization_id: UUID, value: object) -> UUID | None:
    """Recover the internal UUID, or ``None`` for any foreign or malformed id."""
    prefix = f"{PREFIXES[kind]}_"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    body = value.removeprefix(prefix)
    if _BODY.fullmatch(body) is None:
        return None
    try:
        sealed = base64.b32decode(body.upper() + "====")
        plain = _cipher(organization_id).decrypt(sealed, [kind.encode("ascii")])
    except (InvalidTag, binascii.Error, ValueError):
        return None
    return UUID(bytes=plain)
