"""Action steps use the real outbox, pinned digests and execute-time authority."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.identity.models import User, UserClinicRole
from apps.workflows import engine, external
from apps.workflows.models import WorkflowStep
from django.utils import timezone

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.test_engine import execute, publish, start, step_id
from workflows.test_worker_authority import remove_permission

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.adapters import SendResult

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_external_action_completes_only_after_outbox_receipt(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    dispatched: list[UUID] = []
    monkeypatch.setattr(integration, "_dispatch", dispatched.append)
    external.register_adapters()
    run = start(
        graph,
        publish(graph, [{"handler": "external", "provider": "workflow-synthetic-v1"}]),
    )
    identifier = step_id(graph, run)
    assert execute(identifier) == "waiting"
    assert len(dispatched) == 1
    with owner_context(graph.organization_a):
        step = WorkflowStep.objects.get(pk=identifier)
        assert step.operation_id is not None
        operation = IntegrationOperation.objects.get(pk=step.operation_id)
        assert operation.pk == dispatched[0]
        assert operation.kind == "action"
        assert operation.channel == ""
        assert operation.idempotency_key == step.pk
        assert len(operation.payload_digest) == 64
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "succeeded"
        assert integration.execute_operation(operation.pk) == "skipped"
    now = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr(engine, "utc_now", lambda: now)
    assert execute(identifier) == "completed"
    assert len(dispatched) == 1
    with owner_context(graph.organization_a):
        operation.refresh_from_db()
        assert operation.attempt_count == 1
        assert operation.provider_reference == f"synthetic:workflow:{operation.pk}"


@pytest.mark.parametrize(
    "revocation",
    ["inactive", "membership", "tasks.view", "tasks.assign", "tasks.reassign"],
)
def test_external_send_is_refused_after_actor_revocation(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    graph = rbac_graph
    dispatched: list[UUID] = []
    monkeypatch.setattr(integration, "_dispatch", dispatched.append)
    external.register_adapters()
    sends: list[UUID] = []
    original = external.SyntheticWorkflowAdapter.send

    def observe(
        self: external.SyntheticWorkflowAdapter, prepared: object, *, operation_id: UUID
    ) -> SendResult:
        sends.append(operation_id)
        return original(self, prepared, operation_id=operation_id)

    monkeypatch.setattr(external.SyntheticWorkflowAdapter, "send", observe)
    run = start(
        graph,
        publish(graph, [{"handler": "external", "provider": "workflow-synthetic-v1"}]),
    )
    identifier = step_id(graph, run)
    assert execute(identifier) == "waiting"
    if revocation == "inactive":
        User.objects.filter(pk=graph.shared_user).update(is_active=False)
    elif revocation == "membership":
        with owner_context(graph.organization_a):
            UserClinicRole.objects.filter(
                user_id=graph.shared_user, clinic_id=graph.clinic_a
            ).delete()
    else:
        remove_permission(graph, revocation)
    with runtime_role():
        assert integration.execute_operation(dispatched[0]) == "cancelled"
    assert sends == []
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(pk=dispatched[0])
        assert operation.status == "cancelled"
        # Base actor revocation is checked before the claim; domain permission
        # removals are checked by the subject guard after claiming, before send.
        assert operation.attempt_count == (
            0 if revocation in {"inactive", "membership"} else 1
        )
