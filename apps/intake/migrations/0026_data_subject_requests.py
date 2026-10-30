"""Add retained LGPD data-subject requests with staff SLA review tasks."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.intake.data_request_models import (
    DATA_REQUEST_KIND_VALUES,
    DATA_REQUEST_REJECTION_VALUES,
    DATA_REQUEST_STATE_VALUES,
)
from apps.intake.migrations._data_request_sql import REVERSE_SQL, SQL, TABLE
from apps.tenancy.migrations_support import revoke_default_dml

_CREATE: list[Operation] = [
    migrations.CreateModel(
        name="DataSubjectRequest",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "kind",
                models.CharField(
                    choices=[
                        ("access", "Access"),
                        ("correction", "Correction"),
                        ("portability", "Portability"),
                        ("sharing_information", "Sharing information"),
                        ("objection", "Objection"),
                    ],
                    max_length=24,
                ),
            ),
            ("purpose", models.CharField(blank=True, default="", max_length=32)),
            (
                "details",
                apps.tenancy.fields.EncryptedTextField(
                    blank=True,
                    null=True,
                    purpose="intake.datasubjectrequest.details",
                ),
            ),
            (
                "state",
                models.CharField(
                    choices=[
                        ("received", "Received"),
                        ("in_progress", "In progress"),
                        ("fulfilled", "Fulfilled"),
                        ("rejected", "Rejected"),
                    ],
                    default="received",
                    max_length=16,
                ),
            ),
            (
                "rejection_reason",
                models.CharField(blank=True, default="", max_length=24),
            ),
            ("received_at", models.DateTimeField()),
            ("due_at", models.DateTimeField()),
            ("closed_at", models.DateTimeField(blank=True, null=True)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("idempotency_key", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("updated_at", models.DateTimeField()),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
                ),
            ),
            (
                "patient",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="intake.patient",
                ),
            ),
            (
                "enrollment",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="data_subject_requests",
                    to="intake.patientclinicenrollment",
                ),
            ),
            (
                "requester_enrollment",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="intake.patientclinicenrollment",
                ),
            ),
            (
                "patient_session",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="intake.patientsession",
                ),
            ),
            (
                "delegate_grant",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="identity.patientdelegategrant",
                ),
            ),
            (
                "task",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="workflows.task",
                ),
            ),
            (
                "updated_by",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "idempotency_key"),
                    name="intake_data_request_key_uniq",
                ),
                models.UniqueConstraint(
                    fields=("organization", "clinic", "id"),
                    name="intake_data_request_scope_uniq",
                ),
                models.UniqueConstraint(
                    fields=("task",), name="intake_data_request_task_uniq"
                ),
                models.CheckConstraint(
                    condition=models.Q(kind__in=DATA_REQUEST_KIND_VALUES),
                    name="intake_data_request_kind_check",
                ),
                models.CheckConstraint(
                    condition=models.Q(state__in=DATA_REQUEST_STATE_VALUES),
                    name="intake_data_request_state_check",
                ),
                models.CheckConstraint(
                    condition=(
                        (models.Q(kind="objection") & ~models.Q(purpose=""))
                        | (~models.Q(kind="objection") & models.Q(purpose=""))
                    ),
                    name="intake_data_request_purpose_check",
                ),
                models.CheckConstraint(
                    condition=(
                        models.Q(kind="correction", details__isnull=False)
                        | (
                            ~models.Q(kind="correction")
                            & models.Q(details__isnull=True)
                        )
                    ),
                    name="intake_data_request_details_check",
                ),
                models.CheckConstraint(
                    condition=(
                        models.Q(
                            state="rejected",
                            rejection_reason__in=DATA_REQUEST_REJECTION_VALUES,
                        )
                        | (~models.Q(state="rejected") & models.Q(rejection_reason=""))
                    ),
                    name="intake_data_request_rejection_check",
                ),
                models.CheckConstraint(
                    condition=(
                        models.Q(
                            state__in=("received", "in_progress"),
                            closed_at__isnull=True,
                        )
                        | models.Q(
                            state__in=("fulfilled", "rejected"),
                            closed_at__isnull=False,
                        )
                    ),
                    name="intake_data_request_closed_check",
                ),
                models.CheckConstraint(
                    condition=models.Q(due_at__gt=models.F("received_at")),
                    name="intake_data_request_due_check",
                ),
                models.CheckConstraint(
                    condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                    name="intake_data_request_fingerprint_check",
                ),
                models.CheckConstraint(
                    condition=models.Q(revision__gte=1),
                    name="intake_data_request_revision_check",
                ),
            ],
        },
    ),
]


class Migration(migrations.Migration):
    """Additive: a reverse drops the empty table; recorded requests need a restore."""

    dependencies: ClassVar = [
        ("intake", "0025_survivor_confirmations"),
        ("identity", "0042_privacy_request_permission"),
        ("consent", "0005_delegate_purposes"),
        ("retention", "0005_transient_purge"),
        ("workflows", "0005_result_source_tasks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar[list[Operation]] = [
        SchemaStateBatch(database_operations=_CREATE, state_operations=_CREATE),
        migrations.RunSQL(revoke_default_dml(TABLE, ("SELECT",)) + SQL, REVERSE_SQL),
    ]
