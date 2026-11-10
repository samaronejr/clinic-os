"""Interop records: governed imports, partner integration, exports and RNDS.

Import records (todo 66) are clinic-scoped and FORCE-RLS protected (``rls.py``).
Uploaded bytes, mapping documents, reports and imported content exist only as
tenant envelopes. An import never rewrites history: state changes append a
transition, batches only advance ``pending -> committed`` and a rollback hides
reversible enrollments through ``HiddenEnrollment`` instead of deleting them.

Partner integration and organization export records (todo 67) hold no clinical
text. ``RndsSubmission`` (todo 68) keeps the exact submitted FHIR bundle only as
a tenant envelope.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.tenancy.fields import (
    EncryptedBytesField,
    EncryptedJSONField,
    EncryptedTextField,
)
from apps.tenancy.models import TenantScopedModel

SOURCE_KINDS: Final = ("csv", "xlsx", "zip", "api")
JOB_STATES: Final = (
    "uploaded",
    "profiled",
    "mapped",
    "dry_run",
    "approved",
    "committed",
    "rolled_back",
)
FILE_ROLES: Final = ("source", "attachment")
SCAN_VERDICTS: Final = ("not_applicable", "clean", "rejected")
DATASETS: Final = ("patients", "history")
RECORD_KINDS: Final = ("patient", "note", "observation", "attachment")
BATCH_STATES: Final = ("pending", "committed")
ROLLBACK_REASONS: Final = (
    "abandoned",
    "data_quality",
    "wrong_source",
    "cutover_cancelled",
)
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"


class ImportScopedRecord(TenantScopedModel):
    """Every import fact belongs to one explicit clinic of its organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Concrete records declare their own constraints and policies."""

        abstract = True


class ImportMapping(ImportScopedRecord):
    """One immutable, versioned column mapping; a new version never edits one."""

    code = models.CharField(max_length=40)
    version = models.PositiveIntegerField()
    source_kind = models.CharField(max_length=8)
    document = EncryptedJSONField(purpose="interop.importmapping.document")
    document_sha256 = models.CharField(max_length=64)
    suggested = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        """Versions are dense per clinic mapping code and never rebound."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "code", "version"), name="interop_mapping_version"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1)
                & models.Q(source_kind__in=SOURCE_KINDS)
                & models.Q(code__regex=r"^[a-z0-9][a-z0-9_-]{0,39}$")
                & models.Q(document_sha256__regex=SHA256_PATTERN),
                name="interop_mapping_values",
            ),
        ]


class ImportJob(ImportScopedRecord):
    """The immutable header of one uploaded source; state lives in transitions."""

    source_kind = models.CharField(max_length=8)
    size_bytes = models.PositiveBigIntegerField()
    source_sha256 = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        """One upload command has one effect per clinic."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="interop_job_command"
            ),
            models.CheckConstraint(
                condition=models.Q(source_kind__in=SOURCE_KINDS)
                & models.Q(size_bytes__gte=1)
                & models.Q(source_sha256__regex=SHA256_PATTERN)
                & models.Q(fingerprint__regex=SHA256_PATTERN),
                name="interop_job_values",
            ),
        ]


class ImportFile(ImportScopedRecord):
    """Uploaded bytes: the source itself, or one quarantined archive attachment."""

    job = models.ForeignKey(ImportJob, on_delete=models.PROTECT)
    role = models.CharField(max_length=16)
    path = EncryptedTextField(purpose="interop.importfile.path", null=True)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    media_type = models.CharField(max_length=128)
    scan_verdict = models.CharField(max_length=16)
    scan_reason = models.CharField(max_length=32, blank=True, default="")
    data = EncryptedBytesField(purpose="interop.importfile.data")

    class Meta:
        """A job keeps exactly one source; attachments keep their scan verdict."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("job",),
                condition=models.Q(role="source"),
                name="interop_file_one_source",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=FILE_ROLES)
                & models.Q(scan_verdict__in=SCAN_VERDICTS)
                & models.Q(sha256__regex=SHA256_PATTERN)
                & models.Q(size_bytes__gte=1)
                & (
                    models.Q(role="source", scan_verdict="not_applicable")
                    | models.Q(
                        role="attachment", scan_verdict__in=("clean", "rejected")
                    )
                ),
                name="interop_file_values",
            ),
        ]


