"""Every workflow route refuses in-view input without session/audit/outbox writes."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms.models import IntegrationOperation
from apps.tenancy.db import tenant_context
from apps.workflows.services import create_task
from django.db import connection
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
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
