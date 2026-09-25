"""Resource-aware booking preparation; PostgreSQL remains the final authority."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import connection
from django.db.models import Q

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_errors import AppointmentAvailabilityError
from apps.scheduling.appointment_values import require_active_practitioner
from apps.scheduling.models import (
    Appointment,
    AppointmentResource,
    AvailabilityBlock,
    Resource,
    ServiceType,
)
from apps.scheduling.patient_authority import (
    authorized_appointment_clinic,
    patient_booking_scope,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_services import require_open_window

if TYPE_CHECKING:
    from datetime import datetime

MAX_BOOKING_RESOURCES: Final = 16


def authorized_service_clinic(
    clinic_id: UUID, practitioner_id: UUID | None = None
) -> Clinic:
    """Use RP booking permissions without widening legacy entrypoints."""
    try:
        try:
            require_permission("appointment.book", clinic_id=clinic_id)
        except CurrentActorError:
            actor = require_permission("appointment.book_own", clinic_id=clinic_id)
            if practitioner_id is not None and actor != practitioner_id:
                raise AppointmentAccessDeniedError from None
        return Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise AppointmentAccessDeniedError from error


def authorized_transition_clinic(appointment: Appointment) -> Clinic:
    """Retain legacy authority; service bookings use the RP move permission."""
    if appointment.service_type_id is None or patient_booking_scope() is not None:
        return authorized_appointment_clinic(appointment.clinic_id)
    try:
        try:
            require_permission("appointment.move", clinic_id=appointment.clinic_id)
        except CurrentActorError:
            actor = require_permission(
                "appointment.move_own", clinic_id=appointment.clinic_id
            )
            if actor != appointment.practitioner_id:
                raise AppointmentAccessDeniedError from None
        return Clinic.objects.get(pk=appointment.clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise AppointmentAccessDeniedError from error


def require_transition_practitioner(appointment: Appointment) -> None:
    """Recheck the appropriate staff or patient professional catalog after locks."""
    if appointment.service_type_id is None or patient_booking_scope() is not None:
        require_active_practitioner(appointment.clinic_id, appointment.practitioner_id)
    elif appointment.practitioner_id not in {
        pk for pk, _ in service_practitioners(appointment.clinic_id)
    }:
        raise AppointmentAvailabilityError


def service_practitioners(clinic_id: UUID) -> tuple[tuple[UUID, str], ...]:
    """Resolve active professional labels through the database scope boundary."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.scheduling_service_practitioners(%s)", [clinic_id]
        )
        return tuple((row[0], row[1]) for row in cursor.fetchall())


def booking_selection(
    *, clinic: Clinic, service_type_id: UUID | None, resource_ids: tuple[UUID, ...]
) -> tuple[ServiceType | None, tuple[Resource, ...]]:
    """Reject unknown and foreign selectors identically, before scheduling checks."""
    if service_type_id is None:
        if resource_ids:
            raise AppointmentAccessDeniedError
        return None, ()
    if (
        type(service_type_id) is not UUID
        or not isinstance(resource_ids, tuple)
        or len(resource_ids) > MAX_BOOKING_RESOURCES
        or any(type(pk) is not UUID for pk in resource_ids)
        or len(set(resource_ids)) != len(resource_ids)
    ):
        raise AppointmentAccessDeniedError
    try:
        service = ServiceType.objects.get(pk=service_type_id, clinic=clinic)
    except ServiceType.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error
    resources = tuple(
        Resource.objects.filter(pk__in=resource_ids, clinic=clinic).order_by("pk")
    )
    if len(resources) != len(resource_ids):
        raise AppointmentAccessDeniedError
    return service, resources


def validate_resource_window(
    *,
    clinic: Clinic,
    practitioner_id: UUID,
    selection: tuple[ServiceType | None, tuple[Resource, ...]],
    interval: tuple[datetime, datetime],
    appointment_id: UUID | None = None,
) -> None:
    """Recheck selected capacity and buffers under ordered write gates."""
    service, resources = selection
    start_at, end_at = interval
    if service is None:
        require_open_window(
            clinic_id=clinic.pk,
            practitioner_id=practitioner_id,
            resource_ids=(),
            start_at=start_at,
            end_at=end_at,
        )
        return
    if (appointment_id is None and not service.active) or any(
        not row.active for row in resources
    ):
        msg = "resource_conflict"
        raise SchedulingRuleError(msg)
    if not set(service.required_resource_kinds) <= {row.kind for row in resources}:
        msg = "resource_conflict"
        raise SchedulingRuleError(msg)
    if end_at - start_at != timedelta(minutes=service.duration_min):
        msg = "buffer_violation"
        raise SchedulingRuleError(msg)
    start_at -= timedelta(minutes=service.buffer_before)
    end_at += timedelta(minutes=service.buffer_after)
    require_open_window(
        clinic_id=clinic.pk,
        practitioner_id=practitioner_id,
        resource_ids=tuple(row.pk for row in resources),
        start_at=start_at,
        end_at=end_at,
    )
    for resource in resources:
        if (
            not AvailabilityBlock.objects.filter(
                clinic=clinic,
                resource=resource,
                retired_at__isnull=True,
                start_at__lte=start_at,
                end_at__gte=end_at,
            )
            .filter(Q(template__isnull=True) | Q(template__active=True))
            .exists()
        ):
            msg = "outside_template"
            raise SchedulingRuleError(msg)
        used = set(
            AppointmentResource.objects.filter(
                resource=resource,
                occupied=True,
                start_at__lt=end_at,
                end_at__gt=start_at,
            )
            .exclude(
                appointment_id__in=() if appointment_id is None else (appointment_id,)
            )
            .values_list("unit", flat=True)
        )
        if len(used) >= resource.capacity:
            msg = "resource_conflict"
            raise SchedulingRuleError(msg)
    if (
        not AvailabilityBlock.objects.filter(
            clinic=clinic,
            practitioner_id=practitioner_id,
            retired_at__isnull=True,
            start_at__lte=start_at,
            end_at__gte=end_at,
        )
        .filter(Q(template__isnull=True) | Q(template__active=True))
        .exists()
    ):
        msg = "buffer_violation"
        raise SchedulingRuleError(msg)