class ImportTransition(ImportScopedRecord):
    """One append-only state of the import state machine with its evidence."""

    job = models.ForeignKey(ImportJob, on_delete=models.PROTECT)
    sequence = models.PositiveIntegerField()
    state = models.CharField(max_length=16)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    mapping = models.ForeignKey(ImportMapping, null=True, on_delete=models.PROTECT)
    report = EncryptedJSONField(purpose="interop.importtransition.report", null=True)
    digest = models.CharField(max_length=64, blank=True, default="")
    row_count = models.PositiveIntegerField(default=0)
    valid_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    rollback_window_hours = models.PositiveIntegerField(null=True)
    reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        """Concurrent commands race on one sequence; the loser is a conflict."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("job", "sequence"), name="interop_transition_sequence"
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1)
                & models.Q(state__in=JOB_STATES)
                & (models.Q(digest="") | models.Q(digest__regex=SHA256_PATTERN))
                & (models.Q(reason="") | models.Q(reason__in=ROLLBACK_REASONS)),
                name="interop_transition_values",
            ),
        ]


class ImportBatch(ImportScopedRecord):
    """One bounded unit of commit work; it advances only pending -> committed."""

    job = models.ForeignKey(ImportJob, on_delete=models.PROTECT)
    sequence = models.PositiveIntegerField()
    dataset = models.CharField(max_length=16)
    first_row = models.PositiveIntegerField()
    last_row = models.PositiveIntegerField()
    rows_sha256 = models.CharField(max_length=64)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=16, default="pending")
    committed_count = models.PositiveIntegerField(default=0)
    committed_at = models.DateTimeField(null=True)

    class Meta:
        """Batch identity and row bounds are fixed when the commit starts."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("job", "sequence"), name="interop_batch_sequence"
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1)
                & models.Q(dataset__in=DATASETS)
                & models.Q(state__in=BATCH_STATES)
                & models.Q(first_row__gte=1)
                & models.Q(last_row__gte=models.F("first_row"))
                & models.Q(rows_sha256__regex=SHA256_PATTERN),
                name="interop_batch_values",
            ),
        ]


class ImportedRecord(ImportScopedRecord):
    """Immutable provenance: source ids, original author and time, content hash."""

    job = models.ForeignKey(ImportJob, on_delete=models.PROTECT)
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    dataset = models.CharField(max_length=16)
    row_number = models.PositiveIntegerField()
    kind = models.CharField(max_length=16)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    file = models.ForeignKey(ImportFile, null=True, on_delete=models.PROTECT)
    source_record_id = EncryptedTextField(
        purpose="interop.importedrecord.source_record_id"
    )
    original_author = EncryptedTextField(
        purpose="interop.importedrecord.original_author", null=True
    )
    original_recorded_at = models.DateTimeField(null=True)
    content = EncryptedJSONField(purpose="interop.importedrecord.content", null=True)
    content_sha256 = models.CharField(max_length=64)
    identifier_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        """One source row has one imported effect."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("job", "dataset", "row_number"), name="interop_record_row"
            ),
            models.CheckConstraint(
                condition=models.Q(dataset__in=DATASETS)
                & models.Q(kind__in=RECORD_KINDS)
                & models.Q(row_number__gte=1)
                & models.Q(content_sha256__regex=SHA256_PATTERN)
                & (
                    models.Q(dataset="patients", kind="patient", file__isnull=True)
                    | models.Q(
                        dataset="history",
                        kind__in=("note", "observation"),
                        file__isnull=True,
                    )
                    | models.Q(dataset="history", kind="attachment", file__isnull=False)
                ),
                name="interop_record_values",
            ),
        ]


class HiddenEnrollment(TenantScopedModel):
    """A rolled-back, still-unused imported enrollment, retained but hidden."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment = models.OneToOneField(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="+",
    )
    job = models.ForeignKey(ImportJob, on_delete=models.PROTECT)
    hidden_at = models.DateTimeField()


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
