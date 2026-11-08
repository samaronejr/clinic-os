"""Add result release notices and the urgent-concern queue; additive.

No table holds patient text, so this is an ordinary reversible migration: the
reverse drops the four tables and their functions on a database that never used
them. Once releases or concerns exist, rollback = restore.
"""

import uuid
from typing import ClassVar

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.workflows.migrations._coordination_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """The four tables, their guard and the patient bridge land together."""

    dependencies: ClassVar = [
        ("comms", "0010_portal_messages"),
        ("identity", "0043_delegate_messages_operation"),
        ("intake", "0028_messages_operation"),
        ("orders", "0005_patient_downloads"),
        ("workflows", "0007_referral_backfill"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    _schema: ClassVar = [
        migrations.CreateModel(
            name="ReleaseNoticeTemplate",
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
                ("version", models.PositiveIntegerField()),
                ("body", models.CharField(max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ReleaseDelivery",
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
                ("audience", models.CharField(max_length=8)),
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=16),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
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
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "failure_task",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="workflows.task",
                    ),
                ),
                (
                    "message",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="comms.message",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "release",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="orders.resultrelease",
                    ),
                ),
                (
                    "released_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="orders.resultversion",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="workflows.releasenoticetemplate",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="UrgentCoveragePolicy",
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
                ("version", models.PositiveIntegerField()),
                (
                    "weekdays",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.PositiveSmallIntegerField(), size=7
                    ),
                ),
                ("opens_at", models.TimeField()),
                ("closes_at", models.TimeField()),
                ("response_hours", models.PositiveSmallIntegerField()),
                (
                    "responsible_roles",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=20), size=4
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="UrgentConcern",
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
                ("state", models.CharField(default="open", max_length=16)),
                ("reported_at", models.DateTimeField()),
                ("acknowledged_at", models.DateTimeField(null=True)),
                ("resolved_at", models.DateTimeField(null=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "acknowledged_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="comms.conversation",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "message",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="comms.message",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="workflows.urgentcoveragepolicy",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="releasenoticetemplate",
            constraint=models.UniqueConstraint(
                fields=("clinic", "version"), name="workflows_notice_version"
            ),
        ),
        migrations.AddConstraint(
            model_name="releasenoticetemplate",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_notice_scope"
            ),
        ),
        migrations.AddConstraint(
            model_name="releasenoticetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="workflows_notice_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="releasedelivery",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_delivery_scope"
            ),
        ),
        migrations.AddConstraint(
            model_name="releasedelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("audience", "patient"), ("delegate_grant__isnull", True)),
                    models.Q(
                        ("audience", "delegate"), ("delegate_grant__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="workflows_delivery_audience",
            ),
        ),
        migrations.AddConstraint(
            model_name="releasedelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("failure_reason__in", ("", "scope_revoked", "send_failed"))
                ),
                name="workflows_delivery_failure",
            ),
        ),
        migrations.AddConstraint(
            model_name="urgentcoveragepolicy",
            constraint=models.UniqueConstraint(
                fields=("clinic", "version"), name="workflows_coverage_version"
            ),
        ),
        migrations.AddConstraint(
            model_name="urgentcoveragepolicy",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_coverage_scope"
            ),
        ),
        migrations.AddConstraint(
            model_name="urgentcoveragepolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("version__gte", 1),
                    ("closes_at__gt", models.F("opens_at")),
                    ("response_hours__gte", 1),
                    ("response_hours__lte", 72),
                ),
                name="workflows_coverage_terms",
            ),
        ),
        migrations.AddConstraint(
            model_name="urgentconcern",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_concern_scope"
            ),
        ),
        migrations.AddConstraint(
            model_name="urgentconcern",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("acknowledged_by__isnull", True), ("state", "open")),
                    models.Q(
                        ("acknowledged_by__isnull", False),
                        ("resolved_by__isnull", True),
                        ("state", "acknowledged"),
                    ),
                    models.Q(
                        ("acknowledged_by__isnull", False),
                        ("resolved_by__isnull", False),
                        ("state", "resolved"),
                    ),
                    _connector="OR",
                ),
                name="workflows_concern_state",
            ),
        ),
    ]
    # One batch renders the four new models once instead of the related tenant
    # graph for every operation (the migration rendering bounds).
    operations: ClassVar = [
        SchemaStateBatch(database_operations=_schema, state_operations=_schema),
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
