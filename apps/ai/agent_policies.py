"""Admin-only, append-only agent policy publication with closed tool schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import connection, transaction

from apps.ai.action_contracts import (
    AUTONOMY,
    ActionInputError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.models import AgentPolicyVersion
from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.action_payload import JSON

MAX_DAILY_ACTIONS: Final = 1000
MAX_TIMEOUT_SECONDS: Final = 60

TOOL_SCHEMAS: Final[dict[str, JSON]] = {
    "comms.synthetic_reminder": {
        "type": "object",
        "additionalProperties": False,
        "required": ["template"],
        "properties": {"template": {"const": "logistics-v1"}},
    },
    "workflows.review_task": {
        "type": "object",
        "additionalProperties": False,
        "required": ["due_at"],
        "properties": {"due_at": {"type": "string", "format": "date-time"}},
    },
}


@dataclass(frozen=True, slots=True)
class PolicyTerms:
    """Code-registered schemas only; admins may narrow but never invent tools."""

    capability: str
    autonomy_ceiling: str
    tools: tuple[str, ...]
    budget_actions: int = 20
    budget_micros: int = 20
    timeout_seconds: int = 30
    escalation_queue: str = "administrative"
    compensation: str = "manual"
    enabled: bool = False
    principal_id: UUID | None = None

    def __post_init__(self) -> None:
        """Parse policy controls once, including Python boolean/int ambiguity."""
        if (
            self.capability not in ("front_desk", "analyst")
            or self.autonomy_ceiling not in AUTONOMY
            or len(set(self.tools)) != len(self.tools)
            or not set(self.tools) <= TOOL_SCHEMAS.keys()
            or (self.autonomy_ceiling == "A0" and self.tools)
            or (
                self.autonomy_ceiling == "A1"
                and set(self.tools) - {"workflows.review_task"}
            )
            or (self.autonomy_ceiling in ("A2", "A3") and self.principal_id is None)
            or type(self.budget_actions) is not int
            or not 1 <= self.budget_actions <= MAX_DAILY_ACTIONS
            or type(self.budget_micros) is not int
            or not 0 <= self.budget_micros <= 10**15
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
            or self.escalation_queue != "administrative"
            or self.compensation not in ("manual", "synthetic_cancel", "none")
            or type(self.enabled) is not bool
        ):
            raise ActionInputError


def policy_administrator(*, clinic_id: UUID) -> UUID:
    """Use canonical permission bundles, never Django staff flags."""
    try:
        return require_permission("automation.admin", clinic_id=clinic_id)
    except CurrentActorError:
        return require_permission("automation.organization", clinic_id=clinic_id)


@transaction.atomic
def publish_agent_policy(
    *,
    clinic_id: UUID,
    expected_version: int,
    terms: PolicyTerms,
) -> AgentPolicyVersion:
    """Disabling is a successor version, so queued approvals become stale."""
    user = policy_administrator(clinic_id=clinic_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-send-v1',0))"
        )
    previous = (
        AgentPolicyVersion.objects.filter(
            clinic_id=clinic_id,
            capability=terms.capability,
        )
        .order_by("-version")
        .first()
    )
    if expected_version != (previous.version if previous else 0):
        raise ActionRefusedError(Refusal.POLICY)
    clinic = Clinic.objects.get(pk=clinic_id)
    policy = AgentPolicyVersion.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        created_by_id=user,
        capability=terms.capability,
        version=expected_version + 1,
        autonomy_ceiling=terms.autonomy_ceiling,
        tool_allowlist={tool: TOOL_SCHEMAS[tool] for tool in sorted(terms.tools)},
        budget_actions=terms.budget_actions,
        budget_micros=terms.budget_micros,
        timeout_seconds=terms.timeout_seconds,
        escalation_queue=terms.escalation_queue,
        compensation_policy={"mode": terms.compensation, "version": 1},
        enabled=terms.enabled,
        principal_id=terms.principal_id,
    )
    record_phase1_event(
        "ai.agent_policy.published",
        clinic_id=clinic_id,
        affected_record_id=policy.pk,
    )
    return policy
