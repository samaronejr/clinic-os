"""Sessionless synthetic receipt HTTP authenticates before any scope lookup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core import integration
from apps.workflows import callbacks, external
from apps.workflows.models import WorkflowStep
from django.test import Client
from django.urls import reverse

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.test_engine import execute, publish, start, step_id

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_disabled_and_unauthenticated_callback_cannot_resolve_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        pytest.fail("unauthenticated bytes reached stored-scope resolution")

    monkeypatch.setattr(integration, "_resolve_callback_scope", forbidden_lookup)
    client = Client(enforce_csrf_checks=True)
    url = reverse("workflows:synthetic-callback")
    assert client.get(url).status_code == 405
    assert (
        client.post(url, data=b"{}", content_type="application/json").status_code == 403
    )


@pytest.mark.usefixtures("synthetic_workflow_transport")
def test_authenticated_http_delivery_and_replay(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integration, "_dispatch", lambda _operation: None)
    external.register_adapters()
    run = start(
        rbac_graph,
        publish(
            rbac_graph,
            [{"handler": "external", "provider": "workflow-synthetic-v1"}],
        ),
    )
    identifier = step_id(rbac_graph, run)
    assert execute(identifier) == "waiting"
    with owner_context(rbac_graph.organization_a):
        operation_id = WorkflowStep.objects.get(pk=identifier).operation_id
    assert operation_id is not None
    with runtime_role():
        assert integration.execute_operation(operation_id) == "succeeded"
    body = json.dumps(
        {
            "event_id": uuid4().hex,
            "provider_reference": f"synthetic:workflow:{operation_id}",
            "status": "delivered",
        }
    ).encode()
    client = Client(enforce_csrf_checks=True)
    url = reverse("workflows:synthetic-callback")
    assert (
        client.post(url, data=body, content_type="application/json").status_code == 403
    )
    signature = callbacks.signature(body)
    with runtime_role():
        first = client.post(
            url,
            data=body,
            content_type="application/json",
            HTTP_X_WORKFLOW_SIGNATURE=signature,
        )
        second = client.post(
            url,
            data=body,
            content_type="application/json",
            HTTP_X_WORKFLOW_SIGNATURE=signature,
        )
    assert first.status_code == second.status_code == 200
    assert first.json() == {"result": "applied"}
    assert second.json() == {"result": "duplicate"}
