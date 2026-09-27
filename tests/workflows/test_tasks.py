"""Todo 26: task authority, legal transitions and atomic evidence."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    assign_task,
    complete_task,
    create_task,
    start_task,
)
from django.db import DatabaseError, transaction
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def spec(graph: RbacGraph) -> TaskSpec:
    return TaskSpec(
        kind="checklist",
        subject_ref={"kind": "clinic", "id": str(graph.clinic_a)},
        due_at=timezone.now() + timedelta(hours=1),
    )


def test_create_assign_start_complete_is_idempotent(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    key = uuid4()
    terms = spec(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(clinic_id=graph.clinic_a, spec=terms, idempotency_key=key)
        assert task.state == "open"
        assert (
            create_task(clinic_id=graph.clinic_a, spec=terms, idempotency_key=key).pk
            == task.pk
        )
        task = assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=task.revision,
        )
        assert task.state == "assigned"
        task = start_task(
            clinic_id=graph.clinic_a, task_id=task.pk, expected_revision=task.revision
        )
        assert task.state == "in_progress"
        task = complete_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            evidence={"checked": True},
            expected_revision=task.revision,
        )
        assert task.state == "done"
        assert task.completion_evidence == {"checked": True}
        with pytest.raises(DatabaseError), transaction.atomic():
            Task.objects.filter(pk=task.pk).update(state="open")
        with pytest.raises(DatabaseError), transaction.atomic():
            Task.objects.filter(pk=task.pk).delete()


def test_foreign_and_unknown_task_have_identical_denial(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
    denials = []
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        for task_id in (task.pk, uuid4()):
            with pytest.raises(CurrentActorError) as caught:
                complete_task(
                    clinic_id=graph.clinic_b,
                    task_id=task_id,
                    evidence={"checked": True},
                    expected_revision=1,
                )
            denials.append((type(caught.value), caught.value.args))
    assert denials[0] == denials[1]


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"checked": False},
        {"checked": True, "note": "SINTETICO-SENTINELA-PHI"},
        {"checked": "true"},
    ],
)
def test_completion_evidence_is_closed_and_refusal_is_atomic(
    rbac_graph: RbacGraph, evidence: dict[str, object]
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        task = assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=task.revision,
        )
        task = start_task(
            clinic_id=graph.clinic_a, task_id=task.pk, expected_revision=task.revision
        )
        with pytest.raises(WorkflowInputError):
            complete_task(
                clinic_id=graph.clinic_a,
                task_id=task.pk,
                evidence=evidence,
                expected_revision=task.revision,
            )
        task.refresh_from_db()
        assert task.state == "in_progress"
        assert task.completion_evidence == {}


def test_inactive_actor_is_refused_with_valid_tenant_and_subject(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        task = assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=task.revision,
        )
    User.objects.filter(pk=graph.physician).update(is_active=False)
    with permission_context(graph, graph.physician), pytest.raises(CurrentActorError):
        start_task(
            clinic_id=graph.clinic_a, task_id=task.pk, expected_revision=task.revision
        )
    with owner_context(graph.organization_a):
        task.refresh_from_db()
        assert task.state == "assigned"


def test_stale_revision_does_not_overwrite_assignment(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        task = assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=task.revision,
        )
        with pytest.raises(WorkflowConflictError):
            start_task(
                clinic_id=graph.clinic_a,
                task_id=task.pk,
                expected_revision=task.revision - 1,
            )
        task.refresh_from_db()
        assert task.state == "assigned"


def test_command_replay_is_bound_to_actor_and_terms(rbac_graph: RbacGraph) -> None:
    """A retried command returns its result only to the same actor and terms."""
    graph = rbac_graph
    first, _enrollment = permission_actor(graph, "clinic_manager")
    second, _enrollment = permission_actor(graph, "clinic_manager")
    with runtime_role(), tenant_context(first, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        assigned = assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=first),
            expected_revision=1,
        )
        replayed = assign_task(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=first),
            expected_revision=1,
        )
        assert replayed.revision == assigned.revision == 2
        with pytest.raises(WorkflowConflictError):
            assign_task(
                clinic_id=graph.clinic_a,
                task_id=task.pk,
                owner=TaskOwner(user_id=second),
                expected_revision=1,
            )
    with runtime_role(), tenant_context(second, graph.organization_a):
        with pytest.raises(WorkflowConflictError):
            assign_task(
                clinic_id=graph.clinic_a,
                task_id=task.pk,
                owner=TaskOwner(user_id=first),
                expected_revision=1,
            )
        assert Task.objects.get(pk=task.pk).revision == 2
