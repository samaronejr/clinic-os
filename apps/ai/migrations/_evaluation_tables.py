"""Frozen additive evaluation schema; encrypted observations are restore-only."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models

from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.migration_operations import AddConstraint


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


def _human(*, nullable: bool = False) -> models.ForeignKey[object, object]:
    return models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=nullable,
    )


OPERATIONS = [
    *(
        migrations.AddField(
            model_name="modelconfiguration",
            name=name,
            field=models.CharField(default="", max_length=64),
        )
        for name in ("prompt_sha256", "retrieval_sha256", "schema_sha256")
    ),
    migrations.CreateModel(
        name="EvaluationProtocol",
        fields=[
            *_base(),
            ("capability", models.CharField(max_length=5)),
            ("version", models.PositiveIntegerField()),
            ("corpus_version", models.CharField(max_length=32)),
            ("corpus_sha256", models.CharField(max_length=64)),
            ("case_manifest", models.JSONField()),
            ("thresholds_document", models.TextField()),
            ("thresholds_sha256", models.CharField(max_length=64)),
            ("registered_by", _human()),
            ("clinical_owner_ref", models.CharField(max_length=128)),
        ],
    ),
    AddConstraint(
        model_name="evaluationprotocol",
        constraint=models.UniqueConstraint(
            fields=("clinic", "capability", "version"), name="ai_eval_protocol_version"
        ),
    ),
    migrations.CreateModel(
        name="EvaluationRun",
        fields=[
            *_base(),
            (
                "protocol",
                models.ForeignKey("ai.evaluationprotocol", on_delete=models.PROTECT),
            ),
            (
                "configuration",
                models.ForeignKey("ai.modelconfiguration", on_delete=models.PROTECT),
            ),
            ("configuration_version", models.PositiveIntegerField()),
            ("versions", models.JSONField()),
            ("rater_one", _human()),
            ("rater_two", _human()),
            ("adjudicator", _human()),
            ("created_by", _human()),
            (
                "sample_results",
                EncryptedJSONField(purpose="ai.evaluation.samples.v1", null=True),
            ),
            ("automatic_failures", models.PositiveSmallIntegerField(default=0)),
            ("scored_at", models.DateTimeField(null=True)),
            ("status", models.CharField(default="pending", max_length=8)),
            ("finished_at", models.DateTimeField(null=True)),
        ],
    ),
    migrations.CreateModel(
        name="EvaluationRating",
        fields=[
            *_base(),
            (
                "run",
                models.ForeignKey(
                    "ai.evaluationrun", on_delete=models.PROTECT, related_name="ratings"
                ),
            ),
            ("case_id", models.CharField(max_length=64)),
            ("slot", models.CharField(max_length=16)),
            ("user", _human()),
            ("labels", models.JSONField()),
        ],
    ),
    migrations.AddConstraint(
        model_name="evaluationrating",
        constraint=models.UniqueConstraint(
            fields=("run", "case_id", "slot"), name="ai_eval_rating_slot"
        ),
    ),
    migrations.AddConstraint(
        model_name="evaluationrating",
        constraint=models.UniqueConstraint(
            fields=("run", "case_id", "user"), name="ai_eval_rating_human"
        ),
    ),
    migrations.CreateModel(
        name="ModelChangeRequest",
        fields=[
            *_base(),
            (
                "configuration",
                models.ForeignKey("ai.modelconfiguration", on_delete=models.PROTECT),
            ),
            ("run", models.ForeignKey("ai.evaluationrun", on_delete=models.PROTECT)),
            ("requested_by", _human()),
            ("approved_by", _human(nullable=True)),
            ("status", models.CharField(default="requested", max_length=9)),
            ("approval_ref", models.CharField(blank=True, max_length=128)),
            ("decided_at", models.DateTimeField(null=True)),
        ],
    ),
]
