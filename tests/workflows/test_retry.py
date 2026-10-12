"""Explicit local retry preserves pins, commands, fences and stored outcomes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows import engine
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.models import Task, WorkflowRun, WorkflowStep
from apps.workflows.services import (
    WorkflowConflictError,
    publish_definition,
    retry_run,
    start_run,
)
from django.urls import reverse

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_retention import staff_client
from workflows.permission_support import permission_actor
from workflows.test_authority import _pending_writes
from workflows.test_engine import task_step

if TYPE_CHECKING:
    from datetime import datetime

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def failed_local_run(
    graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> tuple[WorkflowRun, WorkflowStep]:
    with owner_context(graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_MANAGER,
            defaults={"workflow_bundle_version": 2},
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        definition = publish_definition(
            clinic_id=graph.clinic_a,
            key="safe-local-retry",
            steps=[task_step()],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[{"kind": "clinic", "id": str(graph.clinic_a)}],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)

    def missing_effect(_step: WorkflowStep, _now: datetime) -> Literal["completed"]:
        return "completed"

    with monkeypatch.context() as broken, runtime_role():
        broken.setitem(engine.HANDLERS, "task", missing_effect)
        assert engine.execute_step(step_id=step.pk) == "failed"
    with owner_context(graph.organization_a):
        run.refresh_from_db()
        step.refresh_from_db()
        assert step.error_code == "handler_rejected"
        assert Task.objects.count() == 0
    return run, step


def test_retry_cas_owner_replay_and_reentry_preserve_one_effect(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    run, step = failed_local_run(graph, monkeypatch)
    other, _enrollment = permission_actor(graph, "clinic_manager")
    key = uuid4()
    with (
        runtime_role(),
        tenant_context(other, graph.organization_a),
        pytest.raises(WorkflowAccessDeniedError),
    ):
        retry_run(
            clinic_id=graph.clinic_a,
            run_id=run.pk,
            expected_revision=run.revision,
            idempotency_key=key,
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        before = _pending_writes()
        with pytest.raises(WorkflowConflictError):
            retry_run(
                clinic_id=graph.clinic_a,
                run_id=run.pk,
                expected_revision=run.revision - 1,
                idempotency_key=key,
            )
        assert _pending_writes() == before
        retried = retry_run(
            clinic_id=graph.clinic_a,
            run_id=run.pk,
            expected_revision=run.revision,
            idempotency_key=key,
        )
        after = _pending_writes()
        replay = retry_run(
            clinic_id=graph.clinic_a,
            run_id=run.pk,
            expected_revision=run.revision,
            idempotency_key=key,
        )
        assert replay.revision == retried.revision == run.revision + 1
        assert _pending_writes() == after
        assert retried.definition_version_id == run.definition_version_id
        step.refresh_from_db()
        assert step.state == "pending"
        assert step.fencing_token == 1
    with runtime_role():
        assert engine.execute_step(step_id=step.pk) == "waiting"
    with owner_context(graph.organization_a):
        step.refresh_from_db()
        assert step.fencing_token == 2
        assert Task.objects.count() == 1
        assert step.created_task_id is not None


def test_native_exception_retry_has_real_cas_and_idempotent_result(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    run, _step = failed_local_run(graph, monkeypatch)
    url = reverse("workflows:run-exceptions", args=(graph.clinic_a,))
    body = {
        "action": "retry",
        "run_id": str(run.pk),
        "expected_revision": str(run.revision),
        "idempotency_key": str(uuid4()),
    }
    with staff_client(graph.clinic_admin) as client:
        assert client.session["active_org_id"] == str(graph.organization_a)
        response = client.get(url)
        assert response.status_code == 200
        assert f'data-run="{run.pk}"'.encode() in response.content
        assert (
            client.post(
                url, {**body, "expected_revision": str(run.revision - 1)}
            ).status_code
            == 409
        )
        for _ in range(2):
            retried = client.post(url, body)
            assert retried.status_code == 302
            assert retried.headers["Location"] == url
        with tenant_context(graph.clinic_admin, graph.organization_a):
            run.refresh_from_db()
            assert run.state == "pending"
            assert run.revision == 4
            assert Task.objects.count() == 0
