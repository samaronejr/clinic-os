"""Manager-authorized booking preparation without generated slots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    PhysicianCatalogEntry,
    UserId,
    list_active_clinic_physicians,
)
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    authorized_appointment_manager_clinic,
)
from apps.scheduling.models import AvailabilityBlock
from apps.scheduling.resource_booking import (
    authorized_service_clinic,
    booking_selection,
    service_practitioners,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.timezones import format_local_minute

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class BookingAvailabilityWindow:
    """Expose one active availability promise, never a generated slot."""

    availability_id: UUID
    start_at: datetime
    end_at: datetime
    start_local: str
    end_local: str


@dataclass(frozen=True, slots=True)
class BookingPractitioner:
    """Expose one resolver-approved physician and their active windows."""

    practitioner_id: UUID
    display_identifier: str
    windows: tuple[BookingAvailabilityWindow, ...]


@dataclass(frozen=True, slots=True)
class BookingPreparation:
    """Expose body-selected enrollment context and explicit booking windows."""

    enrollment_id: UUID
    patient_display_name: str
    practitioners: tuple[BookingPractitioner, ...]
    service_type_id: UUID | None = None
    resource_ids: tuple[UUID, ...] = ()
    duration_min: int | None = None
    buffer_before: int = 0
    buffer_after: int = 0


def prepare_booking(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    service_type_id: UUID | None = None,
    resource_ids: tuple[UUID, ...] = (),
) -> BookingPreparation:
    """Return one clinic-scoped booking DTO for the current clinic manager."""
    if type(clinic_id) is not UUID or type(enrollment_id) is not UUID:
        raise AppointmentAccessDeniedError
    with transaction.atomic():
        clinic = (
            authorized_appointment_manager_clinic(clinic_id)
            if service_type_id is None
            else authorized_service_clinic(clinic_id)
        )
        service, resources = booking_selection(
            clinic=clinic, service_type_id=service_type_id, resource_ids=resource_ids
        )
        if service is not None and (
            not service.active
            or any(not row.active for row in resources)
            or not set(service.required_resource_kinds)
            <= {row.kind for row in resources}
        ):
            msg = "resource_conflict"
            raise SchedulingRuleError(msg)
        timezone_key = clinic.timezone
        if not isinstance(timezone_key, str):
            raise AppointmentAccessDeniedError
        try:
            enrollment = PatientClinicEnrollment.objects.select_related("patient").get(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                pk=enrollment_id,
            )
            catalog = tuple(
                sorted(
                    list_active_clinic_physicians(clinic_id)
                    if service is None
                    else tuple(
                        PhysicianCatalogEntry(UserId(pk), label)
                        for pk, label in service_practitioners(clinic_id)
                    ),
                    key=lambda item: (item.display_label.casefold(), item.user_id),
                )
            )
        except (CurrentActorError, PatientClinicEnrollment.DoesNotExist) as error:
            raise AppointmentAccessDeniedError from error

        physician_ids = tuple(item.user_id for item in catalog)
        rows = AvailabilityBlock.objects.filter(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            practitioner_id__in=physician_ids,
            retired_at__isnull=True,
            end_at__gt=timezone.now(),
        ).order_by("practitioner_id", "start_at", "pk")
        windows_by_practitioner: dict[UUID, list[BookingAvailabilityWindow]] = {
            physician_id: [] for physician_id in physician_ids
        }
        for row in rows:
            if row.practitioner_id is None:
                continue
            windows_by_practitioner[row.practitioner_id].append(
                BookingAvailabilityWindow(
                    availability_id=row.pk,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    start_local=format_local_minute(row.start_at, timezone_key),
                    end_local=format_local_minute(row.end_at, timezone_key),
                )
            )
        result = BookingPreparation(
            enrollment_id=enrollment.pk,
            patient_display_name=enrollment.patient.full_name,
            service_type_id=service.pk if service else None,
            resource_ids=tuple(row.pk for row in resources),
            duration_min=service.duration_min if service else None,
            buffer_before=service.buffer_before if service else 0,
            buffer_after=service.buffer_after if service else 0,
            practitioners=tuple(
                BookingPractitioner(
                    practitioner_id=physician.user_id,
                    display_identifier=physician.display_label,
                    windows=tuple(windows_by_practitioner[physician.user_id]),
                )
                for physician in catalog
            ),
        )
        record_phase1_event(
            "scheduling.booking.viewed",
            clinic_id=clinic_id,
            affected_record_id=enrollment.pk,
        )
        return result
