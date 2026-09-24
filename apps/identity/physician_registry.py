"""Professional registry boundary; no real provider or credentials are selected.

The task-6 physician_registration capability is unavailable (2026-09-24-v2).
A synthetic response is never an authorized sandbox response or live approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from django.conf import settings
from django.utils.timezone import now as utc_now

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class RegistrationIdentity:
    """Owner-provisioned exact professional identity, not a name lookup."""

    jurisdiction: str
    registration_number: str
    signing_subject: str


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    """Verified signing-adapter output, never fields copied from an HTTP form.

    Real signing adapters must authenticate this binding and validate certificate
    trust/revocation themselves. No such adapter is approved or implemented yet.
    """

    issuer_id: UUID
    subject: str
    synthetic: bool


@dataclass(frozen=True, slots=True)
class RegistryResponse:
    """Minimal normalized timestamped evidence; no raw credential payload."""

    identity: RegistrationIdentity
    status: str
    checked_at: datetime
    expires_at: datetime | None
    recheck_at: datetime
    reference: str
    synthetic: bool


class RegistryUnavailableError(Exception):
    """A registry timeout/outage; an old success must never replace it."""


class PhysicianRegistry(Protocol):
    """Future approved capability's exact-identity lookup contract."""

    provider: str

    def lookup(self, identity: RegistrationIdentity) -> RegistryResponse:
        """Return fresh evidence or raise RegistryUnavailableError."""
        ...


@dataclass(frozen=True, slots=True)
class RegistryCapability:
    """Explicit synthetic opt-in with a permanently closed current live gate."""

    synthetic_enabled: bool
    real_enabled: bool = False
    reason: str = "missing_registry_provider_and_owner_approval"


def registry_capability() -> RegistryCapability:
    """Reflect the unavailable task-6 record, not caller claims of approval."""
    return RegistryCapability(
        synthetic_enabled=getattr(settings, "PHYSICIAN_SYNTHETIC_REGISTRY", False)
        is True
    )


class SyntheticPhysicianRegistry:
    """Visibly synthetic regular fixture; never calls a network registry."""

    provider = "synthetic-physician-registry-v1"

    def lookup(self, identity: RegistrationIdentity) -> RegistryResponse:
        """Verify only a visibly synthetic identity under a test-only policy."""
        if not (
            identity.registration_number.startswith("SYNTHETIC-")
            and identity.signing_subject.startswith("synthetic:")
        ):
            raise RegistryUnavailableError
        now = utc_now()
        return RegistryResponse(
            identity=identity,
            status="regular",
            checked_at=now,
            expires_at=None,
            recheck_at=now + timedelta(minutes=5),
            reference="SYNTHETIC-NOT-A-REGISTRY-RECEIPT",
            synthetic=True,
        )


def get_physician_registry() -> PhysicianRegistry:
    """Resolve only the explicitly enabled synthetic adapter."""
    if not registry_capability().synthetic_enabled:
        raise RegistryUnavailableError
    return SyntheticPhysicianRegistry()
