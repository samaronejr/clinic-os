"""Tenant-scoped envelope encryption over the approved pgcrypto boundary.

This module is the only application surface for the task-6 data-at-rest
capability. Payloads are encrypted inside the database by
``clinic_app.tenant_encrypt``/``tenant_decrypt`` (AES-256 via pgcrypto's
reviewed OpenPGP implementation, MDC integrity, no compression); the tenant
DEK lives only wrapped in ``tenancy_tenantdatakey`` and is unwrapped there by
a KEK fetched per call from the configured ``SecretStore``.

There is no plaintext fallback and no handwritten crypto: a missing backend,
a missing key, a wrong KEK, a tampered envelope or a wrong tenant/purpose
context raises ``EnvelopeError`` and returns nothing. Callers must hold an
active tenant context (``tenant_context``) so ``app.current_tenant`` is set;
the functions re-validate it in-database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import DatabaseError, connection

from apps.core.secrets import secret_store

if TYPE_CHECKING:
    from datetime import datetime

KEK_SECRET_NAME = "tenant-kek"  # noqa: S105 - a secret name, not a value


class EnvelopeError(Exception):
    """Base failure for the envelope boundary; nothing is returned or written."""


class EnvelopeUnavailableError(EnvelopeError):
    """Key material, KEK or capability is unavailable for this tenant."""


class EnvelopeContextError(EnvelopeError):
    """Tenant context, purpose or KEK shape failed validation."""


class EnvelopeFormatError(EnvelopeError):
    """The stored envelope is malformed or bound to another context."""


@dataclass(frozen=True, slots=True)
class TenantKeyStatus:
    """One wrapped DEK version's lifecycle state for the current tenant."""

    key_version: int
    status: str
    created_at: datetime
    retired_at: datetime | None


_SQLSTATE_MAP = {
    "22023": EnvelopeContextError,
    "39000": EnvelopeUnavailableError,
    "TEN01": EnvelopeUnavailableError,
    "TEN02": EnvelopeFormatError,
    "TEN03": EnvelopeFormatError,
}


def _sqlstate(error: DatabaseError) -> str | None:
    """Extract the PostgreSQL SQLSTATE from a wrapped driver error."""
    for candidate in (error, error.__cause__):
        value = getattr(candidate, "sqlstate", None)
        if type(value) is str:
            return value
    return None


def _raise_mapped(error: DatabaseError) -> None:
    """Map database failures onto the closed envelope error vocabulary."""
    mapped = _SQLSTATE_MAP.get(_sqlstate(error) or "")
    if mapped is not None:
        raise mapped from error
    raise EnvelopeError from error


def _kek() -> str:
    """Fetch the tenant KEK from the configured secret store, fail closed."""
    return secret_store().get_secret(KEK_SECRET_NAME)


_PURPOSE_MAX_LENGTH: Final = 128
_PURPOSE_MIN_CODEPOINT: Final = 0x20
_PURPOSE_DELETE_CODEPOINT: Final = 0x7F


def _validate_purpose(purpose: str) -> None:
    """Mirror the in-database purpose contract before the call is made."""
    if (
        type(purpose) is not str
        or not 1 <= len(purpose) <= _PURPOSE_MAX_LENGTH
        or any(
            ord(character) < _PURPOSE_MIN_CODEPOINT
            or ord(character) == _PURPOSE_DELETE_CODEPOINT
            for character in purpose
        )
    ):
        raise EnvelopeContextError


