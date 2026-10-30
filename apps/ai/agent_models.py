"""Versioned agent authority and encrypted provenance, separate from metering."""

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class AgentRecord(TenantScopedModel):
    """Common identity for retained, exact-clinic agent records."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Every concrete record has a composite scope key."""

        abstract = True
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="%(app_label)s_%(class)s_scope",
            ),
        ]


class AgentPolicyVersion(AgentRecord):
    """An admin's immutable opt-in; changes, including disabling, append a version."""

    capability = models.CharField(max_length=24)
    version = models.PositiveIntegerField()
    autonomy_ceiling = models.CharField(max_length=2)
    tool_allowlist = models.JSONField()
    budget_actions = models.PositiveIntegerField()
    budget_micros = models.PositiveBigIntegerField()
    timeout_seconds = models.PositiveSmallIntegerField()
    escalation_queue = models.CharField(max_length=24)
    compensation_policy = models.JSONField()
    enabled = models.BooleanField(default=False)
    principal = models.ForeignKey(
        "identity.ServicePrincipal",
        null=True,
        on_delete=models.PROTECT,
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta(AgentRecord.Meta):
        """A version cannot be overwritten or published twice."""

        abstract = False
        constraints: ClassVar = [
            *AgentRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("clinic", "capability", "version"),
                name="ai_agent_policy_version",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1)
                & models.Q(autonomy_ceiling__in=("A0", "A1", "A2", "A3"))
                & models.Q(capability__in=("front_desk", "analyst"))
                & models.Q(budget_actions__gte=1, budget_actions__lte=1000)
                & models.Q(budget_micros__lte=10**15)
                & models.Q(timeout_seconds__gte=1, timeout_seconds__lte=60)
                & models.Q(escalation_queue="administrative"),
                name="ai_agent_policy_values",
            ),
        ]


class AgentRun(AgentRecord):
    """AI-10 provenance; contract data is protected and never chain-of-thought."""

    policy = models.ForeignKey(AgentPolicyVersion, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    principal = models.ForeignKey(
        "identity.ServicePrincipal",
        null=True,
        on_delete=models.PROTECT,
    )
    configuration = models.ForeignKey("ai.ModelConfiguration", on_delete=models.PROTECT)
    invocation = models.ForeignKey(
        "ai.AIInvocation",
        null=True,
        on_delete=models.PROTECT,
    )
    prompt_sha256 = models.CharField(max_length=64)
    trigger = models.CharField(max_length=16, default="manual")
    contract = EncryptedJSONField(purpose="ai.agent_run.contract.v1")
    reviewer_edits = EncryptedJSONField(purpose="ai.agent_run.edits.v1", null=True)
    evaluation_set = models.CharField(max_length=64)
    outcome = models.CharField(max_length=24, default="proposal")
    reason_code = models.CharField(max_length=32, blank=True)
    cost_micros = models.PositiveBigIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)


class ActionProposal(AgentRecord):
    """Immutable canonical JSON and digest; only the state machine may advance."""

    run = models.OneToOneField(AgentRun, on_delete=models.PROTECT)
    payload = EncryptedTextField(purpose="ai.action_proposal.payload.v1")
    digest = models.CharField(max_length=64, unique=True)
    idempotency_key = models.UUIDField()
    state = models.CharField(max_length=24, default="proposal")
    reason_code = models.CharField(max_length=32, blank=True)

    class Meta(AgentRecord.Meta):
        """One action key per clinic; changed terms never reuse its approval."""

        abstract = False
        constraints: ClassVar = [
            *AgentRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"),
                name="ai_action_proposal_key",
            ),
        ]


class Approval(AgentRecord):
    """A server attestation of one exact human decision, not a qualified signature."""

    proposal = models.ForeignKey(ActionProposal, on_delete=models.PROTECT)
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    authority_snapshot = models.JSONField()
    step_up_evidence_ref = models.UUIDField()
    step_up_evidence = models.JSONField()
    key_id = models.CharField(max_length=32, default="agent-attestation-v1")
    attestation = models.CharField(max_length=64)

    class Meta(AgentRecord.Meta):
        """Concurrent approvals by one person contribute exactly one vote."""

        abstract = False
        constraints: ClassVar = [
            *AgentRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("proposal", "approver"),
                name="ai_action_approval_once",
            ),
        ]
