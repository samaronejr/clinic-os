"""Organization-scoped practitioner availability promises."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar, Final, TypedDict

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    ArrayField,
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.identity.models import Clinic
from apps.intake.models import Patient
from apps.scheduling.resource_models import (
    Absence,
    AppointmentResource,
    AvailabilityTemplate,
    Holiday,
    Resource,
    ServiceType,
)
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


__all__ = (
    "Absence",
    "Appointment",
    "AppointmentResource",
    "AvailabilityBlock",
    "AvailabilityTemplate",
    "Holiday",
    "PatientBookingEvent",
    "Resource",
    "ServiceType",
    "WaitlistEntry",
    "WaitlistOffer",
)


class _NullableReasonOptions(TypedDict):
    null: bool
    blank: bool


_NULLABLE_REASON_OPTIONS: Final[_NullableReasonOptions] = {
    "null": True,
    "blank": True,
}


class AvailabilityBlock(TenantScopedModel):
    """One immutable UTC interval a practitioner or resource promises."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    resource = models.ForeignKey(
        Resource, on_delete=models.PROTECT, null=True, blank=True
    )
    template = models.ForeignKey(
        AvailabilityTemplate, on_delete=models.PROTECT, null=True, blank=True
    )
    generated_date = models.DateField(null=True, blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep active promises immediate and index manager query paths."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="scheduling_availability_org_idempotency_uniq",
            ),
            models.CheckConstraint(
                condition=Q(practitioner__isnull=False, resource__isnull=True)
                | Q(practitioner__isnull=True, resource__isnull=False),
                name="scheduling_availability_subject",
            ),
            models.CheckConstraint(
                condition=Q(template__isnull=True, generated_date__isnull=True)
                | Q(template__isnull=False, generated_date__isnull=False),
                name="scheduling_availability_generation",
            ),
            models.UniqueConstraint(
                fields=("template", "generated_date"),
                name="scheduling_template_date_uniq",
            ),
            ExclusionConstraint(
                name="scheduling_availability_resource_excl",
                expressions=(
                    ("resource", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(retired_at__isnull=True),
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_availability_org_clinic_id_uniq",
            ),
            ExclusionConstraint(
                name="scheduling_availability_active_practitioner_excl",
                expressions=(
                    ("practitioner", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(retired_at__isnull=True),
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("clinic", "start_at"),
                name="sched_avail_clinic_start_idx",
            ),
            models.Index(
                fields=("practitioner", "start_at"),
                name="sched_avail_pract_start_idx",
            ),
        ]


class PatientBookingEvent(TenantScopedModel):
    """Database-owned immutable receipt for a patient scheduling transition."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    appointment = models.ForeignKey("Appointment", on_delete=models.PROTECT)
    patient_session_id = models.UUIDField()
    action = models.CharField(max_length=16)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class Appointment(TenantScopedModel):
    """One clinic booking for an enrolled organization patient."""

    class Status(models.TextChoices):
        """The complete Phase 1A appointment lifecycle."""

        SCHEDULED = "scheduled", "Scheduled"
        CANCELLED = "cancelled", "Cancelled"

    class CancellationReason(models.TextChoices):
        """The fixed non-free-text cancellation vocabulary."""

        PATIENT_REQUEST = "patient_request", "Patient request"
        CLINIC_REQUEST = "clinic_request", "Clinic request"
        PRACTITIONER_UNAVAILABLE = (
            "practitioner_unavailable",
            "Practitioner unavailable",
        )
        DUPLICATE = "duplicate", "Duplicate"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    service_type = models.ForeignKey(
        ServiceType, on_delete=models.PROTECT, null=True, blank=True
    )
    resource_ids = ArrayField(
        models.UUIDField(), default=list, db_default=[], blank=True
    )
    buffer_before = models.PositiveSmallIntegerField(
        default=0, db_default=0, editable=False
    )
    buffer_after = models.PositiveSmallIntegerField(
        default=0, db_default=0, editable=False
    )
    practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    status = models.CharField(
        max_length=10,
        choices=Status,
        default=Status.SCHEDULED,
    )
    cancellation_reason = models.CharField(
        max_length=32,
        choices=CancellationReason,
        **_NULLABLE_REASON_OPTIONS,
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep scheduled bookings immediate and organization-idempotent."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="scheduling_appointment_org_idempotency_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_appointment_org_clinic_id_uniq",
            ),
            ExclusionConstraint(
                name="scheduling_z_buffer_practitioner_excl",
                expressions=(
                    ("practitioner", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            "buffer_before",
                            "buffer_after",
                            function="clinic_app.scheduling_effective_interval",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(status__in=["held", "scheduled", "arrived", "in_progress"]),
            ),
            ExclusionConstraint(
                name="scheduling_appointment_scheduled_practitioner_excl",
                expressions=(
                    ("practitioner", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(status="scheduled"),
            ),
            ExclusionConstraint(
                name="scheduling_appointment_scheduled_patient_excl",
                expressions=(
                    ("patient", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(status="scheduled"),
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("clinic", "start_at"),
                name="sched_appt_clinic_start_idx",
            ),
            models.Index(
                fields=("practitioner", "start_at"),
                name="sched_appt_pract_start_idx",
            ),
            models.Index(
                fields=("patient", "start_at"),
                name="sched_appt_patient_start_idx",
            ),
            models.Index(
                fields=("status", "start_at"),
                name="sched_appt_status_start_idx",
            ),
        ]


class WaitlistEntry(TenantScopedModel):
    """Immutable request window, ordered by its database-assigned FIFO number."""

    class State(models.TextChoices):
        """Retain terminal requests rather than deleting history."""

        WAITING = "waiting", "Na fila"
        OFFERED = "offered", "Oferta enviada ao portal"
        EXPIRED = "expired", "Oferta expirada"
        DECLINED = "declined", "Oferta recusada"
        FULFILLED = "fulfilled", "Consulta agendada"

    id = models.BigAutoField(primary_key=True)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    practitioner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    practitioner_label = models.CharField(max_length=150)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    state = models.CharField(max_length=16, choices=State, default=State.WAITING)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Index the clinic queue and reject inverted windows."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(end_at__gt=models.F("start_at")),
                name="waitlist_entry_window",
            ),
            models.CheckConstraint(
                condition=Q(
                    state__in=["waiting", "offered", "expired", "declined", "fulfilled"]
                ),
                name="waitlist_entry_state",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("clinic", "practitioner", "state", "id"),
                name="waitlist_fifo_idx",
            ),
        ]


class WaitlistOffer(TenantScopedModel):
    """An expiring invitation, never a reservation or availability promise."""

    class State(models.TextChoices):
        """Every terminal offer remains inspectable by its patient and staff."""

        PENDING = "pending", "Aguardando resposta"
        ACCEPTED = "accepted", "Consulta agendada"
        EXPIRED = "expired", "Expirada"
        DECLINED = "declined", "Recusada"
        UNAVAILABLE = "unavailable", "Horário indisponível"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    entry = models.ForeignKey(
        WaitlistEntry, on_delete=models.PROTECT, related_name="offers"
    )
    practitioner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=16, choices=State, default=State.PENDING)
    appointment = models.OneToOneField(
        Appointment, null=True, blank=True, on_delete=models.PROTECT
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Only one overlapping live offer; bookings remain entirely separate."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(
                    end_at__gt=models.F("start_at"),
                    expires_at__gt=models.F("created_at"),
                ),
                name="waitlist_offer_window",
            ),
            models.CheckConstraint(
                condition=Q(
                    state__in=[
                        "pending",
                        "accepted",
                        "expired",
                        "declined",
                        "unavailable",
                    ]
                ),
                name="waitlist_offer_state",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state="accepted", appointment__isnull=False)
                    | (~Q(state="accepted") & Q(appointment__isnull=True))
                ),
                name="waitlist_offer_booking",
            ),
            models.UniqueConstraint(
                fields=("entry",),
                condition=Q(state="pending"),
                name="waitlist_entry_one_offer",
            ),
            ExclusionConstraint(
                name="waitlist_offer_one_opening",
                expressions=(
                    ("practitioner", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(state="pending"),
            ),
        ]
