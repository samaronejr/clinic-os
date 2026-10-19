"""Already-encrypted adjudication schema; no plaintext history backfill."""

import uuid

from django.conf import settings
from django.db import migrations, models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField

OPERATION = migrations.CreateModel(
    name="MergeDecision",
    fields=[
        (
            "id",
            models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
        ("decision", models.CharField(max_length=16)),
        ("step_up_verified_at", models.DateTimeField()),
        ("merged_version", models.PositiveIntegerField()),
        ("survivor_version", models.PositiveIntegerField()),
        ("result_version", models.PositiveIntegerField(null=True)),
        ("reason", EncryptedTextField(purpose="intake.merge.reason")),
        ("field_choices", EncryptedJSONField(purpose="intake.merge.field_choices")),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        (
            "organization",
            models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
        ),
        ("clinic", models.ForeignKey(on_delete=models.PROTECT, to="identity.clinic")),
        (
            "candidate",
            models.OneToOneField(
                on_delete=models.PROTECT, to="intake.duplicatecandidate"
            ),
        ),
        (
            "adjudicator",
            models.ForeignKey(on_delete=models.PROTECT, to=settings.AUTH_USER_MODEL),
        ),
        (
            "verified_session",
            models.ForeignKey(on_delete=models.PROTECT, to="identity.sessionrecord"),
        ),
        (
            "merged_patient",
            models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="merge_decisions",
                to="intake.patient",
            ),
        ),
        (
            "survivor_patient",
            models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="survivor_decisions",
                to="intake.patient",
            ),
        ),
    ],
    options={
        "constraints": [
            models.CheckConstraint(
                condition=models.Q(decision__in=("merge", "dismiss")),
                name="intake_merge_decision_kind_check",
            ),
            models.CheckConstraint(
                condition=~models.Q(merged_patient=models.F("survivor_patient")),
                name="intake_merge_distinct_patients_check",
            ),
            models.UniqueConstraint(
                fields=("organization", "id"), name="intake_merge_org_id_uniq"
            ),
        ],
    },
)
