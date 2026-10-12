"""Every workflow route refuses in-view input without session/audit/outbox writes."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import RoleGrant, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from apps.workflows.reassignment import preview_reassignment
from apps.workflows.services import TaskOwner, assign_task, create_task, start_task
from django.urls import reverse
from django.utils import timezone

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from renewal.test_retention import staff_client
from workflow_view_support import WRITE_ACTIONS, _queue_snapshot, assert_refused, writes
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("manager", [False, True], ids=["staff", "manager"])
def test_non_owner_start_and_complete_are_the_identical_route_denial(
    rbac_graph: RbacGraph, manager: bool
) -> None:
    """B1 at the route: seeing a task (as a manager) never grants its owner's work."""
    graph = rbac_graph
    owner, _enrollment = permission_actor(graph, "nurse")
    boss, _enrollment = permission_actor(graph, "clinic_manager")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(user_id__in=(owner, boss)).update(
            workflow_bundle_version=2
        )
    with runtime_role(), tenant_context(boss, graph.organization_a):
        assigned, started = (
            assign_task(
                clinic_id=graph.clinic_a,
                task_id=create_task(
                    clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
                ).pk,
                owner=TaskOwner(user_id=owner),
                expected_revision=1,
            )
            for _ in range(2)
        )
    with runtime_role(), tenant_context(owner, graph.organization_a):
        started = start_task(
            clinic_id=graph.clinic_a, task_id=started.pk, expected_revision=2
        )
    if manager:
        with owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=graph.physician,
                role=UserClinicRole.Role.CLINIC_MANAGER,
            )
    with runtime_role(), tenant_context(boss, graph.organization_a):
        before = writes()
    own_url = reverse("workflows:tasks", kwargs={"clinic_id": graph.clinic_a})
    with physician_client(graph) as client:
        baseline = client.get(reverse("workflows:tasks", kwargs={"clinic_id": uuid4()}))
        for headers in ({}, {"HX-Request": "true"}):
            for action, task in (("start", assigned), ("complete", started)):
                assert_refused(
                    client.post(
                        own_url,
                        {
                            "action": action,
                            "task_id": str(task.pk),
                            "expected_revision": str(task.revision),
                            "checked": "on",
                        },
                        headers=headers,
                    ),
                    baseline,
                )
    with runtime_role(), tenant_context(boss, graph.organization_a):
        assert writes() == before
        assert Task.objects.get(pk=assigned.pk).state == "assigned"
        assert Task.objects.get(pk=started.pk).state == "in_progress"


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_write_permission_without_view_scope_refuses_before_any_write(
    rbac_graph: RbacGraph, action: str
) -> None:
    """Authorize-before-write: the queue re-render needs tasks.view.

    A manager keeps tasks.assign, tasks.complete and tasks.reassign while a v2
    clinic subtraction removes tasks.view. Every write action must answer the
    unknown-clinic refusal and leave no task, comment, audit or outbox row: a
    view that wrote first and then failed its read scope would commit the write
    behind the refusal (the tenant transaction commits below 500).
    """
    graph = rbac_graph
    admin = graph.clinic_admin
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
            workflow_bundle_version=2,
        )
    with runtime_role(), tenant_context(admin, graph.organization_a):
        made = {
            name: create_task(
                clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
            )
            for name in ("open", "assigned", "started", "cancel", "comment", "bulk")
        }
        for name in ("assigned", "started"):
            made[name] = assign_task(
                clinic_id=graph.clinic_a,
                task_id=made[name].pk,
                owner=TaskOwner(user_id=admin),
                expected_revision=1,
            )
        made["started"] = start_task(
            clinic_id=graph.clinic_a, task_id=made["started"].pk, expected_revision=2
        )
        token = preview_reassignment(
            clinic_id=graph.clinic_a,
            task_ids=[made["bulk"].pk],
            owner=TaskOwner(role=UserClinicRole.Role.RECEPTIONIST),
        ).token
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role=UserClinicRole.Role.CLINIC_ADMIN,
            permission="tasks.view",
            bundle_version=2,
            valid_from=timezone.now() - timedelta(minutes=1),
        )
    bodies = {
        "create": {
            "action": "create",
            "kind": "checklist",
            "priority": "normal",
            "due_date": "02/06/2035",
            "due_time": "09:00",
            "subject_kind": "clinic",
            "subject_id": str(graph.clinic_a),
            "idempotency_key": str(uuid4()),
        },
        "assign": {
            "action": "assign",
            "task_id": str(made["open"].pk),
            "expected_revision": "1",
            "owner": "role:receptionist",
        },
        "start": {
            "action": "start",
            "task_id": str(made["assigned"].pk),
            "expected_revision": "2",
        },
        "complete": {
            "action": "complete",
            "task_id": str(made["started"].pk),
            "expected_revision": "3",
            "checked": "on",
        },
        "cancel": {
            "action": "cancel",
            "task_id": str(made["cancel"].pk),
            "expected_revision": "1",
        },
        "comment": {
            "action": "comment",
            "task_id": str(made["comment"].pk),
            "expected_revision": "1",
            "idempotency_key": str(uuid4()),
            "body": "Sintetico comment",
        },
        "bulk-apply": {"action": "bulk-apply", "preview_token": token},
    }
    before = _queue_snapshot(graph)
    own_url = reverse("workflows:tasks", kwargs={"clinic_id": graph.clinic_a})
    with staff_client(admin) as client:
        baseline = client.get(reverse("workflows:tasks", kwargs={"clinic_id": uuid4()}))
        assert baseline.status_code == 403
        for headers in ({}, {"HX-Request": "true"}):
            assert_refused(
                client.post(own_url, bodies[action], headers=headers), baseline
            )
    assert _queue_snapshot(graph) == before


def test_method_selection_form_and_revision_exits_write_nothing(
    rbac_graph: RbacGraph,
) -> None:
    """Every in-view 4xx exit runs over HTTP and leaves no trace.

    A manager is refused for an unsupported method and for a malformed bulk
    selection with the unknown-clinic bytes; an invalid creation form answers
    400 and a stale revision 409, and none of them writes.
    """
    graph = rbac_graph
    admin = graph.clinic_admin
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
            workflow_bundle_version=2,
        )
    with runtime_role(), tenant_context(admin, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
    before = _queue_snapshot(graph)
    own_url = reverse("workflows:tasks", kwargs={"clinic_id": graph.clinic_a})
    with staff_client(admin) as client:
        baseline = client.get(reverse("workflows:tasks", kwargs={"clinic_id": uuid4()}))
        assert baseline.status_code == 403
        assert_refused(client.put(own_url), baseline)
        for headers in ({}, {"HX-Request": "true"}):
            assert_refused(
                client.post(
                    own_url,
                    {
                        "action": "bulk-preview",
                        "task_ids": "malformed",
                        "owner": "role:receptionist",
                    },
                    headers=headers,
                ),
                baseline,
            )
            invalid = client.post(
                own_url,
                {"action": "create", "kind": "checklist", "priority": "normal"},
                headers=headers,
            )
            assert invalid.status_code == 400
            assert invalid.wsgi_request.session.modified is False
            stale = client.post(
                own_url,
                {
                    "action": "assign",
                    "task_id": str(task.pk),
                    "expected_revision": "7",
                    "owner": "me",
                },
                headers=headers,
            )
            assert stale.status_code == 409
            assert stale.wsgi_request.session.modified is False
    assert _queue_snapshot(graph) == before
