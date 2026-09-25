"""Todo 7: typed non-communication work and canonical replay identity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.core.integration import (
    ActionOperationRequest,
    IdempotencyConflictError,
    IntegrationInputError,
    OperationKind,
    OperationRequest,
    enqueue_operation,
    execute_operation,
    register_action_adapter,
)
from apps.identity.current_context import require_permission
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connections, transaction

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_integration_boundary import (
    CHANNEL,
    PROVIDER,
    SUBJECT_TYPE,
    Harness,
    _clinic_id,
    _operation,
    harness,
    recorded_dispatch,
)

if TYPE_CHECKING:
    from apps.comms.adapters import OperationScope

    from conftest import TenantGraph

__all__ = ("harness", "recorded_dispatch")
pytestmark = pytest.mark.django_db(transaction=True)


def _request(graph: TenantGraph) -> OperationRequest:
    return OperationRequest(
        channel=CHANNEL,
        provider=PROVIDER,
        clinic_id=_clinic_id(graph.organization_a),
        subject_type=SUBJECT_TYPE,
        subject_id=uuid4(),
        idempotency_key=uuid4(),
    )


@pytest.mark.parametrize(
    "changed",
    ["channel", "provider", "subject_type", "subject_id", "clinic_id", "max_attempts"],
)
def test_same_key_with_different_routing_refuses_without_dispatch(
    tenant_graph: TenantGraph, recorded_dispatch: list[str], changed: str
) -> None:
    request = _request(tenant_graph)
    changes = {
        "channel": replace(request, channel="email"),
        "provider": replace(request, provider="synthetic-other"),
        "subject_type": replace(request, subject_type="synthetic.other"),
        "subject_id": replace(request, subject_id=uuid4()),
        "clinic_id": replace(request, clinic_id=uuid4()),
        "max_attempts": replace(request, max_attempts=3),
    }
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
    ):
        original = enqueue_operation(request)
        with pytest.raises(IdempotencyConflictError, match="idempotency key conflicts"):
            enqueue_operation(changes[changed])
        assert enqueue_operation(request) == original
    assert recorded_dispatch == [str(original)]


def test_action_has_no_communication_channel_and_executes_through_boundary(
    tenant_graph: TenantGraph,
    recorded_dispatch: list[str],
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def eligible(scope: OperationScope) -> bool:
        require_permission("appointment.read", clinic_id=scope.clinic_id)
        return True

    register_action_adapter(harness.adapter)
    monkeypatch.setitem(integration._SUBJECT_RECHECKS, "synthetic.action", eligible)
    request = ActionOperationRequest(
        provider=PROVIDER,
        clinic_id=_clinic_id(tenant_graph.organization_a),
        subject_type="synthetic.action",
        subject_id=uuid4(),
        idempotency_key=uuid4(),
        payload_digest="a" * 64,
    )
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
    ):
        operation_id = enqueue_operation(request)
        assert enqueue_operation(request) == operation_id
        with pytest.raises(IdempotencyConflictError):
            enqueue_operation(replace(request, payload_digest="b" * 64))
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.kind == OperationKind.ACTION
    assert operation.channel == ""
    assert operation.payload_digest == "a" * 64
    assert recorded_dispatch == [str(operation_id)]
    with runtime_role():
        assert execute_operation(operation_id) == "succeeded"
    assert len(harness.adapter.sent) == 1
    assert (
        _operation(tenant_graph.organization_a, operation_id).status
        == IntegrationOperation.Status.SUCCEEDED
    )


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "SINTETICO-SENTINELA-PHI"])
def test_action_payload_is_a_digest_never_free_text(
    tenant_graph: TenantGraph, recorded_dispatch: list[str], digest: str
) -> None:
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
        pytest.raises(IntegrationInputError),
    ):
        enqueue_operation(
            ActionOperationRequest(
                provider=PROVIDER,
                clinic_id=_clinic_id(tenant_graph.organization_a),
                subject_type="synthetic.action",
                subject_id=uuid4(),
                idempotency_key=uuid4(),
                payload_digest=digest,
            )
        )
    assert recorded_dispatch == []


def test_kind_and_actor_are_part_of_replay_identity(
    tenant_graph: TenantGraph, recorded_dispatch: list[str]
) -> None:
    request = _request(tenant_graph)
    with owner_context(tenant_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=tenant_graph.organization_a,
            clinic_id=request.clinic_id,
            user_id=tenant_graph.user_b,
            role="owner",
        )
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
    ):
        original = enqueue_operation(request)
        action = ActionOperationRequest(
            provider=request.provider,
            clinic_id=request.clinic_id,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            idempotency_key=request.idempotency_key,
            payload_digest="a" * 64,
        )
        with pytest.raises(IdempotencyConflictError):
            enqueue_operation(action)
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_b, tenant_graph.organization_a),
        pytest.raises(IdempotencyConflictError),
    ):
        enqueue_operation(request)
    assert recorded_dispatch == [str(original)]


@pytest.mark.parametrize("registered", [False, True])
def test_actions_require_their_own_adapter_and_subject_recheck(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
    registered: bool,
) -> None:
    if registered:
        register_action_adapter(harness.adapter)
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
    ):
        operation_id = enqueue_operation(
            ActionOperationRequest(
                provider=PROVIDER,
                clinic_id=_clinic_id(tenant_graph.organization_a),
                subject_type="synthetic.action.no_policy",
                subject_id=uuid4(),
                idempotency_key=uuid4(),
                payload_digest="a" * 64,
            )
        )
    with runtime_role():
        assert execute_operation(operation_id) == (
            "cancelled" if registered else "failed"
        )
    assert harness.adapter.sent == []
    assert harness.adapter.prepared_calls == []


def test_action_binding_is_immutable_at_the_database(
    tenant_graph: TenantGraph, recorded_dispatch: list[str]
) -> None:
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
    ):
        operation_id = enqueue_operation(
            ActionOperationRequest(
                provider=PROVIDER,
                clinic_id=_clinic_id(tenant_graph.organization_a),
                subject_type="synthetic.action",
                subject_id=uuid4(),
                idempotency_key=uuid4(),
                payload_digest="a" * 64,
            )
        )
    with owner_context(tenant_graph.organization_a):
        with pytest.raises(IntegrityError), transaction.atomic():
            IntegrationOperation.objects.filter(pk=operation_id).update(
                payload_digest="b" * 64
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            IntegrationOperation.objects.filter(pk=operation_id).update(
                kind="communication"
            )


@pytest.mark.parametrize(
    ("kind", "channel", "digest"),
    [
        ("action", "email", "a" * 64),
        ("action", "", "bad"),
        ("communication", "", ""),
        ("communication", "carrier", ""),
        ("unknown", "email", ""),
    ],
)
def test_action_kind_and_digest_constraint_refuses_raw_inserts(
    tenant_graph: TenantGraph,
    kind: str,
    channel: str,
    digest: str,
) -> None:
    clinic_id = _clinic_id(tenant_graph.organization_a)
    with (
        owner_context(tenant_graph.organization_a),
        pytest.raises(IntegrityError) as error,
        transaction.atomic(),
    ):
        IntegrationOperation.objects.create(
            organization_id=tenant_graph.organization_a,
            clinic_id=clinic_id,
            actor_id=tenant_graph.user_a,
            kind=kind,
            channel=channel,
            payload_digest=digest,
            provider=PROVIDER,
            subject_type="synthetic.action",
            subject_id=uuid4(),
            idempotency_key=uuid4(),
        )
    assert isinstance(error.value.__cause__, psycopg.errors.CheckViolation)
    assert error.value.__cause__.diag.constraint_name == "comms_operation_kind_contract"


@pytest.mark.parametrize("different", [False, True])
def test_concurrent_key_claims_have_one_intent_and_no_blind_replay(
    tenant_graph: TenantGraph, recorded_dispatch: list[str], different: bool
) -> None:
    request = _request(tenant_graph)
    ready = Barrier(2, timeout=15)

    def enqueue(candidate: OperationRequest) -> str:
        try:
            with (
                runtime_role(),
                tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
            ):
                ready.wait()
                try:
                    return str(enqueue_operation(candidate))
                except IdempotencyConflictError:
                    return "conflict"
        finally:
            connections.close_all()

    second = replace(request, subject_id=uuid4()) if different else request
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(enqueue, request)
        second_result = pool.submit(enqueue, second)
        outcomes = [first_result.result(timeout=20), second_result.result(timeout=20)]
    assert len(recorded_dispatch) == 1
    assert outcomes.count("conflict") == int(different)
    assert set(outcomes) - {"conflict"} == set(recorded_dispatch)
