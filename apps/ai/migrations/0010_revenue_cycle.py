"""Add the default-off AI-06 purpose and its human-only A1 policy vocabulary.

No table or column is added. Finance gains access only to this capability's
proposal history through a scope-bound resolver. Reversing is supported on an
empty synthetic database; retained history requires restore instead of deletion.
"""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations import _revenue_sql

_PURPOSES = (
    "scribe_asr",
    "scribe_draft",
    "brief",
    "extraction",
    "front_desk",
    "analyst",
    "decision_support",
    "care_coordination",
    "revenue_cycle",
)


def _replace(model: str, constraint: models.CheckConstraint) -> list[Operation]:
    return [
        migrations.RemoveConstraint(model_name=model, name=constraint.name),
        migrations.AddConstraint(model_name=model, constraint=constraint),
    ]


class Migration(migrations.Migration):
    """Exact vocabularies, source guards and approval snapshots change atomically."""

    dependencies: ClassVar = [
        ("ai", "0009_merge_care_coordination_front_desk"),
        ("insurance", "0001_revenue_cycle"),
    ]
    operations: ClassVar = [
        *_replace(
            "agentpolicyversion",
            models.CheckConstraint(
                condition=models.Q(version__gte=1)
                & models.Q(autonomy_ceiling__in=("A0", "A1", "A2", "A3"))
                & models.Q(
                    capability__in=(
                        "front_desk",
                        "analyst",
                        "care_coordination",
                        "revenue_cycle",
                    )
                )
                & models.Q(budget_actions__gte=1, budget_actions__lte=1000)
                & models.Q(budget_micros__lte=10**15)
                & models.Q(timeout_seconds__gte=1, timeout_seconds__lte=60)
                & models.Q(escalation_queue="administrative"),
                name="ai_agent_policy_values",
            ),
        ),
        migrations.AddConstraint(
            model_name="agentpolicyversion",
            constraint=models.CheckConstraint(
                condition=~models.Q(capability="revenue_cycle")
                | (
                    models.Q(autonomy_ceiling__in=("A0", "A1"), principal__isnull=True)
                    & models.Q(compensation_policy={"mode": "manual", "version": 1})
                ),
                name="ai_revenue_drafts_only",
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
        migrations.RunSQL(_revenue_sql.SQL, reverse_sql=_revenue_sql.REVERSE_SQL),
    ]
