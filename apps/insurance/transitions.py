"""Atomic transitions, immutable replay receipts and registered audit appends."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.insurance.access import audit, fingerprint, replay
from apps.insurance.models import Appeal, Batch, CommandReceipt, Guide
from apps.insurance.states import require_transition

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.models import Clinic


def transition(
    *,
    clinic: Clinic,
    row: Guide | Batch | Appeal,
    target: str,
    command_id: UUID,
) -> bool:
    """Call inside clinic_write; stale or changed replay writes no state or audit."""
    kind = (
        "guide"
        if isinstance(row, Guide)
        else "batch"
        if isinstance(row, Batch)
        else "appeal"
    )
    digest = fingerprint([kind, str(row.pk), target])
    existing = replay(
        model=CommandReceipt,
        clinic_id=clinic.pk,
        command_id=command_id,
        digest=digest,
    )
    if existing is not None:
        return False
    require_transition(kind=kind, current=row.state, target=target)
    row.state = target
    row.save(update_fields=("state",))
    CommandReceipt.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic.pk,
        command_id=command_id,
        fingerprint=digest,
        subject_id=row.pk,
        kind=kind,
        next_state=target,
    )
    audit(clinic_id=clinic.pk, row=row, event=f"insurance.{kind}.{target}")
    return True
