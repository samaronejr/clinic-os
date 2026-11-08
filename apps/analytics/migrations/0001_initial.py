"""Add immutable governed metric definitions, refreshes and human decisions."""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from apps.analytics.migrations._analytics_v1_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Retain report history; rollback after writes requires a restore."""

    initial = True

    dependencies: ClassVar = [
        ("ai", "0009_merge_care_coordination_front_desk"),
        ("identity", "0043_delegate_messages_operation"),
        ("billing", "0011_operational_journal"),
        ("orders", "0005_patient_downloads"),
        ("scribe", "0005_note_drafts"),
        ("workflows", "0008_release_and_urgent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="AnalystReview",
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
                ("descriptor", models.JSONField()),
                ("authorization_version", models.CharField(max_length=64)),
                ("decision", models.CharField(max_length=8)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "invocation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ai.aiinvocation",
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
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="MetricDefinitionVersion",
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
                ("metric_id", models.CharField(max_length=32)),
                ("version", models.PositiveIntegerField(default=1)),
                ("specification", models.JSONField()),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
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
            name="ReadModelRefresh",
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
                ("authorization_version", models.CharField(max_length=64)),
                ("descriptor", models.JSONField()),
                ("command_id", models.UUIDField()),
                ("rows", models.JSONField()),
                ("quality", models.CharField(max_length=16)),
                ("as_of", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="analytics.metricdefinitionversion",
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
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SavedReport",
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
                ("descriptor", models.JSONField()),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="analytics.metricdefinitionversion",
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
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="metricdefinitionversion",
            constraint=models.UniqueConstraint(
                fields=("clinic", "metric_id", "version"),
                name="analytics_definition_version",
            ),
        ),
        migrations.AddIndex(
            model_name="readmodelrefresh",
            index=models.Index(
                fields=["clinic", "user", "definition", "-as_of"],
                name="analytics_refresh_cache",
            ),
        ),
        migrations.AddConstraint(
            model_name="readmodelrefresh",
            constraint=models.UniqueConstraint(
                fields=("clinic", "user", "command_id"),
                name="analytics_refresh_command",
            ),
        ),
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
