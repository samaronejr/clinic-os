"""Cancel/recreate forward bookings and queue entries; history stays bound."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid5

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.models import Encounter
from apps.intake.merge_transfer import (
    ForwardLink,
    merge_transfer_scope,
    record_forward_link,
)
from apps.scheduling.appointment_cancellation import cancel_appointment
from apps.scheduling.appointment_persistence import insert_appointment
from apps.scheduling.appointment_values import (
    AppointmentLocalRange,
    CreateAppointmentRequest,
    prepare_appointment,
)
from apps.scheduling.models import Appointment, WaitlistEntry, WaitlistOffer
from apps.scheduling.timezones import format_local_minute

if TYPE_CHECKING:
    from uuid import UUID


def transfer_patient_forward(*, clinic_id: UUID, merge_decision_id: UUID) -> None:
    """Transfer only unencountered forward work under a fresh stored decision."""
    scope = merge_transfer_scope(
        clinic_id=clinic_id, merge_decision_id=merge_decision_id
    )
    with transaction.atomic():
        bound_encounter = Encounter.objects.filter(appointment_id=OuterRef("pk"))
        appointments = (
            Appointment.objects.select_for_update()
            .filter(
                clinic_id=clinic_id,
                patient_id=scope.original.patient_id,
                status="scheduled",
                start_at__gt=timezone.now(),
            )
            .filter(~Exists(bound_encounter))
            .order_by("id")
        )
        for old in appointments:
            cancel_appointment(
                appointment_id=old.pk,
                reason=Appointment.CancellationReason.DUPLICATE,
            )
            request = CreateAppointmentRequest(
                clinic_id=clinic_id,
                enrollment_id=scope.survivor.pk,
                practitioner_id=old.practitioner_id,
                local_range=AppointmentLocalRange(
                    format_local_minute(old.start_at, scope.clinic.timezone or ""),
                    format_local_minute(old.end_at, scope.clinic.timezone or ""),
                ),
                idempotency_key=uuid5(scope.decision.pk, f"appointment:{old.pk}"),
                service_type_id=old.service_type_id,
                resource_ids=tuple(old.resource_ids),
            )
            prepared = prepare_appointment(scope.clinic, request)
            successor, _created = insert_appointment(scope.clinic, request, prepared)
            record_forward_link(scope, ForwardLink("appointment", old.pk, successor.pk))
            record_phase1_event(
                "scheduling.appointment.created",
                clinic_id=clinic_id,
                affected_record_id=successor.pk,
            )
        entries = (
            WaitlistEntry.objects.select_for_update()
            .filter(
                clinic_id=clinic_id,
                patient_id=scope.original.patient_id,
                state__in=("waiting", "offered"),
            )
            .order_by("id")
        )
        for entry in entries:
            for offer in (
                WaitlistOffer.objects.select_for_update()
                .filter(entry=entry, state="pending")
                .order_by("id")
            ):
                offer.state = WaitlistOffer.State.DECLINED
                offer.responded_at = timezone.now()
                offer.save(update_fields=("state", "responded_at"))
            entry.state = WaitlistEntry.State.DECLINED
            entry.save(update_fields=("state",))
            successor_entry = WaitlistEntry.objects.create(
                organization_id=scope.clinic.organization_id,
                clinic_id=clinic_id,
                patient_id=scope.survivor.patient_id,
                enrollment=scope.survivor,
                practitioner_id=entry.practitioner_id,
                practitioner_label=entry.practitioner_label,
                start_at=entry.start_at,
                end_at=entry.end_at,
            )
            record_forward_link(
                scope, ForwardLink("waitlist", entry.pk, successor_entry.pk)
            )
