"""Organization-scoped practitioner availability promises."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar, Final, TypedDict

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.db import models
from django.db.models import Q

from apps.identity.models import Clinic
from apps.intake.models import Patient
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class _NullableReasonOptions(TypedDict):
    null: bool
    blank: bool


_NULLABLE_REASON_OPTIONS: Final[_NullableReasonOptions] = {
    "null": True,
    "blank": True,
}


class AvailabilityBlock(TenantScopedModel):
    """One immutable UTC interval a practitioner promises to one clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
    )
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
