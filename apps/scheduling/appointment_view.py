"""Manager-authorized appointment read for transition forms."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db import DataError, transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    practitioner_display_label,
)
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    authorized_appointment_manager_clinic,
)
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import format_local_minute


@dataclass(frozen=True, slots=True)
class AppointmentTransitionView:
    """Expose only transition-safe appointment display fields."""

    appointment_id: UUID
    clinic_id: UUID
    patient_display_name: str
    practitioner_id: UUID
    practitioner_display_identifier: str
    start_local: str
    end_local: str
    status: str
    cancellation_reason: str | None


def view_appointment_for_transition(
    *,
    appointment_id: UUID,
) -> AppointmentTransitionView:
    """Return one opaque manager-visible appointment and append one view event."""
    if type(appointment_id) is not UUID:
        raise AppointmentAccessDeniedError
    with transaction.atomic():
        try:
            appointment = Appointment.objects.select_related("patient").get(
                pk=appointment_id
            )
            clinic = authorized_appointment_manager_clinic(appointment.clinic_id)
            if clinic.organization_id != appointment.organization_id:
                raise AppointmentAccessDeniedError
            timezone_key = clinic.timezone
            if not isinstance(timezone_key, str):
                raise AppointmentAccessDeniedError
            display_identifier = practitioner_display_label(
                appointment.practitioner_id,
                appointment.clinic_id,
            )
        except (CurrentActorError, Appointment.DoesNotExist, DataError) as error:
            raise AppointmentAccessDeniedError from error
        result = AppointmentTransitionView(
            appointment_id=appointment.pk,
            clinic_id=appointment.clinic_id,
            patient_display_name=appointment.patient.full_name,
            practitioner_id=appointment.practitioner_id,
            practitioner_display_identifier=display_identifier,
            start_local=format_local_minute(appointment.start_at, timezone_key),
            end_local=format_local_minute(appointment.end_at, timezone_key),
            status=appointment.status,
            cancellation_reason=appointment.cancellation_reason,
        )
        record_phase1_event(
            "scheduling.appointment.viewed",
            clinic_id=appointment.clinic_id,
            affected_record_id=appointment.pk,
        )
        return result
