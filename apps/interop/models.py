"""Partner integration, organization export and RNDS submission records.

No partner or export row holds clinical text. ``RndsSubmission`` keeps the
exact submitted FHIR bundle only as a tenant envelope.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.tenancy.fields import EncryptedBytesField
from apps.tenancy.models import TenantScopedModel

# Closed scope vocabulary of external API v1 (docs/api/external-v1.yaml).
SCOPES: Final = (
    "appointments:read",
    "availability:read",
    "invoices.status:read",
    "patients.demographics:read",
    "webhooks:manage",
)
# Closed webhook event vocabulary and the read scope each event requires.
EVENT_SCOPES: Final = {
    "appointment.created": "appointments:read",
    "appointment.status_changed": "appointments:read",
    "appointment.rescheduled": "appointments:read",
    "invoice.status_changed": "invoices.status:read",
}
EVENT_TYPES: Final = tuple(EVENT_SCOPES)
CLIENT_STATES: Final = ("active", "revoked")
SUBSCRIPTION_STATES: Final = ("active", "revoked")
EXPORT_STATES: Final = ("requested", "building", "ready", "failed", "expired")
ATTEMPT_OUTCOMES: Final = ("delivered", "http_error", "timeout", "network_error")
RNDS_STATES: Final = ("pending", "accepted", "rejected", "uncertain")


class ApiClient(TenantScopedModel):
    """One partner credential holder bound to exactly one clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    scopes = ArrayField(models.CharField(max_length=32))
    rate_limit_per_minute = models.PositiveIntegerField()
    state = models.CharField(max_length=16, default="active")
    revision = models.PositiveIntegerField(default=1)
    # The org admin whose current authority every client call and delivery needs.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    revocation_reason = models.CharField(max_length=16, default="", blank=True)

    def __str__(self) -> str:
        """Return identifiers only; the display name may be partner text."""
        return f"{self.pk}:{self.state}"


class ApiClientSecret(TenantScopedModel):
    """One SHA-256 secret digest version; the secret itself is shown once."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(ApiClient, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    secret_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    # NULL while current; a rotated version keeps a bounded overlap window.
    expires_at = models.DateTimeField(null=True)

    class Meta:
        """One row per client secret version."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("client", "version"), name="interop_secret_version"
            ),
        ]


class ApiAccessToken(TenantScopedModel):
    """One short-lived bearer token digest and the scopes it carries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(ApiClient, on_delete=models.PROTECT)
    token_digest = models.CharField(max_length=64, unique=True)
    scopes = ArrayField(models.CharField(max_length=32))
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()


class ApiWriteReceipt(TenantScopedModel):
    """The immutable receipt and idempotency record of one partner write."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(ApiClient, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=64)
    operation = models.CharField(max_length=48)
    request_digest = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    subject_id = models.UUIDField(null=True)
    created_at = models.DateTimeField()

    class Meta:
        """An idempotency key names exactly one write per client."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("client", "idempotency_key"), name="interop_write_key"
            ),
        ]


class WebhookSubscription(TenantScopedModel):
    """One partner endpoint for a closed event set; the URL carries no query."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    client = models.ForeignKey(ApiClient, on_delete=models.PROTECT)
    url = models.CharField(max_length=2048)
    events = ArrayField(models.CharField(max_length=48))
    state = models.CharField(max_length=16, default="active")
    revision = models.PositiveIntegerField(default=1)
    secret_version = models.PositiveIntegerField(default=1)
    previous_secret_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    revocation_reason = models.CharField(max_length=16, default="", blank=True)


class WebhookDelivery(TenantScopedModel):
    """One captured event for one subscription; state derives from its operation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    subscription = models.ForeignKey(WebhookSubscription, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=48)
    subject_id = models.UUIDField()
    subject_state = models.CharField(max_length=32)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    occurred_at = models.DateTimeField()
    operation_id = models.UUIDField(unique=True)
    payload_digest = models.CharField(max_length=64)


class WebhookAttempt(TenantScopedModel):
    """One send attempt in the delivery log: outcome and HTTP status only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(WebhookDelivery, on_delete=models.PROTECT)
    attempt = models.PositiveSmallIntegerField()
    outcome = models.CharField(max_length=16)
    http_status = models.PositiveSmallIntegerField(null=True)
    recorded_at = models.DateTimeField()

    class Meta:
        """An attempt number is recorded once per delivery."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("delivery", "attempt"), name="interop_attempt_number"
            ),
        ]


class OrganizationExport(TenantScopedModel):
    """One organization-wide export job and its archive receipt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The selector clinic of the request; audit events attribute to it.
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=16, default="requested")
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)
    part_count = models.PositiveIntegerField(default=0)
    archive_bytes = models.PositiveBigIntegerField(default=0)
    archive_sha256 = models.CharField(max_length=64, default="", blank=True)
    manifest_sha256 = models.CharField(max_length=64, default="", blank=True)
    dataset_count = models.PositiveIntegerField(default=0)
    record_count = models.PositiveBigIntegerField(default=0)
    attachment_count = models.PositiveIntegerField(default=0)
    failure_reason = models.CharField(max_length=32, default="", blank=True)


class RndsSubmission(TenantScopedModel):
    """One laboratory-result bundle sent to the RNDS homologation environment.

    The id is also the bundle id and its identifier value. ``state`` is the RNDS
    answer: ``pending`` while an outbox operation is live, then ``accepted``
    with the RNDS identifier, ``rejected`` with a definitive validation code, or
    ``uncertain`` when no definitive answer came. An uncertain bundle may be at
    RNDS, so it keeps the result version until a resend of the same bundle
    settles it; ``command_id`` names the command that armed the last send.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    result_version = models.ForeignKey(
        "orders.ResultVersion", on_delete=models.PROTECT, related_name="+"
    )
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # The provider lifecycle version, and so the owner approval, it was sent under.
    capability_version = models.ForeignKey(
        "providers.CapabilityVersion", on_delete=models.PROTECT, related_name="+"
    )
    command_id = models.UUIDField()
    request_digest = models.CharField(max_length=64)
    environment = models.CharField(max_length=16, default="homologation")
    profile = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="pending")
    revision = models.PositiveIntegerField(default=1)
    bundle = EncryptedBytesField(purpose="interop.rndssubmission.bundle")
    bundle_sha256 = models.CharField(max_length=64)
    rnds_identifier = models.CharField(max_length=128, default="", blank=True)
    answer_code = models.CharField(max_length=32, default="", blank=True)
    created_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True)

    class Meta:
        """One command per clinic; one unreleased bundle per result version."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "command_id"), name="interop_rnds_command"
            ),
            models.UniqueConstraint(
                fields=("result_version",),
                condition=models.Q(state__in=("pending", "accepted", "uncertain")),
                name="interop_rnds_live_version",
            ),
        ]

    def __str__(self) -> str:
        """Return identifiers and state only; the bundle is clinical data."""
        return f"{self.pk}:{self.state}"


class OrganizationExportPart(TenantScopedModel):
    """One encrypted byte range of a ready archive; nulled in place on expiry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    export = models.ForeignKey(OrganizationExport, on_delete=models.PROTECT)
    part = models.PositiveIntegerField()
    data = EncryptedBytesField(purpose="interop.organizationexportpart.data", null=True)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    created_at = models.DateTimeField()

    class Meta:
        """Parts are numbered once per export."""

        constraints: ClassVar = [
            models.UniqueConstraint(fields=("export", "part"), name="interop_part"),
        ]
