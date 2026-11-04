"""Urgent patient concerns (todo 55): a clinic coverage policy and a human queue.

A concern is a pointer to the patient's own portal message (whose encrypted text
stays in comms) plus its accountable state. The coverage policy is clinic
configuration: hours, responsible roles and a response window in business hours.
It is displayed to patients honestly; no policy means no coverage claim.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.tenancy.models import TenantScopedModel

RESPONSIBLE_ROLES: Final = ("nurse", "physician", "receptionist", "clinic_manager")
CONCERN_STATES: Final = ("open", "acknowledged", "resolved")
MAX_RESPONSE_HOURS: Final = 72


class UrgentCoveragePolicy(TenantScopedModel):
    """One immutable version of a clinic's urgent-concern staffing policy."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    weekdays = ArrayField(models.PositiveSmallIntegerField(), size=7)
    opens_at = models.TimeField()
    closes_at = models.TimeField()
    response_hours = models.PositiveSmallIntegerField()
    responsible_roles = ArrayField(models.CharField(max_length=20), size=4)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A window inside one day and a bounded, positive response time."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "version"), name="workflows_coverage_version"
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_coverage_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1)
                & models.Q(closes_at__gt=models.F("opens_at"))
                & models.Q(response_hours__gte=1)
                & models.Q(response_hours__lte=MAX_RESPONSE_HOURS),
                name="workflows_coverage_terms",
            ),
        ]


class UrgentConcern(TenantScopedModel):
    """One urgent patient message routed to an accountable human queue."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    message = models.OneToOneField(
        "comms.Message", on_delete=models.PROTECT, related_name="+"
    )
    conversation = models.ForeignKey(
        "comms.Conversation", on_delete=models.PROTECT, related_name="+"
    )
    policy = models.ForeignKey(
        UrgentCoveragePolicy, null=True, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=16, default="open")
    reported_at = models.DateTimeField()
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        """States carry exactly the attribution they need."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_concern_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(state="open", acknowledged_by__isnull=True)
                | models.Q(
                    state="acknowledged",
                    acknowledged_by__isnull=False,
                    resolved_by__isnull=True,
                )
                | models.Q(
                    state="resolved",
                    acknowledged_by__isnull=False,
                    resolved_by__isnull=False,
                ),
                name="workflows_concern_state",
            ),
        ]
