"""Every workflow route refuses in-view input without session/audit/outbox writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.comms.models import IntegrationOperation
from apps.tenancy.db import tenant_context
from apps.workflows.callbacks import callback
from apps.workflows.models import Task, TaskComment
from django.db import connection
from django.urls import URLPattern, URLResolver

from patient_service_support import runtime_role

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
            if "clinic_id" in pattern.pattern.converters:
                yield prefix + pattern.name
            else:
                assert pattern.callback is callback


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
                "pk",
                "state",
                "revision",
                "owner_user_id",
                "owner_role",
            )
        )
        return writes(), rows, TaskComment.objects.count()
