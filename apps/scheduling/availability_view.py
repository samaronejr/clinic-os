"""Transaction-bound clinic availability read service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.scheduling.access import authorized_view_scope
from apps.scheduling.models import AvailabilityBlock

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class AvailabilityViewItem:
    """Expose stable identifiers and UTC bounds without joining identity rows."""

    availability_id: UUID
    practitioner_id: UUID
    start_at: datetime
    end_at: datetime


def view_availability(*, clinic_id: UUID) -> tuple[AvailabilityViewItem, ...]:
    """Return active manager-visible clinic availability in stable order."""
    with transaction.atomic():
        scope = authorized_view_scope(clinic_id)
        rows = AvailabilityBlock.objects.filter(
            organization_id=scope.clinic.organization_id,
            clinic_id=clinic_id,
            retired_at__isnull=True,
            practitioner__isnull=False,
        )
        if scope.practitioner_id is not None:
            rows = rows.filter(practitioner_id=scope.practitioner_id)
        ordered = rows.order_by("start_at", "practitioner_id", "pk")
        items = tuple(
            AvailabilityViewItem(
                availability_id=row.pk,
                practitioner_id=row.practitioner_id,
                start_at=row.start_at,
                end_at=row.end_at,
            )
            for row in ordered
            if row.practitioner_id is not None
        )
        record_phase1_event(
            "scheduling.availability.viewed",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )
        return items
