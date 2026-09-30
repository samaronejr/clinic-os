"""Every workflow route refuses in-view input without session/audit/outbox writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.comms.models import IntegrationOperation
from apps.identity.models import RoleGrant, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task, TaskComment
from apps.workflows.reassignment import preview_reassignment
from apps.workflows.services import TaskOwner, assign_task, create_task, start_task
from django.db import connection
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from renewal.test_retention import staff_client
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_cross_clinic_appointment_setups,
)
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def workflow_routes(resolver: URLResolver, prefix: str = "") -> Iterator[str]:
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLResolver):
            namespace = f"{prefix}{pattern.namespace}:" if pattern.namespace else prefix
            yield from workflow_routes(pattern, namespace)
        elif isinstance(pattern, URLPattern) and prefix == "workflows:":
            assert pattern.name is not None
            yield prefix + pattern.name


def writes() -> tuple[int, int]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0]), IntegrationOperation.objects.count()


def assert_refused(
    response: _MonkeyPatchedWSGIResponse, baseline: _MonkeyPatchedWSGIResponse
) -> None:
    assert response.status_code == 403
    assert response.content == baseline.content
    assert not response.cookies
    assert response.wsgi_request.session.modified is False
    # The strict CSP uses no nonces; only telemetry's request ID varies.
    assert {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() != "x-request-id"
    } == {
        key.lower(): value
        for key, value in baseline.headers.items()
        if key.lower() != "x-request-id"
    }


def test_all_routes_unknown_foreign_and_malformed_selectors_are_side_effect_free(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task = create_task(
            clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        foreign = create_task(
            clinic_id=graph.clinic_b,
            spec=replace(
                spec(graph), subject_ref={"kind": "clinic", "id": str(graph.clinic_b)}
            ),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        before = writes()
    routes = tuple(workflow_routes(get_resolver()))
    assert routes
    with physician_client(graph) as client:
        for name in routes:
            foreign_url = reverse(name, kwargs={"clinic_id": graph.clinic_b})
            unknown_url = reverse(name, kwargs={"clinic_id": uuid4()})
            own_url = reverse(name, kwargs={"clinic_id": graph.clinic_a})
            session = dict(client.session)
            baseline = client.get(unknown_url)
            assert baseline.status_code == 403
            assert_refused(client.get(foreign_url), baseline)
            for headers in ({}, {"HX-Request": "true"}):
                for body in (
                    {},
                    {"action": "unknown"},
                    {"action": "complete", "task_id": "malformed"},
                    {
                        "action": "complete",
                        "task_id": str(uuid4()),
                        "expected_revision": "1",
                        "checked": "on",
                    },
                ):
                    assert_refused(
                        client.post(own_url, body, headers=headers), baseline
                    )
                for task_id in (foreign.pk, uuid4()):
                    assert_refused(
                        client.post(
                            own_url,
                            {
                                "action": "complete",
                                "task_id": str(task_id),
                                "expected_revision": "1",
                                "checked": "on",
                            },
                            headers=headers,
                        ),
                        baseline,
                    )
                for task_id in (task.pk, uuid4()):
                    assert_refused(
                        client.post(
                            foreign_url,
                            {
                                "action": "complete",
                                "task_id": str(task_id),
                                "expected_revision": "1",
                                "checked": "on",
                            },
                            headers=headers,
                        ),
                        baseline,
                    )
            assert dict(client.session) == session
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        task.refresh_from_db()
        assert task.state == "open"
        assert writes() == before


@pytest.mark.parametrize("manager", [False, True], ids=["staff", "manager"])
def test_non_owner_start_and_complete_are_the_identical_route_denial(
    rbac_graph: RbacGraph, manager: bool
) -> None:
    """B1 at the route: seeing a task (as a manager) never grants its owner's work."""
    graph = rbac_graph
    owner, _enrollment = permission_actor(graph, "nurse")
    boss, _enrollment = permission_actor(graph, "clinic_manager")
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


def test_another_clinics_reference_is_the_unknown_id_route_denial(
    rbac_graph: RbacGraph,
) -> None:
    """R4-B1 at the route: a two-clinic user gets identical bytes, no writes."""
    graph = rbac_graph
    first, second = seed_cross_clinic_appointment_setups(graph)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointment = create_synthetic_appointment(
            second, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        ).pk
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        foreign_task = create_task(
            clinic_id=graph.clinic_b,
            spec=replace(
                spec(graph), subject_ref={"kind": "clinic", "id": str(graph.clinic_b)}
            ),
            idempotency_key=uuid4(),
        ).pk
        review = create_task(
            clinic_id=graph.clinic_a,
            spec=replace(spec(graph), kind="review"),
            idempotency_key=uuid4(),
        )
        review = assign_task(
            clinic_id=graph.clinic_a,
            task_id=review.pk,
            owner=TaskOwner(user_id=graph.physician),
            expected_revision=1,
        )
        review = start_task(
            clinic_id=graph.clinic_a, task_id=review.pk, expected_revision=2
        )
    foreigners = {
        "clinic": graph.clinic_b,
        "enrollment": second.enrollment_id,
        "appointment": appointment,
        "task": foreign_task,
    }
    assert first.clinic_id == graph.clinic_a
    own_url = reverse("workflows:tasks", kwargs={"clinic_id": graph.clinic_a})

    def create(kind: str, record: UUID) -> dict[str, str]:
        return {
            "action": "create",
            "kind": "checklist",
            "priority": "normal",
            "due_date": "02/06/2035",
            "due_time": "09:00",
            "subject_kind": kind,
            "subject_id": str(record),
            "idempotency_key": str(uuid4()),
        }

    def complete(kind: str, record: UUID) -> dict[str, str]:
        return {
            "action": "complete",
            "task_id": str(review.pk),
            "expected_revision": str(review.revision),
            "evidence_kind": "reference",
            "record_kind": kind,
            "record_id": str(record),
            "outcome": "reviewed",
        }

    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        before = writes()
    with physician_client(graph) as client:
        for kind, foreign in foreigners.items():
            for body in (create, complete):
                baseline = client.post(own_url, body(kind, uuid4()))
                assert baseline.status_code == 403
                assert_refused(client.post(own_url, body(kind, foreign)), baseline)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert writes() == before
        assert Task.objects.get(pk=review.pk).state == "in_progress"


WRITE_ACTIONS = (
    "create",
    "assign",
    "start",
    "complete",
    "cancel",
    "comment",
    "bulk-apply",
)


def _queue_snapshot(graph: RbacGraph) -> tuple[tuple[int, int], object, int]:
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        rows = sorted(
            Task.objects.filter(clinic_id=graph.clinic_a).values_list(
                "pk", "state", "revision", "owner_user_id", "owner_role"
            )
        )
        return writes(), rows, TaskComment.objects.count()


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
