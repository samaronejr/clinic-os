"""Recorded sandbox eligibility; this lane cannot authorize any real PSP leg."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from config.settings.contracts import resolved_data_mode
from django.conf import settings

from apps.billing.psp_contracts import FIXTURE_PROVIDER, PaymentUnavailableError
from apps.providers.models import CapabilityVersion
from apps.providers.services import current_version

if TYPE_CHECKING:
    from uuid import UUID

CAPABILITY_KEYS = {"pix": "psp_pix", "card": "psp_card"}
REGISTRY_KEYS = {"psp_pix": "pix", "psp_card": "psp_card"}


@dataclass(frozen=True, slots=True)
class PspCapability:
    """Distinguish local fixture eligibility from blocked external activation."""

    capability_key: str
    sandbox_enabled: bool
    real_enabled: bool = False
    reason: str = "BLOCKED-ON-EG-1/EG-5"


def require_psp_rehearsal() -> None:
    """Reject live, invalid or disabled runtimes before any fixture scope is read."""
    if (
        resolved_data_mode() != "synthetic"
        or getattr(settings, "BILLING_SYNTHETIC_PSP", False) is not True
    ):
        raise PaymentUnavailableError


def require_psp_sandbox(
    *,
    clinic_id: UUID,
    method: str,
    version_id: UUID | None = None,
    lookup_only: bool = False,
) -> CapabilityVersion:
    """Require the current version for effects and the approved binding for lookups."""
    require_psp_rehearsal()
    if method not in CAPABILITY_KEYS or (lookup_only and version_id is None):
        raise PaymentUnavailableError
    key = REGISTRY_KEYS[CAPABILITY_KEYS[method]]
    version = (
        CapabilityVersion.objects.select_related("capability")
        .filter(pk=version_id)
        .first()
        if lookup_only and version_id is not None
        else current_version(key, clinic_id=clinic_id)
    )
    if (
        version is None
        or version.state != "sandbox"
        or version.approval_id is None
        or version.provider != FIXTURE_PROVIDER
        or version.environment != "sandbox"
        or version.account != "fixture"
        or version.capability.key != key
        or version.capability.clinic_id not in {None, clinic_id}
        or (version_id is not None and version.pk != version_id)
    ):
        raise PaymentUnavailableError
    return version


def psp_capability(*, clinic_id: UUID, method: str) -> PspCapability:
    """Never imply that sandbox state satisfies EG-1 or EG-5."""
    try:
        require_psp_sandbox(clinic_id=clinic_id, method=method)
    except PaymentUnavailableError:
        return PspCapability(
            capability_key=CAPABILITY_KEYS.get(method, ""), sandbox_enabled=False
        )
    return PspCapability(capability_key=CAPABILITY_KEYS[method], sandbox_enabled=True)
