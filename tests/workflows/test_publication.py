"""A publication command cannot accidentally create successor versions on retry."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.db import tenant_context
from apps.workflows.models import WorkflowDefinitionVersion
from apps.workflows.services import WorkflowConflictError, publish_definition

from patient_service_support import runtime_role
from workflows.test_engine import publish, task_step

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_publication_replay_binds_actor_key_and_steps(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    publish(graph, [task_step()])
    key = uuid4()
    steps = [{"handler": "timer", "seconds": 0}]
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        first = publish_definition(
            clinic_id=graph.clinic_a, key="replay", steps=steps, idempotency_key=key
        )
        replay = publish_definition(
            clinic_id=graph.clinic_a, key="replay", steps=steps, idempotency_key=key
        )
        assert replay.pk == first.pk
        assert replay.version == first.version == 1
        with pytest.raises(WorkflowConflictError):
            publish_definition(
                clinic_id=graph.clinic_a,
                key="replay",
                steps=[{"handler": "timer", "seconds": 1}],
                idempotency_key=key,
            )
        assert WorkflowDefinitionVersion.objects.filter(key="replay").count() == 1
