"""Result release notices (todo 55): approved wording and delivery provenance.

The notice never carries result content: it is one clinic-wide sentence a
clinician approved, bound by exact version into each delivery. A delivery row is
provenance only; its delivered state is the portal message's own outbox state.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.db import models

from apps.tenancy.models import TenantScopedModel

AUDIENCES: Final = ("patient", "delegate")
FAILURES: Final = ("", "scope_revoked", "send_failed")
MAX_NOTICE_CHARS: Final = 300


class ReleaseNoticeTemplate(TenantScopedModel):
    """One immutable, clinician-approved notice version of a clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    body = models.CharField(max_length=MAX_NOTICE_CHARS)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One number per clinic version; text is never edited in place."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "version"), name="workflows_notice_version"
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_notice_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="workflows_notice_positive"
            ),
        ]


class ReleaseDelivery(TenantScopedModel):
    """One release's notice to its exact audience; content-free provenance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    release = models.OneToOneField(
        "orders.ResultRelease", on_delete=models.PROTECT, related_name="+"
    )
    version = models.ForeignKey(
        "orders.ResultVersion", on_delete=models.PROTECT, related_name="+"
    )
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    audience = models.CharField(max_length=8)
    delegate_grant = models.ForeignKey(
        "identity.PatientDelegateGrant",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    template = models.ForeignKey(
        ReleaseNoticeTemplate, on_delete=models.PROTECT, related_name="+"
    )
    message = models.OneToOneField(
        "comms.Message", null=True, on_delete=models.PROTECT, related_name="+"
    )
    failure_reason = models.CharField(max_length=16, blank=True, default="")
    failure_task = models.OneToOneField(
        "workflows.Task", null=True, on_delete=models.PROTECT, related_name="+"
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """The audience shape matches the release it delivers."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_delivery_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(audience="patient", delegate_grant__isnull=True)
                | models.Q(audience="delegate", delegate_grant__isnull=False),
                name="workflows_delivery_audience",
            ),
            models.CheckConstraint(
                condition=models.Q(failure_reason__in=FAILURES),
                name="workflows_delivery_failure",
            ),
        ]
