"""AI-06's shared recheck as an observed permission-root delegation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ai.action_contracts import ActionRefusedError
from apps.ai.revenue_cycle import check_revenue_sources

from identity.nonstaff_differential import DifferentialProbe

if TYPE_CHECKING:
    from apps.ai.action_payload import JSON

    from rbac_fixtures import RbacGraph

SYMBOL = "apps.ai.revenue_cycle.check_revenue_sources"


def revenue_probes(graph: RbacGraph) -> list[DifferentialProbe]:
    """require_permission runs first; no census state ever owns the output."""
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

    return [DifferentialProbe(SYMBOL, call, expected=False)]
