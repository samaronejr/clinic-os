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
    additional_availability: tuple[AvailabilityBlock | None, ...]
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
    additional_ranges: tuple[tuple[datetime, datetime], ...] = (),
    appointment_ids: tuple[UUID, ...] = (),
) -> AppointmentWriteRows:
    """Lock containing availability, then overlapping appointments by UUID."""
    ranges = ((start_at, end_at), *additional_ranges)
    availability_filter = Q()
    appointment_overlap = Q()
    for range_start, range_end in ranges:
        availability_filter |= Q(start_at__lte=range_start, end_at__gte=range_end)
        appointment_overlap |= Q(start_at__lt=range_end, end_at__gt=range_start)
    availability_rows = tuple(
        AvailabilityBlock.objects.select_for_update()
        .filter(
            organization_id=target.organization_id,
            clinic_id=target.clinic_id,
            practitioner_id__in=target.practitioner_ids,
            retired_at__isnull=True,
        )
        .filter(availability_filter)
        .order_by("pk")
    )
    availability_matches = tuple(
        _covering_availability(availability_rows, range_start, range_end)
        for range_start, range_end in ranges
    )
    conflict_ids = tuple(
        Appointment.objects.select_for_update()
        .filter(
            organization_id=target.organization_id,
        )
        .filter(
            Q(pk__in=appointment_ids)
            | (
                Q(status=Appointment.Status.SCHEDULED)
                & appointment_overlap
                & (
                    Q(patient_id=target.patient_id)
                    | Q(practitioner_id__in=target.practitioner_ids)
                )
            )
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return AppointmentWriteRows(
        availability=availability_matches[0],
        additional_availability=availability_matches[1:],
        conflicting_appointment_ids=conflict_ids,
    )


def _covering_availability(
    rows: tuple[AvailabilityBlock, ...],
    start_at: datetime,
    end_at: datetime,
) -> AvailabilityBlock | None:
    matches = tuple(
        row for row in rows if row.start_at <= start_at and row.end_at >= end_at
    )
    return matches[0] if len(matches) == 1 else None
