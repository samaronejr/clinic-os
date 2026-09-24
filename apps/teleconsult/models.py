"""Scoped teleconsultation sessions bound to stored clinical authority.

One session binds one encounter, its assigned physician, its patient and the
exact consent version that authorized it. Provider rooms are created only
through the committed outbox; room authority derives from the stored session
and operation rows, never from external tenant identifiers. Recording and
transcription stay disabled at the database layer.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.models import TenantScopedModel

SESSION_STATE_VALUES = ("waiting", "active", "ended", "failed")
CREDENTIAL_ROLE_VALUES = ("physician", "patient")
EVENT_KIND_VALUES = (
    "created",
    "joined",
    "join_denied",
    "started",
    "ended",
    "failed",
)


class TeleconsultSession(TenantScopedModel):
    """One consultation session per encounter; terminal states never reopen."""

    class State(models.TextChoices):
        """The complete stored session lifecycle."""

        WAITING = "waiting", "Aguardando"
        ACTIVE = "active", "Em andamento"
        ENDED = "ended", "Encerrada"
        FAILED = "failed", "Falhou"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    encounter = models.OneToOneField("ehr.Encounter", on_delete=models.PROTECT)
    appointment = models.ForeignKey("scheduling.Appointment", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    physician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    consent = models.ForeignKey("consent.ConsentAcceptance", on_delete=models.PROTECT)
    state = models.CharField(max_length=16, choices=State, default=State.WAITING)
    revision = models.PositiveIntegerField(default=1)
    failure_reason = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Keep the lifecycle vocabulary and timestamps coherent."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(state__in=SESSION_STATE_VALUES),
                name="teleconsult_session_state_check",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name="teleconsult_session_revision_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="waiting",
                        started_at__isnull=True,
                        ended_at__isnull=True,
                        failure_reason="",
                    )
                    | models.Q(
                        state="active",
                        started_at__isnull=False,
                        ended_at__isnull=True,
                        failure_reason="",
                    )
                    | models.Q(
                        state="ended",
                        ended_at__isnull=False,
                        failure_reason="",
                    )
                    | models.Q(
                        state="failed",
                        ended_at__isnull=False,
                    )
                    & ~models.Q(failure_reason="")
                ),
                name="teleconsult_session_lifecycle_check",
            ),
        ]


class TeleconsultRoom(TenantScopedModel):
    """The stored idempotent reference for one outbox room-creation operation."""

    operation = models.OneToOneField(
        "comms.IntegrationOperation",
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="teleconsult_room",
    )
    session = models.OneToOneField(
        TeleconsultSession, on_delete=models.PROTECT, related_name="room"
    )
    room_name = models.CharField(max_length=128)
    recording_enabled = models.BooleanField(default=False)
    transcription_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Recording and transcription are disabled for every synthetic room."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    recording_enabled=False, transcription_enabled=False
                ),
                name="teleconsult_room_capture_disabled",
            ),
        ]


class TeleconsultCredential(TenantScopedModel):
    """One short-lived role-scoped join credential; only its digest is stored."""

    class Role(models.TextChoices):
        """Room access is always bound to one exact stored participant."""

        PHYSICIAN = "physician", "Médico"
        PATIENT = "patient", "Paciente"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        TeleconsultSession, on_delete=models.PROTECT, related_name="credentials"
    )
    role = models.CharField(max_length=16, choices=Role)
    participant_id = models.UUIDField()
    token_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    first_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """At most one live credential exists per session and role."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("session", "role"),
                condition=models.Q(revoked_at__isnull=True),
                name="teleconsult_credential_live_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=CREDENTIAL_ROLE_VALUES),
                name="teleconsult_credential_role_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    token_digest__regex=r"^[0-9a-f]{64}$"  # noqa: S106
                ),
                name="teleconsult_credential_digest_check",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="teleconsult_credential_expiry_check",
            ),
        ]


class TeleconsultEvent(TenantScopedModel):
    """Immutable scoped history for one session; metadata only, never content."""

    id = models.BigAutoField(primary_key=True)
    session = models.ForeignKey(
        TeleconsultSession, on_delete=models.PROTECT, related_name="events"
    )
    kind = models.CharField(max_length=16)
    actor_role = models.CharField(max_length=16, blank=True, default="")
    reason_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep the event vocabulary closed."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(kind__in=EVENT_KIND_VALUES),
                name="teleconsult_event_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(actor_role__in=("", *CREDENTIAL_ROLE_VALUES)),
                name="teleconsult_event_actor_check",
            ),
        ]
