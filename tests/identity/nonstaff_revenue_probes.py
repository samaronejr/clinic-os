"""AI-06's shared recheck as an observed permission-root delegation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ai.action_contracts import ActionRefusedError
from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.revenue_cycle import check_revenue_sources
from apps.identity.current_context import CurrentActorError

from identity.nonstaff_differential import DifferentialProbe

if TYPE_CHECKING:
    from apps.ai.action_payload import JSON

    from rbac_fixtures import RbacGraph

SYMBOL = "apps.ai.revenue_cycle.check_revenue_sources"
PUBLISH = "apps.ai.agent_policies.publish_agent_policy"


def revenue_probes(graph: RbacGraph) -> list[DifferentialProbe]:
    """Both reach has_permission first; a foreign output and a stale version refuse."""
    data: dict[str, JSON] = {
        "subject": {
            "kind": "ai_artifact",
            "id": str(uuid4()),
            "expected_state": "output",
            "revision": 1,
        },
        "parameters": {"position": 0},
        "source_versions": [],
        "operation": {"name": "appeal.draft", "version": 1},
    }

    def call() -> bool:
        try:
            check_revenue_sources(clinic_id=graph.clinic_a, data=data)
        except ActionRefusedError:
            return False
        return True

    def publish() -> bool:
        # A stale version refuses even an actor holding both authorities, so
        # every census state reaches has_permission and is refused.
        try:
            publish_agent_policy(
                clinic_id=graph.clinic_a,
                expected_version=2**31,
                terms=PolicyTerms("revenue_cycle", "A0", (), enabled=False),
            )
        except (ActionRefusedError, CurrentActorError):
            return False
        return True

    return [
        DifferentialProbe(SYMBOL, call, expected=False),
        DifferentialProbe(PUBLISH, publish, expected=False),
    ]
