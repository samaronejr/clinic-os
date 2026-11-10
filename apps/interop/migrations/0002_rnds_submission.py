"""RNDS laboratory-result submission log: additive schema behind EG-7.

One FORCE-RLS table written only by resolvers, its retained-transition guard
and the outbox settlement trigger. Additive only: the reverse drops this
migration's objects on an empty database; once a submission exists, retain it.
"""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import apps.tenancy.fields
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.interop.migrations import _rnds_sql


def _install_foreign_keys(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Create the deferred foreign keys before the guard and resolvers use them."""
    del apps
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()


class Migration(migrations.Migration):
    """Install the RNDS submission log and its database-enforced lifecycle."""

    # Foreign-key targets, the result and consent tables the request resolver
    # reads, and the outbox table its settlement trigger watches.
    dependencies: ClassVar = [
        ("consent", "0006_rnds_exchange_purpose"),
        ("interop", "0001_external_api_export"),
        ("orders", "0005_patient_downloads"),
        ("providers", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    schema_operations: ClassVar = [
        migrations.CreateModel(
            name="RndsSubmission",
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
                ("command_id", models.UUIDField()),
                ("request_digest", models.CharField(max_length=64)),
                (
                    "environment",
                    models.CharField(default="homologation", max_length=16),
                ),
                ("profile", models.CharField(max_length=64)),
                ("state", models.CharField(default="pending", max_length=16)),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "bundle",
                    apps.tenancy.fields.EncryptedBytesField(
                        purpose="interop.rndssubmission.bundle"
                    ),
                ),
                ("bundle_sha256", models.CharField(max_length=64)),
                (
                    "rnds_identifier",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "answer_code",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                ("created_at", models.DateTimeField()),
                ("decided_at", models.DateTimeField(null=True)),
                (
                    "capability_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="providers.capabilityversion",
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
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="intake.patientclinicenrollment",
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
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "result_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="orders.resultversion",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "command_id"), name="interop_rnds_command"
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("state__in", ("pending", "accepted", "uncertain"))
                        ),
                        fields=("result_version",),
                        name="interop_rnds_live_version",
                    ),
                ],
            },
        ),
    ]

    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=schema_operations, state_operations=schema_operations
        ),
        migrations.RunPython(_install_foreign_keys, migrations.RunPython.noop),
        migrations.RunSQL(_rnds_sql.SQL, _rnds_sql.REVERSE),
    ]
