"""Fixed advisory-lock domains shared by scheduling and identity services."""

from collections.abc import Iterable
from uuid import UUID

from django.db import connection

from apps.identity.identifiers import canonicalize_email, canonicalize_username


class AdvisoryLockKeyError(ValueError):
    """Reject an identity gate without both canonical global strings."""


class _InvalidLockDomainError(AdvisoryLockKeyError):
    def __init__(self) -> None:
        super().__init__("advisory lock key has an invalid domain")


class _BlankIdentityLockError(AdvisoryLockKeyError):
    def __init__(self) -> None:
        super().__init__("identity lock values must be nonblank")


class _LockOrderError(AdvisoryLockKeyError):
    def __init__(self) -> None:
        super().__init__("advisory lock keys are not globally ordered")


def _lock_order(key: str) -> tuple[int, bytes]:
    domains = (
        "clinic-lock-v1:identity-",
        "clinic-lock-v1:clinic:",
        "clinic-lock-v1:user:",
        "clinic-lock-v1:resource:",
        "clinic-lock-v1:patient:",
    )
    for index, prefix in enumerate(domains):
        if key.startswith(prefix) and len(key) > len(prefix):
            return index, key.encode()
    raise _InvalidLockDomainError


def identity_lock_keys(username: str, email: str) -> tuple[str, ...]:
    """Return canonical identity gates in UTF-8 byte order."""
    canonical_username = canonicalize_username(username)
    canonical_email = canonicalize_email(email)
    if not canonical_username or not canonical_email:
        raise _BlankIdentityLockError
    keys = (
        f"clinic-lock-v1:identity-email:{canonical_email}",
        f"clinic-lock-v1:identity-username:{canonical_username}",
    )
    return tuple(sorted(keys, key=str.encode))


def clinic_lock_key(clinic_id: UUID) -> str:
    """Return the gate for one clinic UUID."""
    return f"clinic-lock-v1:clinic:{clinic_id}"


def patient_lock_key(organization_id: UUID, patient_id: UUID) -> str:
    """Return the organization-patient gate used across clinics."""
    return f"clinic-lock-v1:patient:{organization_id}:{patient_id}"


def user_lock_keys(user_ids: Iterable[UUID]) -> tuple[str, ...]:
    """Return unique target/practitioner gates in UUID order."""
    return tuple(f"clinic-lock-v1:user:{user_id}" for user_id in sorted(set(user_ids)))


def resource_lock_keys(resource_ids: Iterable[UUID]) -> tuple[str, ...]:
    """Return unique resource gates in UUID order, before the patient gate."""
    return tuple(f"clinic-lock-v1:resource:{pk}" for pk in sorted(set(resource_ids)))


def acquire_advisory_locks(keys: Iterable[str]) -> None:
    """Acquire unique transaction locks only in the fixed global domain order."""
    ordered = tuple(keys)
    if ordered != tuple(sorted(set(ordered), key=_lock_order)):
        raise _LockOrderError
    with connection.cursor() as cursor:
        for key in ordered:
            cursor.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(%s, 0))",
                [key],
            )
