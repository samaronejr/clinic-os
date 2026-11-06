"""Add the AI-05 care-coordination purpose and its proposal table.

Additive: one purpose term in four closed vocabularies, three resolver bodies
derived from their released text, and one plain-text-free table with its exact
posture. Reverse is a rehearsal for an empty synthetic database only; once care
proposals exist, rollback = restore.
"""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations import _care_sql
from apps.ai.migrations._state_batch import SchemaStateBatch

_PROTECT = django.db.models.deletion.PROTECT
_PURPOSES = (
    "scribe_asr",
    "scribe_draft",
    "brief",
    "extraction",
    "front_desk",
    "analyst",
    "decision_support",
    "care_coordination",
)
_CARE_KINDS = (
    "return_window",
    "order",
    "referral",
    "questionnaire",
    "monitoring_task",
    "patient_instruction",
    "staff_coordination",
    "unsupported",
)


def _replace(model: str, constraint: models.CheckConstraint) -> list[Operation]:
    return [
        migrations.RemoveConstraint(model_name=model, name=constraint.name),
        migrations.AddConstraint(model_name=model, constraint=constraint),
    ]


_VOCABULARIES = [
    *_replace(
        "agentpolicyversion",
        models.CheckConstraint(
            condition=models.Q(version__gte=1)
            & models.Q(autonomy_ceiling__in=("A0", "A1", "A2", "A3"))
            & models.Q(capability__in=("front_desk", "analyst", "care_coordination"))
            & models.Q(budget_actions__gte=1, budget_actions__lte=1000)
            & models.Q(budget_micros__lte=10**15)
            & models.Q(timeout_seconds__gte=1, timeout_seconds__lte=60)
            & models.Q(escalation_queue="administrative"),
            name="ai_agent_policy_values",
        ),
    ),
    *_replace(
        "aiinvocation",
        models.CheckConstraint(
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
    *_replace(
        "modelconfiguration",
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
    ),
    # 0003 imported the live purpose tuple, so its deployed CHECK predates this
    # purpose on upgraded databases; recreate it from a frozen tuple.
    *_replace(
        "aiartifact",
        models.CheckConstraint(
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
                    retention_class__in=("review_draft", "transcript_partial"),
                )
            ),
            name="ai_artifact_values",
        ),
    ),
    migrations.AlterField(
        model_name="modelconfiguration",
        name="purpose",
        field=models.CharField(choices=[(p, p) for p in _PURPOSES], max_length=24),
    ),
]

