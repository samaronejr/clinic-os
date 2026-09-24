"""Create scoped teleconsult sessions, rooms, credentials and events."""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Store session bindings, outbox room references and join credentials."""

    initial = True

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0003_video_channel"),
        ("consent", "0002_consent_policy"),
        ("ehr", "0008_finalization_policy"),
        ("identity", "0005_clinic_timezone"),
        (
            "intake",
            "0008_remove_patientaccessgrant_intake_grant_operations_check_and_more",
        ),
        ("scheduling", "0004_waitlistentry_waitlistoffer_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="TeleconsultSession",
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
                    "state",
                    models.CharField(
                        choices=[
                            ("waiting", "Aguardando"),
                            ("active", "Em andamento"),
                            ("ended", "Encerrada"),
                            ("failed", "Falhou"),
                        ],
                        default="waiting",
                        max_length=16,
                    ),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.appointment",
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
                    "consent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="consent.consentacceptance",
                    ),
                ),
                (
                    "encounter",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
                (
                    "physician",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TeleconsultRoom",
            fields=[
                (
                    "operation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        primary_key=True,
                        related_name="teleconsult_room",
                        serialize=False,
                        to="comms.integrationoperation",
                    ),
                ),
                ("room_name", models.CharField(max_length=128)),
                ("recording_enabled", models.BooleanField(default=False)),
                ("transcription_enabled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "session",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="room",
                        to="teleconsult.teleconsultsession",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TeleconsultEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("kind", models.CharField(max_length=16)),
                ("actor_role", models.CharField(blank=True, default="", max_length=16)),
                (
                    "reason_code",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="teleconsult.teleconsultsession",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TeleconsultCredential",
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
                    "role",
                    models.CharField(
                        choices=[("physician", "Médico"), ("patient", "Paciente")],
                        max_length=16,
                    ),
                ),
                ("participant_id", models.UUIDField()),
                ("token_digest", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("first_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="credentials",
                        to="teleconsult.teleconsultsession",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="teleconsultsession",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("state__in", ("waiting", "active", "ended", "failed"))
                ),
                name="teleconsult_session_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultsession",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)),
                name="teleconsult_session_revision_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultsession",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("ended_at__isnull", True),
                        ("failure_reason", ""),
                        ("started_at__isnull", True),
                        ("state", "waiting"),
                    ),
                    models.Q(
                        ("ended_at__isnull", True),
                        ("failure_reason", ""),
                        ("started_at__isnull", False),
                        ("state", "active"),
                    ),
                    models.Q(
                        ("ended_at__isnull", False),
                        ("failure_reason", ""),
                        ("state", "ended"),
                    ),
                    models.Q(
                        ("ended_at__isnull", False),
                        ("state", "failed"),
                        models.Q(("failure_reason", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="teleconsult_session_lifecycle_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultroom",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("recording_enabled", False), ("transcription_enabled", False)
                ),
                name="teleconsult_room_capture_disabled",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        (
                            "created",
                            "joined",
                            "join_denied",
                            "started",
                            "ended",
                            "failed",
                        ),
                    )
                ),
                name="teleconsult_event_kind_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("actor_role__in", ("", "physician", "patient"))),
                name="teleconsult_event_actor_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultcredential",
            constraint=models.UniqueConstraint(
                condition=models.Q(("revoked_at__isnull", True)),
                fields=("session", "role"),
                name="teleconsult_credential_live_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultcredential",
            constraint=models.CheckConstraint(
                condition=models.Q(("role__in", ("physician", "patient"))),
                name="teleconsult_credential_role_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultcredential",
            constraint=models.CheckConstraint(
                condition=models.Q(("token_digest__regex", "^[0-9a-f]{64}$")),
                name="teleconsult_credential_digest_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="teleconsultcredential",
            constraint=models.CheckConstraint(
                condition=models.Q(("expires_at__gt", models.F("created_at"))),
                name="teleconsult_credential_expiry_check",
            ),
        ),
    ]
