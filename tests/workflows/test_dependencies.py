"""Eligibility depends on stored history, not predecessor row visibility."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from apps.workflows.services import (
    TaskOwner,
    assign_task,
    complete_task,
    create_task,
    start_task,
)
from django.db import DatabaseError, transaction
from django.utils import timezone

from patient_service_support import runtime_role
from workflows.permission_support import permission_actor
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_completed_hidden_predecessor_allows_its_current_child_owner(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a manager finishes a predecessor hidden from the child's owner.
    graph = rbac_graph
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    with runtime_role(), tenant_context(manager, graph.organization_a):
        parent = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        child = create_task(
            clinic_id=graph.clinic_a,
            spec=replace(spec(graph), depends_on=parent.pk),
            idempotency_key=uuid4(),
        )
        assign_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            owner=TaskOwner(user_id=manager),
            expected_revision=1,
        )
        start_task(clinic_id=graph.clinic_a, task_id=parent.pk, expected_revision=2)
        complete_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            evidence={"checked": True},
            expected_revision=3,
        )
        assign_task(
            clinic_id=graph.clinic_a,
            task_id=child.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=1,
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not Task.objects.filter(pk=parent.pk).exists()
        # When: the eligible current owner starts the dependent task.
        started = start_task(
            clinic_id=graph.clinic_a, task_id=child.pk, expected_revision=2
        )
        # Then: its actual stored transition is accepted.
        assert started.state == "in_progress"


def test_runtime_sql_cannot_create_a_self_dependency(rbac_graph: RbacGraph) -> None:
    # Given: a valid same-clinic actor and a new task identifier.
    graph = rbac_graph
    identifier = uuid4()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # When: raw runtime SQL attempts a self-cycle that a foreign key allows.
        with pytest.raises(DatabaseError), transaction.atomic():
            Task.objects.create(
                id=identifier,
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                created_by_id=graph.physician,
                kind="checklist",
                subject_ref={"kind": "clinic", "id": str(graph.clinic_a)},
                due_at=timezone.now(),
                depends_on_id=identifier,
                idempotency_key=uuid4(),
                fingerprint="0" * 64,
            )
        # Then: no cyclic task survives the refusal.
        assert not Task.objects.filter(pk=identifier).exists()
