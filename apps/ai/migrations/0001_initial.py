"""Create encrypted gateway storage; irreversible, rollback requires restore."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields
from apps.ai.migrations import (
    _configuration_sql,
    _ledger_sql,
    _platform_sql,
    _policy_sql,
)
from apps.ai.migrations._state_batch import SchemaStateBatch

_PURPOSES = (
    "scribe_asr",
    "scribe_draft",
    "brief",
    "extraction",
    "front_desk",
    "analyst",
    "decision_support",
)
_PROTECT = django.db.models.deletion.PROTECT
_CASCADE = django.db.models.deletion.CASCADE


class Migration(migrations.Migration):
    """Install exact posture in the first migration, not a later hardening pass."""

    initial = True
    atomic = False
    dependencies: ClassVar = [
        ("comms", "0006_action_operations"),
        ("identity", "0039_ai_policy"),
        ("intake", "0014_delegate_sessions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    _schema_operations: ClassVar = [
        migrations.CreateModel(
            name="AIArtifact",
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
                ("purpose", models.CharField(max_length=24)),
                ("method", models.CharField(default="complete", max_length=12)),
                ("kind", models.CharField(default="input", max_length=8)),
                (
                    "content",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="ai.artifact.content.v1"
                    ),
                ),
                ("retention_class", models.CharField(max_length=24)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("clinic", models.ForeignKey(on_delete=_PROTECT, to="identity.clinic")),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "patient_enrollment",
                    models.ForeignKey(
                        null=True,
                        on_delete=_PROTECT,
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(on_delete=_PROTECT, to=settings.AUTH_USER_MODEL),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AIInvocation",
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
                ("purpose", models.CharField(max_length=24)),
                ("config_version", models.PositiveIntegerField()),
                ("prompt_sha256", models.CharField(max_length=64)),
                ("tokens_in", models.PositiveIntegerField(default=0)),
                ("tokens_out", models.PositiveIntegerField(default=0)),
                ("tokens_cached", models.PositiveIntegerField(default=0)),
                ("audio_seconds", models.PositiveIntegerField(default=0)),
                ("limit_tokens_in", models.PositiveIntegerField()),
                ("limit_tokens_out", models.PositiveIntegerField()),
                ("limit_audio_seconds", models.PositiveIntegerField()),
                ("reserved_micros", models.PositiveBigIntegerField()),
                ("cost_micros", models.PositiveBigIntegerField(default=0)),
                ("cost_minor", models.PositiveBigIntegerField(default=0)),
                ("latency_ms", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(default="reserved", max_length=12)),
                ("reason_code", models.CharField(blank=True, max_length=32)),
                ("settled_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("clinic", models.ForeignKey(on_delete=_PROTECT, to="identity.clinic")),
                (
                    "inputs_ref",
                    models.OneToOneField(
                        on_delete=_PROTECT,
                        related_name="requested_invocation",
                        to="ai.aiartifact",
                    ),
                ),
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
                    "user",
                    models.ForeignKey(on_delete=_PROTECT, to=settings.AUTH_USER_MODEL),
                ),
            ],
        ),
        migrations.AddField(
            model_name="aiartifact",
            name="invocation",
            field=models.OneToOneField(
                null=True,
                on_delete=_PROTECT,
                related_name="output",
                to="ai.aiinvocation",
            ),
        ),
        migrations.CreateModel(
            name="GatewaySwitch",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, primary_key=True, serialize=False
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("approved_regions", models.JSONField(default=list)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(id=1), name="ai_switch_singleton"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="ModelConfiguration",
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
                    "purpose",
                    models.CharField(
                        choices=[(p, p) for p in _PURPOSES], max_length=24
                    ),
                ),
                ("capability_key", models.CharField(max_length=64)),
                ("provider", models.CharField(max_length=32)),
                ("model_id", models.CharField(max_length=128)),
                ("region", models.CharField(max_length=64)),
                ("jurisdiction_class", models.CharField(max_length=8)),
                ("endpoint", models.URLField(max_length=255)),
                ("retention_terms", models.TextField()),
                ("training_terms", models.TextField()),
                ("input_micros", models.PositiveIntegerField(default=0)),
                ("output_micros", models.PositiveIntegerField(default=0)),
                ("cached_micros", models.PositiveIntegerField(default=0)),
                ("audio_micros", models.PositiveIntegerField(default=0)),
                ("version", models.PositiveIntegerField()),
                ("approval_ref", models.CharField(blank=True, max_length=128)),
                (
                    "route",
                    models.CharField(
                        choices=[("primary", "Primary"), ("fallback", "Fallback")],
                        max_length=8,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            (s, s) for s in ("draft", "approved", "active", "retired")
                        ],
                        default="draft",
                        max_length=8,
                    ),
                ),
                ("timeout_seconds", models.PositiveSmallIntegerField(default=30)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("purpose", "version"),
                        name="ai_configuration_version_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("purpose",),
                        condition=models.Q(state="active", route="primary"),
                        name="ai_configuration_primary_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(purpose__in=_PURPOSES)
                        & models.Q(state__in=("draft", "approved", "active", "retired"))
                        & models.Q(route__in=("primary", "fallback"))
                        & models.Q(capability_key__in=("asr", "llm_inference"))
                        & models.Q(provider__in=("synthetic", "recorded_fixture"))
                        & models.Q(version__gte=1)
                        & models.Q(timeout_seconds__gte=1, timeout_seconds__lte=60)
                        & models.Q(input_micros__lte=1_000_000)
                        & models.Q(output_micros__lte=1_000_000)
                        & models.Q(cached_micros__lte=1_000_000)
                        & models.Q(audio_micros__lte=1_000_000),
                        name="ai_configuration_values",
                    ),
                ]
            },
        ),
        migrations.AddField(
            model_name="aiinvocation",
            name="configuration",
            field=models.ForeignKey(on_delete=_PROTECT, to="ai.modelconfiguration"),
        ),
        migrations.CreateModel(
            name="AICircuit",
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
                ("opened_at", models.DateTimeField()),
                ("closed_at", models.DateTimeField(null=True)),
                (
                    "organization",
                    models.ForeignKey(on_delete=_CASCADE, to="identity.organization"),
                ),
                (
                    "configuration",
                    models.ForeignKey(on_delete=_PROTECT, to="ai.modelconfiguration"),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="aiartifact",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_artifact_scope_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="aiartifact",
            constraint=models.CheckConstraint(
                condition=models.Q(purpose__in=_PURPOSES)
                & models.Q(method__in=("complete", "stream", "transcribe", "extract"))
                & (
                    models.Q(
                        kind="input",
                        invocation__isnull=True,
                        retention_class="transient_input",
                    )
                    | models.Q(
                        kind="output",
                        invocation__isnull=False,
                        retention_class="review_draft",
                    )
                ),
                name="ai_artifact_values",
            ),
        ),
        migrations.AddIndex(
            model_name="aiinvocation",
            index=models.Index(
                fields=("organization", "created_at", "purpose"),
                name="ai_budget_lookup",
            ),
        ),
        migrations.AddConstraint(
            model_name="aiinvocation",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_invocation_scope_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="aiinvocation",
            constraint=models.CheckConstraint(
                condition=models.Q(purpose__in=_PURPOSES)
                & models.Q(status__in=("reserved", "succeeded", "failed"))
                & models.Q(tokens_cached__lte=models.F("tokens_in"))
                & models.Q(tokens_in__lte=models.F("limit_tokens_in"))
                & models.Q(tokens_out__lte=models.F("limit_tokens_out"))
                & models.Q(audio_seconds__lte=models.F("limit_audio_seconds"))
                & models.Q(cost_micros__lte=models.F("reserved_micros"))
                & models.Q(limit_tokens_in__lte=1_000_000)
                & models.Q(limit_tokens_out__lte=1_000_000)
                & models.Q(limit_audio_seconds__lte=1_000_000)
                & models.Q(reserved_micros__lte=10**15),
                name="ai_invocation_values",
            ),
        ),
        migrations.AddConstraint(
            model_name="aicircuit",
            constraint=models.UniqueConstraint(
                fields=("organization", "configuration"),
                condition=models.Q(closed_at__isnull=True),
                name="ai_circuit_config_uniq",
            ),
        ),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
        migrations.RunSQL(
            "BEGIN;\n"
            + _policy_sql.SQL
            + _ledger_sql.SQL
            + _platform_sql.SQL
            + _configuration_sql.SQL
            + "\nCOMMIT;",
            reverse_sql=None,
        ),
    ]
