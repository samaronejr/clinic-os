"""Frozen model-state operations for the agent execution ledger."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField

_PROTECT = models.PROTECT


def _base() -> list[tuple[str, models.Field[object, object]]]:
    return [
        (
            "id",
            models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("clinic", models.ForeignKey("identity.clinic", on_delete=_PROTECT)),
        (
            "organization",
            models.ForeignKey("identity.organization", on_delete=models.CASCADE),
        ),
    ]


def _scope(name: str) -> migrations.AddConstraint:
    return migrations.AddConstraint(
        model_name=name,
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"),
            name=f"ai_{name}_scope",
        ),
    )


OPERATIONS = [
    migrations.CreateModel(
        name="AgentPolicyVersion",
        fields=[
            *_base(),
            ("capability", models.CharField(max_length=24)),
            ("version", models.PositiveIntegerField()),
            ("autonomy_ceiling", models.CharField(max_length=2)),
            ("tool_allowlist", models.JSONField()),
            ("budget_actions", models.PositiveIntegerField()),
            ("budget_micros", models.PositiveBigIntegerField()),
            ("timeout_seconds", models.PositiveSmallIntegerField()),
            ("escalation_queue", models.CharField(max_length=24)),
            ("compensation_policy", models.JSONField()),
            ("enabled", models.BooleanField(default=False)),
            (
                "principal",
                models.ForeignKey(
                    "identity.serviceprincipal", null=True, on_delete=_PROTECT
                ),
            ),
            (
                "created_by",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=_PROTECT),
            ),
        ],
        options={"abstract": False},
    ),
    migrations.CreateModel(
        name="AgentRun",
        fields=[
            *_base(),
            ("policy", models.ForeignKey("ai.agentpolicyversion", on_delete=_PROTECT)),
            ("user", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=_PROTECT)),
            (
                "principal",
                models.ForeignKey(
                    "identity.serviceprincipal", null=True, on_delete=_PROTECT
                ),
            ),
            (
                "configuration",
                models.ForeignKey("ai.modelconfiguration", on_delete=_PROTECT),
            ),
            (
                "invocation",
                models.ForeignKey("ai.aiinvocation", null=True, on_delete=_PROTECT),
            ),
            ("prompt_sha256", models.CharField(max_length=64)),
            ("trigger", models.CharField(max_length=16, default="manual")),
            ("contract", EncryptedJSONField(purpose="ai.agent_run.contract.v1")),
            (
                "reviewer_edits",
                EncryptedJSONField(purpose="ai.agent_run.edits.v1", null=True),
            ),
            ("evaluation_set", models.CharField(max_length=64)),
            ("outcome", models.CharField(max_length=24, default="proposal")),
            ("reason_code", models.CharField(max_length=32, blank=True)),
            ("cost_micros", models.PositiveBigIntegerField(default=0)),
            ("latency_ms", models.PositiveIntegerField(default=0)),
        ],
        options={"abstract": False},
    ),
    migrations.CreateModel(
        name="ActionProposal",
        fields=[
            *_base(),
            ("run", models.OneToOneField("ai.agentrun", on_delete=_PROTECT)),
            ("payload", EncryptedTextField(purpose="ai.action_proposal.payload.v1")),
            ("digest", models.CharField(max_length=64, unique=True)),
            ("idempotency_key", models.UUIDField()),
            ("state", models.CharField(max_length=24, default="proposal")),
            ("reason_code", models.CharField(max_length=32, blank=True)),
        ],
        options={"abstract": False},
    ),
    migrations.CreateModel(
        name="Approval",
        fields=[
            *_base(),
            ("proposal", models.ForeignKey("ai.actionproposal", on_delete=_PROTECT)),
            (
                "approver",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=_PROTECT),
            ),
            ("digest", models.CharField(max_length=64)),
            ("authority_snapshot", models.JSONField()),
            ("step_up_evidence_ref", models.UUIDField()),
            ("step_up_evidence", models.JSONField()),
            ("key_id", models.CharField(max_length=32, default="agent-attestation-v1")),
            ("attestation", models.CharField(max_length=64)),
        ],
        options={"abstract": False},
    ),
    migrations.CreateModel(
        name="ActionExecution",
        fields=[
            *_base(),
            ("proposal", models.OneToOneField("ai.actionproposal", on_delete=_PROTECT)),
            ("digest", models.CharField(max_length=64, unique=True)),
            ("fencing_token", models.PositiveBigIntegerField(default=1)),
            ("provider_key", models.UUIDField(unique=True)),
            ("state", models.CharField(max_length=24, default="executing")),
            ("intent_at", models.DateTimeField()),
            ("send_started_at", models.DateTimeField(null=True)),
            ("deadline", models.DateTimeField()),
            ("reserved_micros", models.PositiveBigIntegerField(default=0)),
            ("reason_code", models.CharField(max_length=32, blank=True)),
        ],
        options={"abstract": False},
    ),
    migrations.CreateModel(
        name="ActionReceipt",
        fields=[
            *_base(),
            ("execution", models.ForeignKey("ai.actionexecution", on_delete=_PROTECT)),
            ("kind", models.CharField(max_length=16)),
            ("outcome", models.CharField(max_length=16)),
            ("provider_reference", models.UUIDField()),
            ("digest", models.CharField(max_length=64)),
            ("fencing_token", models.PositiveBigIntegerField()),
            ("key_id", models.CharField(max_length=32, default="agent-attestation-v1")),
            ("attestation", models.CharField(max_length=64)),
        ],
        options={"abstract": False},
    ),
    migrations.CreateModel(
        name="Compensation",
        fields=[
            *_base(),
            (
                "execution",
                models.OneToOneField("ai.actionexecution", on_delete=_PROTECT),
            ),
            ("provider_key", models.UUIDField(unique=True)),
            ("policy_version", models.PositiveIntegerField(default=1)),
            ("state", models.CharField(max_length=24, default="compensating")),
            ("reason_code", models.CharField(max_length=32, blank=True)),
        ],
        options={"abstract": False},
    ),
    *(
        _scope(name)
        for name in (
            "agentpolicyversion",
            "agentrun",
            "actionproposal",
            "approval",
            "actionexecution",
            "actionreceipt",
            "compensation",
        )
    ),
    migrations.AddConstraint(
        model_name="agentpolicyversion",
        constraint=models.UniqueConstraint(
            fields=("clinic", "capability", "version"),
            name="ai_agent_policy_version",
        ),
    ),
    migrations.AddConstraint(
        model_name="agentpolicyversion",
        constraint=models.CheckConstraint(
            condition=models.Q(version__gte=1)
            & models.Q(autonomy_ceiling__in=("A0", "A1", "A2", "A3"))
            & models.Q(capability__in=("front_desk", "analyst"))
            & models.Q(budget_actions__gte=1, budget_actions__lte=1000)
            & models.Q(budget_micros__lte=10**15)
            & models.Q(timeout_seconds__gte=1, timeout_seconds__lte=60)
            & models.Q(escalation_queue="administrative"),
            name="ai_agent_policy_values",
        ),
    ),
    migrations.AddConstraint(
        model_name="actionproposal",
        constraint=models.UniqueConstraint(
            fields=("clinic", "idempotency_key"),
            name="ai_action_proposal_key",
        ),
    ),
    migrations.AddConstraint(
        model_name="approval",
        constraint=models.UniqueConstraint(
            fields=("proposal", "approver"),
            name="ai_action_approval_once",
        ),
    ),
    migrations.AddConstraint(
        model_name="actionreceipt",
        constraint=models.UniqueConstraint(
            fields=("execution", "kind"),
            name="ai_action_receipt_once",
        ),
    ),
    migrations.AddConstraint(
        model_name="actionreceipt",
        constraint=models.CheckConstraint(
            condition=models.Q(kind__in=("execution", "reconciliation", "compensation"))
            & models.Q(outcome__in=("succeeded", "failed", "unknown", "cancelled")),
            name="ai_action_receipt_values",
        ),
    ),
]
