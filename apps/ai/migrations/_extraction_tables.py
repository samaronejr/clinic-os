"""Frozen encrypted extraction schema operations."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models

from apps.tenancy.fields import EncryptedJSONField


def _base() -> list[tuple[str, models.Field[object, object]]]:
    return [
        (
            "id",
            models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("clinic", models.ForeignKey("identity.clinic", on_delete=models.PROTECT)),
        (
            "organization",
            models.ForeignKey("identity.organization", on_delete=models.CASCADE),
        ),
    ]


OPERATIONS = [
    migrations.CreateModel(
        name="ExtractionJob",
        fields=[
            *_base(),
            (
                "user",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT),
            ),
            (
                "patient_enrollment",
                models.ForeignKey(
                    "intake.patientclinicenrollment", on_delete=models.PROTECT
                ),
            ),
            (
                "invocation",
                models.OneToOneField("ai.aiinvocation", on_delete=models.PROTECT),
            ),
            (
                "result_version",
                models.ForeignKey(
                    "orders.resultversion", null=True, on_delete=models.PROTECT
                ),
            ),
            (
                "attachment",
                models.ForeignKey(
                    "ehr.clinicalattachment", null=True, on_delete=models.PROTECT
                ),
            ),
            ("sha256", models.CharField(max_length=64)),
            ("rotations", models.JSONField()),
        ],
        options={"abstract": False},
    ),
    migrations.AddConstraint(
        model_name="extractionjob",
        constraint=models.CheckConstraint(
            condition=(
                models.Q(result_version__isnull=False, attachment__isnull=True)
                | models.Q(result_version__isnull=True, attachment__isnull=False)
            ),
            name="ai_extraction_source",
        ),
    ),
    migrations.CreateModel(
        name="ExtractionCandidateRecord",
        fields=[
            *_base(),
            (
                "job",
                models.ForeignKey(
                    "ai.extractionjob",
                    on_delete=models.PROTECT,
                    related_name="candidates",
                ),
            ),
            ("position", models.PositiveSmallIntegerField()),
            ("payload", EncryptedJSONField(purpose="ai.extraction.candidate.v1")),
            ("status", models.CharField(max_length=12)),
            (
                "observation",
                models.OneToOneField(
                    "orders.resultobservation", null=True, on_delete=models.PROTECT
                ),
            ),
            (
                "reviewed_by",
                models.ForeignKey(
                    settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
                ),
            ),
            ("reviewed_at", models.DateTimeField(null=True)),
            ("expires_at", models.DateTimeField()),
        ],
        options={"db_table": "ai_extractioncandidate"},
    ),
    migrations.AddConstraint(
        model_name="extractioncandidaterecord",
        constraint=models.UniqueConstraint(
            fields=("job", "position"), name="ai_extraction_position"
        ),
    ),
    migrations.AddConstraint(
        model_name="extractioncandidaterecord",
        constraint=models.CheckConstraint(
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
    ),
]
