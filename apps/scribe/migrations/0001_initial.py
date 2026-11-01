"""Create encrypted capture storage; irreversible, rollback requires restore."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.scribe.migrations import _capture_sql
from apps.tenancy.migration_operations import AddConstraint

_PROTECT = django.db.models.deletion.PROTECT
_CASCADE = django.db.models.deletion.CASCADE


class Migration(migrations.Migration):
    """Install posture in the first additive migration, before runtime use."""

    initial = True
    atomic = False
    dependencies: ClassVar = [
        ("comms", "0006_action_operations"),
        ("consent", "0005_delegate_purposes"),
        ("ehr", "0012_addendum_receipt_open_encounter"),
        ("identity", "0039_ai_policy"),
        ("intake", "0014_delegate_sessions"),
        ("providers", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    _schema_operations: ClassVar = [
        migrations.CreateModel(
            name="RecordingSession",
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
                ("token_digest", models.CharField(max_length=64)),
                ("creation_key", models.UUIDField()),
                ("state", models.CharField(default="created", max_length=24)),
                ("epoch", models.PositiveIntegerField(default=0)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("discarded", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("stopped_at", models.DateTimeField(null=True)),
                ("capability_version_id", models.UUIDField()),
                ("capability_snapshot", models.JSONField(default=dict)),
                ("clinic", models.ForeignKey(on_delete=_PROTECT, to="identity.clinic")),
                (
                    "consent",
                    models.ForeignKey(
                        on_delete=_PROTECT, to="consent.consentacceptance"
                    ),
                ),
                (
                    "encounter",
                    models.ForeignKey(on_delete=_PROTECT, to="ehr.encounter"),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=_PROTECT, to="intake.patientclinicenrollment"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "physician",
                    models.ForeignKey(on_delete=_PROTECT, to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="RecordingCommand",
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
                ("action", models.CharField(max_length=16)),
                ("payload_digest", models.CharField(max_length=64)),
                ("result_state", models.CharField(max_length=24)),
                ("result_epoch", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "session",
                    models.ForeignKey(on_delete=_PROTECT, to="scribe.recordingsession"),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="CaptureEpoch",
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
                ("number", models.PositiveIntegerField()),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(null=True)),
                ("reason", models.CharField(default="", max_length=24)),
                ("high_water", models.JSONField(default=dict)),
                ("gaps", models.JSONField(default=dict)),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=_PROTECT,
                        related_name="epochs",
                        to="scribe.recordingsession",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="AudioChunk",
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
                ("track", models.CharField(max_length=16)),
                ("seq", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("duration_ms", models.PositiveIntegerField()),
                ("codec", models.CharField(max_length=40)),
                ("size_bytes", models.PositiveIntegerField()),
                ("storage_key", models.CharField(max_length=64, unique=True)),
                ("wrapped_key", models.BinaryField(default=bytes)),
                ("encrypted_data", models.BinaryField(default=bytes)),
                ("expires_at", models.DateTimeField()),
                ("state", models.CharField(default="buffered", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "operation",
                    models.OneToOneField(
                        null=True, on_delete=_PROTECT, to="comms.integrationoperation"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "epoch",
                    models.ForeignKey(
                        on_delete=_PROTECT,
                        related_name="chunks",
                        to="scribe.captureepoch",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=_PROTECT,
                        related_name="chunks",
                        to="scribe.recordingsession",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        AddConstraint(
            model_name="recordingsession",
            constraint=models.UniqueConstraint(
                fields=("clinic", "physician", "creation_key"),
                name="scribe_creation_receipt",
            ),
        ),
        AddConstraint(
            model_name="recordingcommand",
            constraint=models.UniqueConstraint(
                fields=("session", "command_id"),
                name="scribe_command_receipt",
            ),
        ),
        AddConstraint(
            model_name="recordingsession",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    state__in=("created", "authorized", "recording", "paused")
                ),
                fields=("encounter",),
                name="scribe_one_live_recording",
            ),
        ),
        AddConstraint(
            model_name="recordingsession",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "created",
                        "authorized",
                        "recording",
                        "paused",
                        "stopped",
                        "stopped_revoked",
                    )
                ),
                name="scribe_recording_state",
            ),
        ),
        AddConstraint(
            model_name="captureepoch",
            constraint=models.UniqueConstraint(
                fields=("session", "number"), name="scribe_epoch_number"
            ),
        ),
        AddConstraint(
            model_name="audiochunk",
            constraint=models.UniqueConstraint(
                fields=("session", "epoch", "track", "seq"),
                name="scribe_chunk_identity",
            ),
        ),
        AddConstraint(
            model_name="audiochunk",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "buffered",
                        "accepted",
                        "quarantined",
                        "rejected",
                        "purged",
                    )
                ),
                name="scribe_chunk_state",
            ),
        ),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
        migrations.RunSQL(_capture_sql.SQL),
    ]
