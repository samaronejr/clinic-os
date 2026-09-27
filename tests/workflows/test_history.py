"""Dependencies, idempotent transitions and protected append-only evidence."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.db import tenant_context
from apps.workflows.models import TaskComment
from apps.workflows.services import (
    TaskOwner,
    WorkflowConflictError,
    add_comment,
    assign_task,
    complete_task,
    create_task,
    start_task,
)
from django.db import DatabaseError, connection, transaction

from patient_service_support import runtime_role
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_dependencies_block_until_evidence_is_committed(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        parent = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        child = create_task(
            clinic_id=graph.clinic_a,
            spec=replace(spec(graph), depends_on=parent.pk),
            idempotency_key=uuid4(),
        )
        parent = assign_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=1,
        )
        child = assign_task(
            clinic_id=graph.clinic_a,
            task_id=child.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=1,
        )
        with pytest.raises(WorkflowConflictError):
            start_task(clinic_id=graph.clinic_a, task_id=child.pk, expected_revision=2)
        parent = start_task(
            clinic_id=graph.clinic_a, task_id=parent.pk, expected_revision=2
        )
        parent = complete_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            evidence={"checked": True},
            expected_revision=3,
        )
        replay = complete_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            evidence={"checked": True},
            expected_revision=3,
        )
        assert replay.revision == parent.revision == 4
        assert replay.last_command_key == parent.last_command_key
        child = start_task(
            clinic_id=graph.clinic_a, task_id=child.pk, expected_revision=2
        )
        assert child.state == "in_progress"


def test_comments_are_encrypted_immutable_and_absent_from_audit(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    sentinel = "SINTETICO-SENTINELA-COMMENT"
    key = uuid4()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
        comment = add_comment(
            clinic_id=graph.clinic_a,
            task_id=task.pk,
            body=sentinel,
            idempotency_key=key,
        )
        assert (
            add_comment(
                clinic_id=graph.clinic_a,
                task_id=task.pk,
                body=sentinel,
                idempotency_key=key,
            ).pk
            == comment.pk
        )
        comment.refresh_from_db()
        assert comment.body == sentinel
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM clinic_app.workflows_taskcomment WHERE id=%s",
                [comment.pk],
            )
            row = cursor.fetchone()
            assert row is not None
            assert sentinel.encode() not in bytes(row[0])
            cursor.execute(
                "SELECT payload FROM clinic_app.audit_event_tenant "
                "WHERE event_type LIKE 'workflows.%'"
            )
            payloads = [json.loads(row[0]) for row in cursor.fetchall()]
        assert payloads
        assert all(sentinel not in str(payload) for payload in payloads)
        assert all(set(payload) == {"clinic_id", "object_verb"} for payload in payloads)
        with pytest.raises(DatabaseError), transaction.atomic():
            TaskComment.objects.filter(pk=comment.pk).update(body="Sintetico changed")
        with pytest.raises(DatabaseError), transaction.atomic():
            TaskComment.objects.filter(pk=comment.pk).delete()
