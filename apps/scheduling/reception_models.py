"""Reception operations contain references and closed states, never clinical text."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.tenancy.models import TenantScopedModel


class QueueEntry(TenantScopedModel):
    """A retained arrival ticket in one location's database-ordered queue."""

    class State(models.TextChoices):
        """One-way queue states; clinical progress follows the appointment."""

        WAITING = "waiting", _("Waiting")
        CALLED = "called", _("Called")
        IN_ROOM = "in_room", _("In room")
        DONE = "done", _("Done")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    appointment = models.OneToOneField(
        "scheduling.Appointment", on_delete=models.PROTECT
    )
    location = models.ForeignKey(
        "scheduling.Resource", on_delete=models.PROTECT, related_name="+"
    )
    position = models.PositiveBigIntegerField(default=0, editable=False)
    ticket_code = models.CharField(max_length=32, default="", editable=False)
    state = models.CharField(max_length=16, choices=State, default=State.WAITING)
    room = models.ForeignKey(
        "scheduling.Resource", on_delete=models.PROTECT, null=True, related_name="+"
    )
    room_unit = models.PositiveSmallIntegerField(null=True)
    called_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Enforce FIFO identity and exclusive active occupancy of each room unit."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "position"), name="scheduling_queue_position"
            ),
            models.UniqueConstraint(
                fields=("clinic", "ticket_code"), name="scheduling_queue_ticket"
            ),
            models.UniqueConstraint(
                fields=("room", "room_unit"),
                condition=Q(state__in=("called", "in_room")),
                name="scheduling_queue_room_occupied",
            ),
            models.CheckConstraint(
                condition=Q(state__in=("waiting", "called", "in_room", "done")),
                name="scheduling_queue_state",
            ),
            models.CheckConstraint(
                condition=(
                    Q(room__isnull=True, room_unit__isnull=True, called_at__isnull=True)
                    | Q(
                        room__isnull=False,
                        room_unit__gte=1,
                        room_unit__lte=64,
                        called_at__isnull=False,
                    )
                ),
                name="scheduling_queue_room_binding",
            ),
        ]


class RoomStatus(TenantScopedModel):
    """An operational room-unit condition; active occupancy is queue-derived."""

    class State(models.TextChoices):
        """Operational state without inferring clinical readiness."""

        AVAILABLE = "available", _("Available")
        CLEANING = "cleaning", _("Cleaning")
        UNAVAILABLE = "unavailable", _("Unavailable")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    room = models.ForeignKey("scheduling.Resource", on_delete=models.PROTECT)
    unit = models.PositiveSmallIntegerField(default=1)
    display_number = models.PositiveIntegerField(default=0, editable=False)
    state = models.CharField(max_length=16, choices=State, default=State.AVAILABLE)
    revision = models.PositiveIntegerField(default=1, editable=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Keep one bounded, versioned condition per physical room unit."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("room", "unit"), name="scheduling_room_status_unit"
            ),
            models.UniqueConstraint(
                fields=("clinic", "display_number"),
                name="scheduling_room_display_number",
            ),
            models.CheckConstraint(
                condition=Q(unit__gte=1, unit__lte=64),
                name="scheduling_room_status_capacity",
            ),
            models.CheckConstraint(
                condition=Q(state__in=("available", "cleaning", "unavailable")),
                name="scheduling_room_status_state",
            ),
        ]


class ClinicianDelay(TenantScopedModel):
    """A bounded administrative delay estimate, not a clinical message."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    practitioner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    minutes = models.PositiveSmallIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1, editable=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Limit delay estimates and retain the latest explicit broadcast."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "practitioner"), name="scheduling_delay_practitioner"
            ),
            models.CheckConstraint(
                condition=Q(minutes__lte=720), name="scheduling_delay_minutes"
            ),
        ]


class DisplayDevice(TenantScopedModel):
    """Read-only public-display authority; only a digest is retained."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
