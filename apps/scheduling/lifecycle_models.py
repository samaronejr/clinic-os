"""Appointment lifecycle v2 receipts and recurring series (todo 22)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.identity.models import Clinic
from apps.intake.models import Patient
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint

MAX_SERIES_OCCURRENCES = 52


class AppointmentSeries(TenantScopedModel):
    """An immutable recurrence rule; occurrences are ordinary appointments."""

    class Frequency(models.TextChoices):
        """The RRULE subset: weekly, biweekly or monthly by weekday."""

        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Every two weeks"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    practitioner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    frequency = models.CharField(max_length=16, choices=Frequency)
    weekday = models.PositiveSmallIntegerField()
    month_week = models.PositiveSmallIntegerField(null=True, blank=True)
    first_date = models.DateField()
    start_local = models.TimeField()
    end_local = models.TimeField()
    timezone = models.CharField(max_length=63)
    count = models.PositiveSmallIntegerField(null=True, blank=True)
    until = models.DateField(null=True, blank=True)
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Close the rule vocabulary and bound materialization."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="scheduling_series_org_idempotency_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_series_scope",
            ),
            models.CheckConstraint(
                condition=Q(frequency__in=["weekly", "biweekly", "monthly"])
                & Q(weekday__lte=6)
                & Q(end_local__gt=models.F("start_local"))
                & (
                    Q(frequency="monthly", month_week__gte=1, month_week__lte=4)
                    | (~Q(frequency="monthly") & Q(month_week__isnull=True))
                )
                & (
                    Q(
                        count__gte=1,
                        count__lte=MAX_SERIES_OCCURRENCES,
                        until__isnull=True,
                    )
                    | Q(count__isnull=True, until__gte=models.F("first_date"))
                ),
                name="scheduling_series_rule",
            ),
        ]


class SeriesException(TenantScopedModel):
    """An immutable 'this occurrence' or 'this and future' edit receipt."""

    class Scope(models.TextChoices):
        """Which occurrences an edit addresses; history is never deleted."""

        THIS = "this", "This occurrence"
        FUTURE = "future", "This and future occurrences"

    class Kind(models.TextChoices):
        """The closed edit vocabulary."""

        CANCELLED = "cancelled", "Cancelled"
        MOVED = "moved", "Moved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    series = models.ForeignKey(
        AppointmentSeries, on_delete=models.PROTECT, related_name="exceptions"
    )
    occurrence_index = models.PositiveSmallIntegerField()
    scope = models.CharField(max_length=8, choices=Scope)
    kind = models.CharField(max_length=16, choices=Kind)
    start_local = models.TimeField(null=True, blank=True)
    command_id = models.UUIDField()
    affected_count = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One receipt per command; moves carry their new local start."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "command_id"),
                name="scheduling_series_exception_command_uniq",
            ),
            models.CheckConstraint(
                condition=Q(scope__in=["this", "future"])
                & Q(occurrence_index__gte=1)
                & Q(occurrence_index__lte=MAX_SERIES_OCCURRENCES)
                & (
                    Q(kind="moved", start_local__isnull=False)
                    | Q(kind="cancelled", start_local__isnull=True)
                ),
                name="scheduling_series_exception_shape",
            ),
        ]


class AppointmentTransition(TenantScopedModel):
    """Database-owned lifecycle receipt; the runtime role can only read it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    appointment = models.ForeignKey(
        "scheduling.Appointment", on_delete=models.PROTECT, related_name="transitions"
    )
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    revision = models.PositiveIntegerField()
    command_id = models.UUIDField(null=True, blank=True)
    actor_kind = models.CharField(max_length=8)
    occurred_at = models.DateTimeField()

    class Meta:
        """A command identifier names exactly one transition per organization."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "command_id"),
                name="scheduling_transition_command_uniq",
            ),
            models.UniqueConstraint(
                fields=("appointment", "revision"),
                name="scheduling_transition_revision_uniq",
            ),
            models.CheckConstraint(
                condition=Q(actor_kind__in=["staff", "patient", "machine"]),
                name="scheduling_transition_actor_kind",
            ),
        ]