_TABLE = [
    migrations.CreateModel(
        name="CareProposal",
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
            ("plan_revision", models.PositiveIntegerField()),
            ("position", models.PositiveSmallIntegerField()),
            ("kind", models.CharField(max_length=24)),
            ("audience", models.CharField(max_length=16)),
            ("window_start", models.DateTimeField(null=True)),
            ("window_end", models.DateTimeField(null=True)),
            ("state", models.CharField(max_length=12)),
            ("reason_code", models.CharField(blank=True, default="", max_length=24)),
            ("decided_at", models.DateTimeField(null=True)),
            ("revision", models.PositiveIntegerField(default=1)),
            (
                "action_proposal",
                models.OneToOneField(
                    null=True,
                    on_delete=_PROTECT,
                    related_name="+",
                    to="ai.actionproposal",
                ),
            ),
            (
                "care_action",
                models.OneToOneField(
                    null=True,
                    on_delete=_PROTECT,
                    related_name="+",
                    to="workflows.careaction",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(on_delete=_PROTECT, to="identity.clinic"),
            ),
            (
                "decided_by",
                models.ForeignKey(
                    null=True,
                    on_delete=_PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "enrollment",
                models.ForeignKey(
                    on_delete=_PROTECT,
                    related_name="+",
                    to="intake.patientclinicenrollment",
                ),
            ),
            (
                "invocation",
                models.ForeignKey(
                    on_delete=_PROTECT, related_name="+", to="ai.aiinvocation"
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
                "plan",
                models.ForeignKey(
                    on_delete=_PROTECT, related_name="+", to="workflows.careplan"
                ),
            ),
            (
                "requested_by",
                models.ForeignKey(
                    on_delete=_PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "source_version",
                models.ForeignKey(
                    on_delete=_PROTECT,
                    related_name="+",
                    to="ehr.clinicaldocumentversion",
                ),
            ),
        ],
        options={"db_table": "ai_careproposal"},
    ),
    migrations.AddIndex(
        model_name="careproposal",
        index=models.Index(
            fields=["clinic", "plan", "state"], name="ai_care_plan_state"
        ),
    ),
    migrations.AddConstraint(
        model_name="careproposal",
        constraint=models.UniqueConstraint(
            fields=("invocation", "position"), name="ai_care_position"
        ),
    ),
    migrations.AddConstraint(
        model_name="careproposal",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="ai_care_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="careproposal",
        constraint=models.CheckConstraint(
            condition=models.Q(kind__in=_CARE_KINDS)
            & models.Q(audience__in=("administrative", "clinical"))
            & models.Q(
                state__in=(
                    "pending",
                    "refused",
                    "approved",
                    "accepted",
                    "rejected",
                    "stale",
                )
            )
            & models.Q(
                reason_code__in=(
                    "",
                    "kind_forbidden",
                    "window_invalid",
                    "malformed",
                    "duplicate",
                    "source_superseded",
                )
            ),
            name="ai_care_vocabulary",
        ),
    ),
    migrations.AddConstraint(
        model_name="careproposal",
        constraint=models.CheckConstraint(
            condition=(models.Q(state="refused") & ~models.Q(reason_code=""))
            | models.Q(
                state="stale",
                reason_code="source_superseded",
                window_start__isnull=False,
                window_end__isnull=False,
                window_end__gt=models.F("window_start"),
            )
            | models.Q(
                state__in=("pending", "approved", "accepted", "rejected"),
                reason_code="",
                window_start__isnull=False,
                window_end__isnull=False,
                window_end__gt=models.F("window_start"),
            ),
            name="ai_care_window",
        ),
    ),
    migrations.AddConstraint(
        model_name="careproposal",
        constraint=models.CheckConstraint(
            condition=models.Q(
                state__in=("pending", "refused"),
                decided_by__isnull=True,
                decided_at__isnull=True,
                action_proposal__isnull=True,
                care_action__isnull=True,
            )
            | models.Q(
                state="approved",
                audience="administrative",
                decided_by__isnull=False,
                decided_at__isnull=False,
                action_proposal__isnull=False,
                care_action__isnull=False,
            )
            | models.Q(
                state="accepted",
                audience="clinical",
                decided_by__isnull=False,
                decided_at__isnull=False,
                action_proposal__isnull=True,
                care_action__isnull=False,
            )
            | models.Q(
                state__in=("rejected", "stale"),
                decided_by__isnull=False,
                decided_at__isnull=False,
                action_proposal__isnull=True,
                care_action__isnull=True,
            ),
            name="ai_care_decision",
        ),
    ),
]


class Migration(migrations.Migration):
    """The vocabulary, table and guards land together in one transaction."""

    # Only what the table and SQL reference: the care tables of workflows 0006
    # (and, through them, the note versions and enrollments). Depending on a
    # later leaf would make every rehearsal of that leaf unapply this migration.
    dependencies: ClassVar = [
        ("ai", "0006_agent_staff_authority"),
        ("workflows", "0006_care_plans"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=[
                *_VOCABULARIES,
                *_TABLE,
                migrations.RunSQL(
                    _care_sql.PURPOSE_SQL, reverse_sql=_care_sql.PURPOSE_REVERSE_SQL
                ),
                migrations.RunSQL(_care_sql.SQL, reverse_sql=_care_sql.REVERSE_SQL),
            ],
            state_operations=[*_VOCABULARIES, *_TABLE],
        ),
    ]
