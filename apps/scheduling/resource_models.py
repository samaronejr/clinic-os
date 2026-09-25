"""Immutable scheduling definitions and database-owned capacity reservations."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

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
from django.utils.translation import gettext_lazy as _

from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class Resource(TenantScopedModel):
    """One room, equipment pool or location with immutable unit capacity."""

    class Kind(models.TextChoices):
        """Closed resource taxonomy."""

        ROOM = "room", _("Room")
        EQUIPMENT = "equipment", _("Equipment")
        LOCATION = "location", _("Location")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind)
    name = models.CharField(max_length=120)
    capacity = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        """Bound identical units and permit composite scope references."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(capacity__gte=1, capacity__lte=64),
                name="scheduling_resource_capacity",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["room", "equipment", "location"]),
                name="scheduling_resource_kind",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_resource_scope",
            ),
        ]


class ServiceType(TenantScopedModel):
    """Version-by-addition service definition; existing bookings keep their rules."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    duration_min = models.PositiveSmallIntegerField()
    buffer_before = models.PositiveSmallIntegerField(default=0)
    buffer_after = models.PositiveSmallIntegerField(default=0)
    required_professional_roles = ArrayField(
        models.CharField(max_length=32), default=list
    )
    required_resource_kinds = ArrayField(models.CharField(max_length=16), default=list)
    insurer_billable = models.BooleanField(default=False)
    price_ref = models.CharField(max_length=80, blank=True, default="")
    active = models.BooleanField(default=True)

    class Meta:
        """Reject zero duration and unbounded buffers at the database boundary."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(
                    duration_min__gte=1,
                    duration_min__lte=720,
                    buffer_before__lte=240,
                    buffer_after__lte=240,
                ),
                name="scheduling_service_duration",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="scheduling_service_scope"
            ),
        ]


class AvailabilityTemplate(TenantScopedModel):
    """A single clinic-local daily interval on a closed set of weekdays."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    resource = models.ForeignKey(
        Resource, null=True, blank=True, on_delete=models.PROTECT
    )
    weekdays = ArrayField(models.PositiveSmallIntegerField())
    start_local = models.TimeField()
    end_local = models.TimeField()
    valid_from = models.DateField()
    valid_to = models.DateField()
    timezone = models.CharField(max_length=63)
    active = models.BooleanField(default=True)

    class Meta:
        """Require exactly one owner and one positive finite local window."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(practitioner__isnull=False, resource__isnull=True)
                | Q(practitioner__isnull=True, resource__isnull=False),
                name="scheduling_template_subject",
            ),
            models.CheckConstraint(
                condition=Q(
                    valid_to__gte=models.F("valid_from"),
                    end_local__gt=models.F("start_local"),
                ),
                name="scheduling_template_window",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_template_scope",
            ),
        ]


class Holiday(TenantScopedModel):
    """Clinic closure; a closed reason code cannot contain personal details."""

    class Reason(models.TextChoices):
        """Non-clinical closure vocabulary."""

        HOLIDAY = "holiday", _("Holiday")
        CLOSURE = "closure", _("Clinic closure")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reason = models.CharField(max_length=16, choices=Reason)
    active = models.BooleanField(default=True)

    class Meta:
        """Retain historical closures rather than deleting them."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(end_at__gt=models.F("start_at")),
                name="scheduling_holiday_window",
            ),
            models.CheckConstraint(
                condition=Q(reason__in=["holiday", "closure"]),
                name="scheduling_holiday_reason",
            ),
        ]


class Absence(TenantScopedModel):
    """Practitioner or resource unavailability without free-text health data."""

    class Reason(models.TextChoices):
        """Non-clinical absence vocabulary."""

        UNAVAILABLE = "unavailable", _("Unavailable")
        MAINTENANCE = "maintenance", _("Maintenance")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    resource = models.ForeignKey(
        Resource, null=True, blank=True, on_delete=models.PROTECT
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reason = models.CharField(max_length=16, choices=Reason)
    active = models.BooleanField(default=True)

    class Meta:
        """Bind one subject and a positive half-open interval."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(practitioner__isnull=False, resource__isnull=True)
                | Q(practitioner__isnull=True, resource__isnull=False),
                name="scheduling_absence_subject",
            ),
            models.CheckConstraint(
                condition=Q(end_at__gt=models.F("start_at")),
                name="scheduling_absence_window",
            ),
            models.CheckConstraint(
                condition=Q(reason__in=["unavailable", "maintenance"]),
                name="scheduling_absence_reason",
            ),
        ]


class AppointmentResource(TenantScopedModel):
    """Database-derived effective interval on one immutable resource unit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    appointment = models.ForeignKey("scheduling.Appointment", on_delete=models.PROTECT)
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    unit = models.PositiveSmallIntegerField()
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    occupied = models.BooleanField(default=True)

    class Meta:
        """Enforce unit capacity even when application locking is bypassed."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("appointment", "resource"),
                name="scheduling_appointment_resource_uniq",
            ),
            models.CheckConstraint(
                condition=Q(unit__gte=1, unit__lte=64, end_at__gt=models.F("start_at")),
                name="scheduling_reservation_window",
            ),
            ExclusionConstraint(
                name="scheduling_resource_capacity_excl",
                expressions=(
                    ("resource", RangeOperators.EQUAL),
                    ("unit", RangeOperators.EQUAL),
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
                condition=Q(occupied=True),
            ),
        ]
