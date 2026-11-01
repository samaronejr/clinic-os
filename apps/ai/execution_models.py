"""Durable execution intent, authenticated outcomes and retained compensation."""

from typing import ClassVar

from django.db import models

from apps.ai.agent_models import AgentRecord


class ActionExecution(AgentRecord):
    """One fenced execution per digest, including unknown and failed attempts."""

    proposal = models.OneToOneField("ai.ActionProposal", on_delete=models.PROTECT)
    digest = models.CharField(max_length=64, unique=True)
    fencing_token = models.PositiveBigIntegerField(default=1)
    provider_key = models.UUIDField(unique=True)
    state = models.CharField(max_length=24, default="executing")
    intent_at = models.DateTimeField()
    send_started_at = models.DateTimeField(null=True)
    deadline = models.DateTimeField()
    reserved_micros = models.PositiveBigIntegerField(default=0)
    reason_code = models.CharField(max_length=32, blank=True)


class ActionReceipt(AgentRecord):
    """Authenticated provider outcome and server attestation with no message body."""

    execution = models.ForeignKey(ActionExecution, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    outcome = models.CharField(max_length=16)
    provider_reference = models.UUIDField()
    digest = models.CharField(max_length=64)
    fencing_token = models.PositiveBigIntegerField()
    key_id = models.CharField(max_length=32, default="agent-attestation-v1")
    attestation = models.CharField(max_length=64)

    class Meta(AgentRecord.Meta):
        """An exact outcome cannot be recorded more than once."""

        abstract = False
        constraints: ClassVar = [
            *AgentRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("execution", "kind"),
                name="ai_action_receipt_once",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=("execution", "reconciliation", "compensation")
                )
                & models.Q(outcome__in=("succeeded", "failed", "unknown", "cancelled")),
                name="ai_action_receipt_values",
            ),
        ]


class Compensation(AgentRecord):
    """A declared compensating command, never an automatic reversal of clinical care."""

    execution = models.OneToOneField(ActionExecution, on_delete=models.PROTECT)
    provider_key = models.UUIDField(unique=True)
    policy_version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=24, default="compensating")
    reason_code = models.CharField(max_length=32, blank=True)
