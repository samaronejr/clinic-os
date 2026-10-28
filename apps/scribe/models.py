"""Retained recording identity, server epochs, immutable slice terms and receipts."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class RecordingSession(TenantScopedModel):
    """A recording never changes encounter, initiator, consent or capability."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    physician = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    consent = models.ForeignKey("consent.ConsentAcceptance", on_delete=models.PROTECT)
    # Provider registry rows are target-seeded during logical recovery, unlike
    # tenant history. Preserve their original version id and immutable terms;
    # a foreign key would make a valid tenant restore depend on fresh seed ids.
    # The insert guard authenticates both against the real registry.
    capability_version_id = models.UUIDField()
    capability_snapshot = models.JSONField(default=dict)
    token_digest = models.CharField(max_length=64)
    creation_key = models.UUIDField()
    state = models.CharField(max_length=24, default="created")
    epoch = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1)
    discarded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    stopped_at = models.DateTimeField(null=True)

    class Meta(TenantScopedModel.Meta):
        """One live capture per encounter; terminal records remain retained."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "physician", "creation_key"),
                name="scribe_creation_receipt",
            ),
            models.UniqueConstraint(
                fields=("encounter",),
                condition=models.Q(
                    state__in=("created", "authorized", "recording", "paused")
                ),
                name="scribe_one_live_recording",
            ),
            models.CheckConstraint(
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
        ]


class CaptureEpoch(TenantScopedModel):
    """A server-issued generation; resumption never reuses a prior generation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        RecordingSession, on_delete=models.PROTECT, related_name="epochs"
    )
    number = models.PositiveIntegerField()
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True)
    reason = models.CharField(max_length=24, default="")
    high_water = models.JSONField(default=dict)
    gaps = models.JSONField(default=dict)

    class Meta(TenantScopedModel.Meta):
        """Numbers belong to exactly one recording and increase monotonically."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("session", "number"), name="scribe_epoch_number"
            ),
        ]


class AudioChunk(TenantScopedModel):
    """Immutable slice terms plus encrypted staging for the after-commit outbox.

    Staging contains only pgcrypto ciphertext. The per-object key is separately
    tenant-enveloped and can be destroyed without retaining a decryptable copy.
    ``expires_at`` is the original upload deadline, not a reconnect timestamp
    and not the later raw-audio retention policy owned by todo 43.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        RecordingSession, on_delete=models.PROTECT, related_name="chunks"
    )
    epoch = models.ForeignKey(
        CaptureEpoch, on_delete=models.PROTECT, related_name="chunks"
    )
    track = models.CharField(max_length=16)
    seq = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    duration_ms = models.PositiveIntegerField()
    codec = models.CharField(max_length=40)
    size_bytes = models.PositiveIntegerField()
    storage_key = models.CharField(max_length=64, unique=True)
    wrapped_key = models.BinaryField(default=bytes)
    encrypted_data = models.BinaryField(default=bytes)
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=16, default="buffered")
    operation = models.OneToOneField(
        "comms.IntegrationOperation", on_delete=models.PROTECT, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """An epoch/track sequence can name only one immutable byte identity."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("session", "epoch", "track", "seq"),
                name="scribe_chunk_identity",
            ),
            models.CheckConstraint(
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
        ]


class RecordingCommand(TenantScopedModel):
    """Append-only control receipt bound to all machine-consumed command terms."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command_id = models.UUIDField()
    session = models.ForeignKey(RecordingSession, on_delete=models.PROTECT)
    action = models.CharField(max_length=16)
    payload_digest = models.CharField(max_length=64)
    result_state = models.CharField(max_length=24)
    result_epoch = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Scope caller command keys to one recording, not hidden tenant rows."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("session", "command_id"), name="scribe_command_receipt"
            ),
        ]


class ChunkTranscription(TenantScopedModel):
    """Immutable binding from accepted audio to one governed AI invocation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RecordingSession, on_delete=models.PROTECT)
    chunk = models.OneToOneField(AudioChunk, on_delete=models.PROTECT)
    consent = models.ForeignKey("consent.ConsentAcceptance", on_delete=models.PROTECT)
    invocation = models.OneToOneField("ai.AIInvocation", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Request provenance is retained even when recognition is unavailable."""

        abstract = False


class TranscriptVersion(TenantScopedModel):
    """Append-only partial/final snapshots; corrections create successors."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RecordingSession, on_delete=models.PROTECT)
    predecessor = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="successor"
    )
    revision = models.PositiveIntegerField()
    state = models.CharField(max_length=8)
    mutation = models.CharField(max_length=24)
    coverage = models.JSONField()
    incomplete_acknowledged = models.BooleanField(default=False)
    request = models.ForeignKey(ChunkTranscription, null=True, on_delete=models.PROTECT)
    recognition_index = models.PositiveSmallIntegerField(null=True)
    latency_ms = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    write_transaction_id = models.BigIntegerField(default=0, editable=False)

    class Meta(TenantScopedModel.Meta):
        """One linear successor chain per recording; finals stay immutable."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("session", "revision"), name="scribe_transcript_revision"
            ),
            models.UniqueConstraint(
                fields=("request", "recognition_index"),
                name="scribe_recognition_event",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=("partial", "final")),
                name="scribe_transcript_state",
            ),
        ]


class TranscriptSegment(TenantScopedModel):
    """Snapshot-local rows retain stable source identities and encrypted text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RecordingSession, on_delete=models.PROTECT)
    version = models.ForeignKey(
        TranscriptVersion, on_delete=models.PROTECT, related_name="segments"
    )
    source_id = models.UUIDField()
    chunk = models.ForeignKey(AudioChunk, on_delete=models.PROTECT)
    start_ms = models.PositiveIntegerField()
    end_ms = models.PositiveIntegerField()
    text = EncryptedTextField(purpose="scribe.transcript.text")
    speaker = models.CharField(max_length=16, default="unknown")
    confidence_milli = models.PositiveSmallIntegerField(null=True)
    uncertainty = EncryptedJSONField(purpose="scribe.transcript.uncertainty")
    language = models.CharField(max_length=35, default="pt-BR")
    speaker_reviewed = models.BooleanField(default=False)
    text_reviewed = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        """A source segment occurs once in each retained transcript snapshot."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("version", "source_id"), name="scribe_transcript_segment"
            ),
            models.CheckConstraint(
                condition=models.Q(end_ms__gt=models.F("start_ms")),
                name="scribe_transcript_time",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    speaker__in=("clinician", "patient", "caregiver", "unknown")
                ),
                name="scribe_transcript_speaker",
            ),
            models.CheckConstraint(
                condition=models.Q(confidence_milli__isnull=True)
                | models.Q(confidence_milli__lte=1000),
                name="scribe_transcript_confidence",
            ),
        ]
