"""Todo 72, scenario 15: a faulted model provider degrades AI only, never core."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.models import AIArtifact, AICircuit, AIInvocation
from apps.ai.views import settings_view
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation
from apps.identity.current_context import require_permission
from apps.tenancy.db import tenant_context
from django.http import HttpResponse

from ai.support import enqueue, seed
from ai.test_controls import native_request
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("mode", "reason", "settings_state"),
    [
        ("down", "provider_unavailable", "ready"),
        ("slow", "provider_timeout", "degraded"),
        ("ambiguous", "invalid_output", "ready"),
    ],
)
def test_a_faulted_model_fails_closed_once_and_core_keeps_working(  # noqa: PLR0913
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    mode: str,
    reason: str,
    settings_state: str,
) -> None:
    # Given: one admitted inference on the real action outbox, then a fault.
    world = seed(rbac_graph, monkeypatch)
    g = rbac_graph
    result = enqueue(world)
    assert result.operation_id is not None
    assert result.invocation_id is not None
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": mode}
    # When: the worker reaches the faulted adapter, and the job is redelivered.
    with runtime_role():
        assert execute_operation(result.operation_id) == "failed"
        assert execute_operation(result.operation_id) == "skipped"
    # Then: one explicit failure, the unknown charge kept, nothing retained and
    # no second route tried behind the user's back.
    with runtime_role(), tenant_context(g.shared_user, g.organization_a):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
        assert (invocation.status, invocation.reason_code) == ("failed", reason)
        assert invocation.cost_micros == invocation.reserved_micros
        assert not AIArtifact.objects.filter(kind="output").exists()
        assert AIInvocation.objects.count() == 1
        assert list(
            IntegrationOperation.objects.values_list("pk", "status", "attempt_count")
        ) == [(result.operation_id, "failed", 1)]
        assert AICircuit.objects.filter(
            configuration=world.configuration, closed_at__isnull=True
        ).exists() is (mode == "slow")
        # Core authority is untouched by the AI fault.
        assert require_permission("appointment.read", clinic_id=g.clinic_a) == (
            g.shared_user
        )
    # The administrator sees content-free health: only a timeout opens the
    # circuit, the other failures stay per request.
    request = native_request(world.administrator, g.clinic_a)
    with runtime_role(), tenant_context(world.administrator, g.organization_a):
        response = settings_view(request, g.clinic_a)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 200
    assert f'data-ai-state="{settings_state}"'.encode() in response.content


def test_the_next_request_after_a_timeout_is_refused_until_the_circuit_closes(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    # Given: a timeout opened the only configuration's circuit.
    world = seed(rbac_graph, monkeypatch)
    first = enqueue(world)
    assert first.operation_id is not None
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "slow"}
    with runtime_role():
        assert execute_operation(first.operation_id) == "failed"
    # When: the provider recovers but nobody has closed the circuit.
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    second = enqueue(world)
    # Then: admission refuses with a manual-path message; nothing is queued and
    # no other route is chosen silently.
    assert second.operation_id is None
    assert second.invocation_id is None
    assert second.reason == "circuit_open"
    assert second.message_key
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert AIInvocation.objects.count() == 1
