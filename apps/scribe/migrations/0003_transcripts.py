"""Add protected transcript history; irreversible, rollback requires restore."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.scribe.migrations._transcript_sql import SQL
from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.migration_operations import AddConstraint

_PROTECT = django.db.models.deletion.PROTECT
_CASCADE = django.db.models.deletion.CASCADE


class Migration(migrations.Migration):
    """Install FORCE RLS, exact grants and immutable guards before runtime use."""

    atomic = False
    dependencies: ClassVar = [
        ("scribe", "0002_ai_disclosure_capture"),
        ("ai", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar = [
        migrations.CreateModel(
            name="ChunkTranscription",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "session",
                    models.ForeignKey(on_delete=_PROTECT, to="scribe.recordingsession"),
                ),
                (
                    "chunk",
                    models.OneToOneField(on_delete=_PROTECT, to="scribe.audiochunk"),
                ),
                (
                    "consent",
                    models.ForeignKey(
                        on_delete=_PROTECT, to="consent.consentacceptance"
                    ),
                ),
                (
                    "invocation",
                    models.OneToOneField(on_delete=_PROTECT, to="ai.aiinvocation"),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="TranscriptVersion",
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
                ("revision", models.PositiveIntegerField()),
                ("state", models.CharField(max_length=8)),
                ("mutation", models.CharField(max_length=24)),
                ("coverage", models.JSONField()),
                ("incomplete_acknowledged", models.BooleanField(default=False)),
                ("recognition_index", models.PositiveSmallIntegerField(null=True)),
                ("latency_ms", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "write_transaction_id",
                    models.BigIntegerField(default=0, editable=False),
                ),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "session",
                    models.ForeignKey(on_delete=_PROTECT, to="scribe.recordingsession"),
                ),
                (
                    "created_by",
                    models.ForeignKey(on_delete=_PROTECT, to=settings.AUTH_USER_MODEL),
                ),
                (
                    "request",
                    models.ForeignKey(
                        null=True, on_delete=_PROTECT, to="scribe.chunktranscription"
                    ),
                ),
                (
                    "predecessor",
                    models.OneToOneField(
                        null=True,
                        on_delete=_PROTECT,
                        related_name="successor",
                        to="scribe.transcriptversion",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="TranscriptSegment",
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
                ("source_id", models.UUIDField()),
                ("start_ms", models.PositiveIntegerField()),
                ("end_ms", models.PositiveIntegerField()),
                ("text", EncryptedTextField(purpose="scribe.transcript.text")),
                ("speaker", models.CharField(default="unknown", max_length=16)),
                ("confidence_milli", models.PositiveSmallIntegerField(null=True)),
                (
                    "uncertainty",
                    EncryptedJSONField(purpose="scribe.transcript.uncertainty"),
                ),
                ("language", models.CharField(default="pt-BR", max_length=35)),
                ("speaker_reviewed", models.BooleanField(default=False)),
                ("text_reviewed", models.BooleanField(default=False)),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "session",
                    models.ForeignKey(on_delete=_PROTECT, to="scribe.recordingsession"),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=_PROTECT,
                        related_name="segments",
                        to="scribe.transcriptversion",
                    ),
                ),
                (
                    "chunk",
                    models.ForeignKey(on_delete=_PROTECT, to="scribe.audiochunk"),
                ),
            ],
            options={"abstract": False},
        ),
        AddConstraint(
            model_name="transcriptversion",
            constraint=models.UniqueConstraint(
                fields=("session", "revision"), name="scribe_transcript_revision"
            ),
        ),
        AddConstraint(
            model_name="transcriptversion",
            constraint=models.UniqueConstraint(
                fields=("request", "recognition_index"), name="scribe_recognition_event"
            ),
        ),
        AddConstraint(
            model_name="transcriptversion",
            constraint=models.CheckConstraint(
                condition=models.Q(state__in=("partial", "final")),
                name="scribe_transcript_state",
            ),
        ),
        AddConstraint(
            model_name="transcriptsegment",
            constraint=models.UniqueConstraint(
                fields=("version", "source_id"), name="scribe_transcript_segment"
            ),
        ),
        AddConstraint(
            model_name="transcriptsegment",
            constraint=models.CheckConstraint(
                condition=models.Q(end_ms__gt=models.F("start_ms")),
                name="scribe_transcript_time",
            ),
        ),
        AddConstraint(
            model_name="transcriptsegment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    speaker__in=("clinician", "patient", "caregiver", "unknown")
                ),
                name="scribe_transcript_speaker",
            ),
        ),
        AddConstraint(
            model_name="transcriptsegment",
            constraint=models.CheckConstraint(
                condition=models.Q(confidence_milli__isnull=True)
                | models.Q(confidence_milli__lte=1000),
                name="scribe_transcript_confidence",
            ),
        ),
        migrations.RunSQL(SQL),
    ]
