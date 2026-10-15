"""Todo 38 acceptance through real tenant roles and the shared action outbox."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai.adapters import SyntheticProvider, register_provider
from apps.ai.artifacts import read_artifact
from apps.ai.contracts import (
    GatewayAccessDeniedError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    Purpose,
)
from apps.ai.models import AIArtifact, AICircuit, AIInvocation
from apps.ai.owner import set_global_switch
from apps.ai.services import invoke
from apps.ai.tasks import run_operation
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation
from apps.identity.current_context import require_permission
from apps.tenancy.db import tenant_context
from django.db import connection
from django.http import HttpResponse

from ai.support import DEFAULT_BUDGET, SENTINEL, enqueue, model, seed, set_policy
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_fake_call_is_metered_once_and_all_content_stays_encrypted(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    # Given: real org authority and an offline provider, with publication observed.
    world = seed(rbac_graph, monkeypatch)
    g = rbac_graph
    result = enqueue(world)
    assert result.operation_id is not None
    assert result.invocation_id is not None
    assert world.publications == [
        ({"operation_id": str(result.operation_id)}, "ai-interactive", 0)
    ]
    monkeypatch.setattr("apps.ai.tasks.fair_acquire", lambda **_kwargs: True)
    # When: the registered worker drives the real outbox and adapter.
    with runtime_role():
        assert run_operation(operation_id=str(result.operation_id)) == "succeeded"
        assert execute_operation(result.operation_id) == "skipped"
    # Then: exact cost, one encrypted output, and content-free sinks remain.
    with runtime_role(), tenant_context(g.shared_user, g.organization_a):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
        assert (
            invocation.tokens_in,
            invocation.tokens_out,
            invocation.cost_micros,
        ) == (4, 6, 42)
        assert invocation.cost_minor == 1
        assert (
            read_artifact(clinic_id=g.clinic_a, artifact_id=invocation.inputs_ref_id)
            == SENTINEL
        )
        output = AIArtifact.objects.get(invocation=invocation)
        assert read_artifact(clinic_id=g.clinic_a, artifact_id=output.pk)
        assert AIArtifact.objects.filter(kind="output").count() == 1
        replay = invoke(
            clinic_id=g.clinic_a,
            purpose=Purpose.FRONT_DESK,
            principal=g.shared_user,
            inputs_ref=invocation.inputs_ref_id,
            budget=DEFAULT_BUDGET,
        )
        assert replay.operation_id == result.operation_id
        assert AIInvocation.objects.count() == 1
        sinks = {
            "metering": list(AIInvocation.objects.values()),
            "outbox": list(IntegrationOperation.objects.values()),
        }
        with connection.cursor() as cursor:
            cursor.execute("SELECT content FROM clinic_app.ai_aiartifact")
            assert all(
                SENTINEL.encode() not in bytes(row[0]) for row in cursor.fetchall()
            )
    # Inspect audit only after the runtime transaction commits, without new ACLs.
    with psycopg.connect(superuser_database_url) as inspection:
        audit_rows = inspection.execute(
            "SELECT event_type, payload FROM clinic_app.audit_event"
        ).fetchall()
    sinks["audit"] = [
        {"event_type": event_type, "payload": payload}
        for event_type, payload in audit_rows
    ]
    assert SENTINEL not in json.dumps(sinks, default=str)
    assert len(world.publications) == 1


def test_budget_exhaustion_refuses_before_outbox_publication(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a cap below the worst-case reservation.
    world = seed(rbac_graph, monkeypatch)
    set_policy(world, cap=1)
    # When: an otherwise authorized invocation requests admission.
    result = enqueue(world)
    # Then: refusal is content-free and the manual alternative is available.
    assert result.reason == "budget_exhausted"
    assert result.invocation_id is None
    assert result.message_key
    assert not world.publications


@pytest.mark.parametrize("global_stop", [False, True])
def test_a_kill_switch_stops_the_next_queued_call(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    global_stop: bool,
) -> None:
    # Given: a reserved request, before either stop commits.
    world = seed(rbac_graph, monkeypatch)
    result = enqueue(world)
    assert result.operation_id is not None
    assert result.invocation_id is not None
    if global_stop:
        set_global_switch(enabled=False, approved_regions=("br-synthetic",))
    else:
        set_policy(world, cap=10_000_000, enabled=False)
    # When: the already queued operation reaches the final send boundary.
    with runtime_role():
        outcome = execute_operation(result.operation_id)
    # Then: it cancels without producing output or spending the reservation.
    assert outcome == "cancelled"
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
        assert (invocation.status, invocation.reason_code, invocation.cost_micros) == (
            "failed",
            "disabled",
            0,
        )
        assert not AIArtifact.objects.filter(kind="output").exists()


class TimeoutProvider(SyntheticProvider):
    def complete(self, *, request: ProviderRequest) -> ProviderResult:
        self.check_boundary(request)
        raise ProviderTimeoutError


def test_timeout_opens_only_its_circuit_and_preserves_manual_work(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a deterministic timeout, not a timer wait.
    world = seed(rbac_graph, monkeypatch)
    register_provider(TimeoutProvider())
    result = enqueue(world)
    assert result.operation_id is not None
    assert result.invocation_id is not None
    # When: the real action transport observes the timeout.
    with runtime_role():
        assert execute_operation(result.operation_id) == "failed"
    # Then: unknown cost stays bounded, the circuit opens, and core still admits.
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
        assert invocation.reason_code == "provider_timeout"
        assert invocation.cost_micros == invocation.reserved_micros
        assert AICircuit.objects.filter(
            configuration=world.configuration, closed_at__isnull=True
        ).exists()
        assert (
            require_permission("appointment.read", clinic_id=rbac_graph.clinic_a)
            == rbac_graph.shared_user
        )
    from apps.ai.views import settings_view  # noqa: PLC0415

    from ai.test_controls import native_request  # noqa: PLC0415

    request = native_request(world.administrator, rbac_graph.clinic_a)
    with runtime_role(), tenant_context(world.administrator, rbac_graph.organization_a):
        response = settings_view(request, rbac_graph.clinic_a)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 200
    assert b'data-ai-state="degraded"' in response.content


@pytest.mark.parametrize("same_jurisdiction", [False, True])
def test_fallback_never_crosses_the_approved_jurisdiction(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    same_jurisdiction: bool,
) -> None:
    # Given: a timed-out primary and a separately reviewed fallback.
    world = seed(rbac_graph, monkeypatch)
    register_provider(TimeoutProvider())
    original = enqueue(world)
    assert original.operation_id is not None
    with runtime_role():
        assert execute_operation(original.operation_id) == "failed"
    region = "br-synthetic" if same_jurisdiction else "us-synthetic"
    fallback = model(fallback=True, region=region)
    set_global_switch(enabled=True, approved_regions=("br-synthetic", "us-synthetic"))
    # When: the next invocation chooses a route.
    result = enqueue(world)
    # Then: approval alone cannot authorize a jurisdiction change.
    if same_jurisdiction:
        assert result.invocation_id is not None
        with (
            runtime_role(),
            tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        ):
            assert AIInvocation.objects.get(
                pk=result.invocation_id
            ).configuration_id == (fallback.pk)
    else:
        assert result.reason == "circuit_open"


def test_unapproved_region_is_refused_at_configuration_load(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: approved model terms whose region is absent from the global allowlist.
    world = seed(rbac_graph, monkeypatch)
    set_global_switch(enabled=True, approved_regions=("eu-synthetic",))
    # When: admission loads the primary.
    result = enqueue(world)
    # Then: no provider or outbox publication occurs.
    assert result.reason == "configuration_unavailable"
    assert world.publications == []


def test_unknown_foreign_and_other_user_inputs_have_identical_denial(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one authorized encrypted source.
    world = seed(rbac_graph, monkeypatch)
    result = enqueue(world)
    assert result.invocation_id is not None
    with owner_context(rbac_graph.organization_a):
        source_id = AIInvocation.objects.get(pk=result.invocation_id).inputs_ref_id
    # When/Then: wrong clinic, missing id, and another user all refuse equally.
    with runtime_role(), tenant_context(world.administrator, rbac_graph.organization_a):
        for clinic, reference in (
            (rbac_graph.clinic_a, source_id),
            (rbac_graph.clinic_a, uuid4()),
            (rbac_graph.clinic_b, source_id),
        ):
            with pytest.raises(GatewayAccessDeniedError):
                invoke(
                    clinic_id=clinic,
                    purpose=Purpose.FRONT_DESK,
                    principal=world.administrator,
                    inputs_ref=reference,
                    budget=DEFAULT_BUDGET,
                )
            with pytest.raises(GatewayAccessDeniedError):
                read_artifact(clinic_id=clinic, artifact_id=reference)
