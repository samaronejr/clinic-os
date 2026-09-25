"""Transaction-bound practitioner availability retirement service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    authorized_manager_clinic,
)
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import Appointment, AvailabilityBlock

if TYPE_CHECKING:
    from uuid import UUID


class AvailabilityHasAppointmentsError(Exception):
    """Refuse retirement while future scheduled appointments depend on a block."""

    def __init__(self) -> None:
        """Expose one stable non-identifying dependency message."""
        super().__init__("availability has future appointments")


def _target(
    organization_id: UUID,
    clinic_id: UUID,
    availability_id: UUID,
) -> AvailabilityBlock:
    try:
        return AvailabilityBlock.objects.get(
            organization_id=organization_id,
            clinic_id=clinic_id,
            pk=availability_id,
        )
    except AvailabilityBlock.DoesNotExist as error:
        raise AvailabilityAccessDeniedError from error


def retire_availability(
    *,
    clinic_id: UUID,
    availability_id: UUID,
) -> AvailabilityBlock:
    """Retire one manager-authorized block without deleting history."""
    with transaction.atomic():
        clinic = authorized_manager_clinic(clinic_id)
        discovered = _target(clinic.organization_id, clinic_id, availability_id)
        if discovered.practitioner_id is None:
            raise AvailabilityAccessDeniedError
        lock_keys = (
            clinic_lock_key(clinic_id),
            *user_lock_keys((discovered.practitioner_id,)),
        )
        acquire_advisory_locks(lock_keys)
        clinic = authorized_manager_clinic(clinic_id)
        try:
            block = AvailabilityBlock.objects.select_for_update().get(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                practitioner_id=discovered.practitioner_id,
                pk=availability_id,
            )
        except AvailabilityBlock.DoesNotExist as error:
            raise AvailabilityAccessDeniedError from error
        if block.retired_at is not None:
            return block
        future_appointment_ids = tuple(
            Appointment.objects.select_for_update()
            .filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                practitioner_id=block.practitioner_id,
                status=Appointment.Status.SCHEDULED,
                start_at__gt=timezone.now(),
                start_at__gte=block.start_at,
                end_at__lte=block.end_at,
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if future_appointment_ids:
            raise AvailabilityHasAppointmentsError
        block.retired_at = timezone.now()
        block.save(update_fields=("retired_at", "updated_at"))
        record_phase1_event(
            "scheduling.availability.retired",
            clinic_id=clinic_id,
            affected_record_id=block.pk,
        )
        return block
