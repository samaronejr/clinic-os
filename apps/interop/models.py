"""Governed import records: immutable sources, append-only states, provenance.

Every row is clinic-scoped and FORCE-RLS protected (``rls.py``). Uploaded
bytes, mapping documents, reports and imported content exist only as tenant
envelopes. An import never rewrites history: state changes append a
transition, batches only advance ``pending -> committed`` and a rollback hides
reversible enrollments through ``HiddenEnrollment`` instead of deleting them.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
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
