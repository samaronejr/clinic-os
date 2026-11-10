"""An offline collection fixture; real collection remains BLOCKED-ON-EG (EG-18)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.db import transaction

from apps.identity.management.base import assert_owner_database_role
from apps.providers.models import CapabilityVersion, ProviderCapability
from apps.providers.services import current_version

if TYPE_CHECKING:
    from uuid import UUID

REAL_ENABLED: Final = False
GATE: Final = "BLOCKED-ON-EG"
ACTIONS: Final = frozenset({"paid", "payment_failed", "advance", "offboard"})


class CollectionUnavailableError(Exception):
    """Refuse without reflecting provider material or configuration."""


@dataclass(frozen=True, slots=True)
class SyntheticReceipt:
    """Only opaque fixture identity and a closed action can leave the adapter."""

    subscription_id: UUID
    event_id: UUID
    action: str
    synthetic: bool = True
    real_enabled: bool = False
    gate: str = GATE


@transaction.atomic
def bootstrap_synthetic_collection() -> None:
    """Explicit owner setup for local rehearsal, never a capability approval."""
    assert_owner_database_role()
    if resolved_data_mode() != SYNTHETIC_DATA_MODE:
        raise CollectionUnavailableError
    capability, _created = ProviderCapability.objects.get_or_create(
        key="saas_collection",
        clinic_id=None,
        defaults={
            "record_ref": "EG-18",
            "description": "Offline SaaS collection fixture; BLOCKED-ON-EG.",
        },
    )
    if capability.current_version_id is not None:
        return
    version = CapabilityVersion.objects.create(
        capability=capability,
        provider="synthetic",
        environment="synthetic",
        api_version="subscription-fixture-v1",
        state="researched",
        region="br-synthetic",
        retention_terms="Opaque fixture receipts only.",
    )
    capability.current_version = version
    capability.save(update_fields=("current_version", "updated_at"))


def simulate_event(
    *, clinic_id: UUID, subscription_id: UUID, event_id: UUID, action: str
) -> SyntheticReceipt:
    """Deterministically simulate a receipt without a network client or charge."""
    version = current_version("saas_collection", clinic_id=clinic_id)
    if (
        resolved_data_mode() != SYNTHETIC_DATA_MODE
        or action not in ACTIONS
        or version is None
        or version.provider != "synthetic"
        or version.environment != "synthetic"
        or version.api_version != "subscription-fixture-v1"
        or version.state in {"revoked", "degraded"}
    ):
        raise CollectionUnavailableError
    return SyntheticReceipt(subscription_id, event_id, action)
