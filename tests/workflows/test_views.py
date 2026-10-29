"""Every workflow route refuses in-view input without session/audit/outbox writes."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from apps.workflows.services import TaskOwner, assign_task, create_task, start_task
from django.urls import get_resolver, reverse

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_cross_clinic_appointment_setups,
)
from workflow_view_support import assert_refused, workflow_routes, writes
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


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
            workflow_bundle_version=2,
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