def _call_scalar(sql: str, params: tuple[str | bytes, ...]) -> object:
    """Run one envelope function and return its single-column result."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
    except DatabaseError as error:
        _raise_mapped(error)
    if row is None:
        raise EnvelopeUnavailableError
    return row[0]


def issue_tenant_key() -> int:
    """Create a fresh active DEK for the current tenant; retire the old one.

    The DEK is generated inside the database, wrapped with the configured
    KEK and stored; plaintext key material never reaches this process.
    Returns the new key version.
    """
    result = _call_scalar("SELECT clinic_app.tenant_dek_issue(%s)", (_kek(),))
    if type(result) is not int:
        raise EnvelopeUnavailableError
    return result


def encrypt(*, purpose: str, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` under the current tenant's active DEK.

    ``purpose`` is authenticated context: decryption with any other purpose
    fails closed. Returns the versioned envelope bytes for storage.
    """
    _validate_purpose(purpose)
    result = _call_scalar(
        "SELECT clinic_app.tenant_encrypt(%s, %s, %s)",
        (_kek(), purpose, plaintext),
    )
    if type(result) is not bytes and not isinstance(result, memoryview):
        raise EnvelopeUnavailableError
    return bytes(result)


def decrypt(*, purpose: str, envelope: bytes) -> bytes:
    """Decrypt one envelope for the current tenant under ``purpose``.

    Wrong tenant, wrong purpose, wrong KEK, retired-but-missing versions and
    tampered bytes all raise ``EnvelopeError``; plaintext is never returned
    on any failure path.
    """
    _validate_purpose(purpose)
    result = _call_scalar(
        "SELECT clinic_app.tenant_decrypt(%s, %s, %s)",
        (_kek(), purpose, envelope),
    )
    if type(result) is not bytes and not isinstance(result, memoryview):
        raise EnvelopeUnavailableError
    return bytes(result)


def reencrypt(*, purpose: str, envelope: bytes) -> bytes:
    """Re-encrypt one envelope under the tenant's current active DEK.

    Decrypt-then-encrypt happens inside the database in one call; plaintext
    never crosses the application boundary. Used for DEK rotation.
    """
    _validate_purpose(purpose)
    result = _call_scalar(
        "SELECT clinic_app.tenant_reencrypt(%s, %s, %s)",
        (_kek(), purpose, envelope),
    )
    if type(result) is not bytes and not isinstance(result, memoryview):
        raise EnvelopeUnavailableError
    return bytes(result)


def protect(*, purpose: str, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` under the resolved context's active DEK.

    Unlike ``encrypt``, the tenant is resolved in-database from either
    ``app.current_tenant`` (staff/owner transactions) or the live
    ``app.current_patient_session`` row, so patient-session writes use the
    same envelope boundary without ever holding a staff tenant GUC.
    """
    _validate_purpose(purpose)
    result = _call_scalar(
        "SELECT clinic_app.protected_encrypt(%s, %s, %s)",
        (_kek(), purpose, plaintext),
    )
    if type(result) is not bytes and not isinstance(result, memoryview):
        raise EnvelopeUnavailableError
    return bytes(result)


def reveal(*, purpose: str, envelope: bytes) -> bytes:
    """Decrypt one envelope under the resolved context's DEK history."""
    _validate_purpose(purpose)
    result = _call_scalar(
        "SELECT clinic_app.protected_decrypt(%s, %s, %s)",
        (_kek(), purpose, envelope),
    )
    if type(result) is not bytes and not isinstance(result, memoryview):
        raise EnvelopeUnavailableError
    return bytes(result)


def rewrap_tenant_keys(*, new_kek: str) -> int:
    """Re-wrap every DEK version of the current tenant under ``new_kek``.

    The old KEK is fetched from the configured store; ``new_kek`` must be a
    64-hex value. Any unwrap failure aborts the whole rewrap atomically.
    Returns the number of rewrapped versions.
    """
    result = _call_scalar(
        "SELECT clinic_app.tenant_dek_rewrap(%s, %s)", (_kek(), new_kek)
    )
    if type(result) is not int:
        raise EnvelopeUnavailableError
    return result


def tenant_key_status() -> tuple[TenantKeyStatus, ...]:
    """List the current tenant's DEK versions and lifecycle state."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT key_version, status, created_at, retired_at "
                "FROM clinic_app.tenant_key_status()"
            )
            rows = cursor.fetchall()
    except DatabaseError as error:
        _raise_mapped(error)
    return tuple(
        TenantKeyStatus(
            key_version=row[0],
            status=row[1],
            created_at=row[2],
            retired_at=row[3],
        )
        for row in rows
    )
