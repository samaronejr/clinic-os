"""Encrypted, source-bound drafts; only the confirmation resolver can file one."""

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.models import TenantScopedModel


class ExtractionJob(TenantScopedModel):
    """One governed invocation of exact immutable source bytes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    patient_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    invocation = models.OneToOneField("ai.AIInvocation", on_delete=models.PROTECT)
    result_version = models.ForeignKey(
        "orders.ResultVersion", null=True, on_delete=models.PROTECT
    )
    attachment = models.ForeignKey(
        "ehr.ClinicalAttachment", null=True, on_delete=models.PROTECT
    )
    sha256 = models.CharField(max_length=64)
    rotations = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A source is a result version XOR an available encounter attachment."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=(
                    models.Q(result_version__isnull=False, attachment__isnull=True)
                    | models.Q(result_version__isnull=True, attachment__isnull=False)
                ),
                name="ai_extraction_source",
            ),
        ]


class ExtractionCandidateRecord(TenantScopedModel):
    """Immutable encrypted proposal plus one terminal human review decision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    job = models.ForeignKey(
        ExtractionJob, on_delete=models.PROTECT, related_name="candidates"
    )
    position = models.PositiveSmallIntegerField()
    payload = EncryptedJSONField(purpose="ai.extraction.candidate.v1")
    status = models.CharField(max_length=12)
    observation = models.OneToOneField(
        "orders.ResultObservation", null=True, on_delete=models.PROTECT
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    reviewed_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep exact source positions and one durable, non-reusable decision."""

        db_table = "ai_extractioncandidate"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("job", "position"), name="ai_extraction_position"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status__in=("candidate", "ambiguous"),
                        reviewed_by__isnull=True,
                        reviewed_at__isnull=True,
                        observation__isnull=True,
                    )
                    | models.Q(
                        status="rejected",
                        reviewed_by__isnull=False,
                        reviewed_at__isnull=False,
                        observation__isnull=True,
                    )
                    | models.Q(
                        status="confirmed",
                        reviewed_by__isnull=False,
                        reviewed_at__isnull=False,
                        observation__isnull=False,
                    )
                ),
                name="ai_extraction_decision",
            ),
        ]
