"""Reversible lineage edges and explicit immutable forward-row receipts."""

import uuid

from django.conf import settings
from django.db import migrations, models

from apps.tenancy.fields import EncryptedTextField

ALIAS_OPERATION = migrations.CreateModel(
    name="PatientAlias",
    fields=[
        (
            "id",
            models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
        ("active", models.BooleanField(default=True)),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("deactivated_at", models.DateTimeField(null=True)),
        (
            "deactivation_reason",
            EncryptedTextField(purpose="intake.alias.deactivation_reason", null=True),
        ),
        (
            "organization",
            models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
        ),
        (
            "merged_patient",
            models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="outgoing_aliases",
                to="intake.patient",
            ),
        ),
        (
            "survivor_patient",
            models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="incoming_aliases",
                to="intake.patient",
            ),
        ),
        (
            "merge_decision",
            models.OneToOneField(on_delete=models.PROTECT, to="intake.mergedecision"),
        ),
        (
            "deactivated_by",
            models.ForeignKey(
                on_delete=models.PROTECT, null=True, to=settings.AUTH_USER_MODEL
            ),
        ),
        (
            "deactivation_session",
            models.ForeignKey(
                on_delete=models.PROTECT, null=True, to="identity.sessionrecord"
            ),
        ),
    ],
    options={
        "constraints": [
            models.UniqueConstraint(
                fields=("organization", "merged_patient"),
                condition=models.Q(active=True),
                name="intake_alias_active_patient_uniq",
            ),
            models.CheckConstraint(
                condition=~models.Q(merged_patient=models.F("survivor_patient")),
                name="intake_alias_distinct_patients_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        active=True,
                        deactivated_at__isnull=True,
                        deactivated_by__isnull=True,
                        deactivation_session__isnull=True,
                        deactivation_reason__isnull=True,
                    )
                    | models.Q(
                        active=False,
                        deactivated_at__isnull=False,
                        deactivated_by__isnull=False,
                        deactivation_session__isnull=False,
                        deactivation_reason__isnull=False,
                    )
                ),
                name="intake_alias_deactivation_check",
            ),
        ],
    },
)

TRANSFER_OPERATION = migrations.CreateModel(
    name="PatientMergeTransfer",
    fields=[
        (
            "id",
            models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
        ("kind", models.CharField(max_length=32)),
        ("original_id", models.CharField(max_length=64)),
        ("replacement_id", models.CharField(max_length=64)),
        ("action", models.CharField(max_length=32)),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        (
            "organization",
            models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
        ),
        ("clinic", models.ForeignKey(on_delete=models.PROTECT, to="identity.clinic")),
        (
            "merge_decision",
            models.ForeignKey(on_delete=models.PROTECT, to="intake.mergedecision"),
        ),
    ],
    options={
        "constraints": [
            models.UniqueConstraint(
                fields=("merge_decision", "kind", "original_id"),
                name="intake_merge_transfer_original_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=(
                        "appointment",
                        "waitlist",
                        "contact",
                        "preference",
                        "access_grant",
                    )
                ),
                name="intake_merge_transfer_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    action__in=("recreated", "kept_survivor", "revoked_and_reissued")
                ),
                name="intake_merge_transfer_action_check",
            ),
        ],
    },
)
