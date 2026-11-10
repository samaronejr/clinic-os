"""Partner API, signed webhooks and organization export: additive schema.

One FORCE-RLS batch, then the frozen grants, guards, resolvers and triggers.
Additive only: the reverse drops this app's objects on an empty synthetic
database; once partner or export history exists, retain it and correct forward.
"""

import uuid
from typing import ClassVar

import django.contrib.postgres.fields
import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import apps.tenancy.fields
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.interop.migrations import _export_sql, _interop_sql


def _install_foreign_keys(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Create the deferred foreign keys before policies and triggers reference them."""
    del apps
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()


class Migration(migrations.Migration):
    """Install the complete additive interop boundary."""

    # The governed import schema (interop.0001) first; then only what this
    # migration's DDL needs at apply time: foreign-key targets,
    # the four tables with capture triggers, the twenty export grants and the
    # resolver column grants the reverse restores. Runtime readers reach every
    # other app's tables only after the full graph is migrated.
    dependencies: ClassVar = [
        ("interop", "0001_import_framework"),
        ("billing", "0014_merge_sandbox_psp_fiscal_documents"),
        ("comms", "0011_agent_replies"),
        ("consent", "0005_delegate_purposes"),
        ("identity", "0050_totp_secret_boundary"),
        ("intake", "0029_membership_payer"),
        ("prescription", "0015_sncr_issuance"),
        ("scheduling", "0011_insurer_membership"),
        ("scribe", "0005_note_drafts"),
        ("subscriptions", "0001_initial"),
        ("teleconsult", "0007_consented_capture"),
        ("tenancy", "0007_blind_indexes"),
        ("workflows", "0009_metrics_refresh"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    schema_operations: ClassVar = [
        migrations.CreateModel(
            name="ApiClient",
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
                ("name", models.CharField(max_length=80)),
                (
                    "scopes",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32), size=None
                    ),
                ),
                ("rate_limit_per_minute", models.PositiveIntegerField()),
                ("state", models.CharField(default="active", max_length=16)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(null=True)),
                (
                    "revocation_reason",
                    models.CharField(blank=True, default="", max_length=16),
                ),
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
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ApiAccessToken",
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
                ("token_digest", models.CharField(max_length=64, unique=True)),
                (
                    "scopes",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32), size=None
                    ),
                ),
                ("issued_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.apiclient",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="OrganizationExport",
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
                ("state", models.CharField(default="requested", max_length=16)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField()),
                ("started_at", models.DateTimeField(null=True)),
                ("finished_at", models.DateTimeField(null=True)),
                ("expires_at", models.DateTimeField(null=True)),
                ("part_count", models.PositiveIntegerField(default=0)),
                ("archive_bytes", models.PositiveBigIntegerField(default=0)),
                (
                    "archive_sha256",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "manifest_sha256",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("dataset_count", models.PositiveIntegerField(default=0)),
                ("record_count", models.PositiveBigIntegerField(default=0)),
                ("attachment_count", models.PositiveIntegerField(default=0)),
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=32),
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
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="WebhookSubscription",
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
                ("url", models.CharField(max_length=2048)),
                (
                    "events",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=48), size=None
                    ),
                ),
                ("state", models.CharField(default="active", max_length=16)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("secret_version", models.PositiveIntegerField(default=1)),
                ("previous_secret_until", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(null=True)),
                (
                    "revocation_reason",
                    models.CharField(blank=True, default="", max_length=16),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.apiclient",
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
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="WebhookDelivery",
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
                ("event_type", models.CharField(max_length=48)),
                ("subject_id", models.UUIDField()),
                ("subject_state", models.CharField(max_length=32)),
                ("starts_at", models.DateTimeField(null=True)),
                ("ends_at", models.DateTimeField(null=True)),
                ("occurred_at", models.DateTimeField()),
                ("operation_id", models.UUIDField(unique=True)),
                ("payload_digest", models.CharField(max_length=64)),
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
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.webhooksubscription",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ApiClientSecret",
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
                ("secret_digest", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField(null=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.apiclient",
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
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("client", "version"), name="interop_secret_version"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ApiWriteReceipt",
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
                ("idempotency_key", models.CharField(max_length=64)),
                ("operation", models.CharField(max_length=48)),
                ("request_digest", models.CharField(max_length=64)),
                ("response_status", models.PositiveSmallIntegerField()),
                ("response_body", models.JSONField()),
                ("subject_id", models.UUIDField(null=True)),
                ("created_at", models.DateTimeField()),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.apiclient",
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
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("client", "idempotency_key"), name="interop_write_key"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="OrganizationExportPart",
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
                ("part", models.PositiveIntegerField()),
                (
                    "data",
                    apps.tenancy.fields.EncryptedBytesField(
                        null=True, purpose="interop.organizationexportpart.data"
                    ),
                ),
                ("sha256", models.CharField(max_length=64)),
                ("size_bytes", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField()),
                (
                    "export",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.organizationexport",
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
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("export", "part"), name="interop_part"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="WebhookAttempt",
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
                ("attempt", models.PositiveSmallIntegerField()),
                ("outcome", models.CharField(max_length=16)),
                ("http_status", models.PositiveSmallIntegerField(null=True)),
                ("recorded_at", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "delivery",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="interop.webhookdelivery",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("delivery", "attempt"), name="interop_attempt_number"
                    )
                ],
            },
        ),
    ]

    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=schema_operations, state_operations=schema_operations
        ),
        migrations.RunPython(_install_foreign_keys, migrations.RunPython.noop),
        migrations.RunSQL(_interop_sql.SQL, _interop_sql.REVERSE),
        migrations.RunSQL(_export_sql.SQL, _export_sql.REVERSE),
    ]
