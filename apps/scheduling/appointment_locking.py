"""Global advisory and UUID-row lock order for every appointment write."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Q

from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    patient_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import Appointment, AvailabilityBlock

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class AppointmentWriteRows:
    """UUID-ordered rows held until the appointment transaction ends."""

    availability: AvailabilityBlock | None
    conflicting_appointment_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class AppointmentWriteTarget:
    """Immutable lock identity shared by create and later transitions."""

    organization_id: UUID
    clinic_id: UUID
    patient_id: UUID
    practitioner_ids: tuple[UUID, ...]


def acquire_appointment_write_gates(
    *,
    target: AppointmentWriteTarget,
) -> None:
    """Take clinic, organization-patient, then sorted practitioner gates."""
    acquire_advisory_locks((clinic_lock_key(target.clinic_id),))
    acquire_advisory_locks(
        (patient_lock_key(target.organization_id, target.patient_id),)
    )
    acquire_advisory_locks(user_lock_keys(target.practitioner_ids))


def lock_appointment_write_rows(
    *,
    target: AppointmentWriteTarget,
    start_at: datetime,
    end_at: datetime,
) -> AppointmentWriteRows:
    """Lock containing availability, then overlapping appointments by UUID."""
    availability_rows = tuple(
        AvailabilityBlock.objects.select_for_update()
        .filter(
            organization_id=target.organization_id,
            clinic_id=target.clinic_id,
            practitioner_id__in=target.practitioner_ids,
            retired_at__isnull=True,
            start_at__lte=start_at,
            end_at__gte=end_at,
        )
        .order_by("pk")
    )
    availability = availability_rows[0] if len(availability_rows) == 1 else None
    conflict_ids = tuple(
        Appointment.objects.select_for_update()
        .filter(
            organization_id=target.organization_id,
            status=Appointment.Status.SCHEDULED,
            start_at__lt=end_at,
            end_at__gt=start_at,
        )
        .filter(
            Q(patient_id=target.patient_id)
            | Q(practitioner_id__in=target.practitioner_ids)
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return AppointmentWriteRows(
        availability=availability,
        conflicting_appointment_ids=conflict_ids,
    )
