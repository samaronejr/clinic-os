"""Acceptance tests for the shared job and callback transaction boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.adapters import (
    AuthenticatedCallback,
    CallbackAuthenticationError,
    SendResult,
    TransientSendError,
)
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.core.integration import (
    IntegrationContextError,
    OperationRequest,
    clear_integration_registrations,
    enqueue_operation,
    receive_provider_callback,
    register_callback_authenticator,
    register_send_adapter,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pytest_django.fixtures import SettingsWrapper

    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)

PROVIDER: Final = "synthetic-provider"
SECRET: Final = b"synthetic-callback-secret"
CHANNEL: Final = IntegrationOperation.Channel.SMS
SUBJECT_TYPE: Final = "intake.patient_clinic_enrollment"


class SyntheticCrashError(Exception):
    """Simulate a worker crash after the provider accepted the send."""


class SyntheticRollbackError(Exception):
    """Force the enqueueing tenant transaction to roll back."""


@dataclass
class RecordingAdapter:
    """Synthetic provider adapter recording transaction state at each call."""

    provider: str = PROVIDER
    prepared_calls: list[tuple[UUID, bool]] = field(default_factory=list)
    sent: list[tuple[UUID, object, bool, str | None]] = field(default_factory=list)
    send_errors: list[Exception] = field(default_factory=list)
    crash_on_send: bool = False

    def prepare(self, operation: IntegrationOperation) -> object:
        """Record that preparation ran inside the tenant transaction."""
        self.prepared_calls.append((operation.pk, connection.in_atomic_block))
        return {
            "subject_type": operation.subject_type,
            "subject_id": str(operation.subject_id),
        }

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Record that the external call ran with no open transaction."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_tenant', true)")
            row = cursor.fetchone()
        tenant_guc = None if row is None else row[0]
        self.sent.append(
            (operation_id, prepared, connection.in_atomic_block, tenant_guc)
        )
        if self.crash_on_send:
            raise SyntheticCrashError
        if self.send_errors:
            raise self.send_errors.pop(0)
        return SendResult(provider_reference=f"ref-{uuid4().hex[:12]}")


class SyntheticAuthenticator:
    """HMAC authenticator for the synthetic provider's signed callbacks."""

    provider: str = PROVIDER

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AuthenticatedCallback:
        """Verify the signature and return only verified callback fields."""
        signature = headers.get("x-synthetic-signature", "")
        expected = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise CallbackAuthenticationError
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise CallbackAuthenticationError from error
        status = payload.get("status")
        if status not in ("delivered", "failed"):
            raise CallbackAuthenticationError
        event_id = payload.get("event_id")
        provider_reference = payload.get("provider_reference")
        if (
            type(event_id) is not str
            or type(provider_reference) is not str
            or not event_id
            or not provider_reference
        ):
            raise CallbackAuthenticationError
        return AuthenticatedCallback(
            event_id=event_id,
            provider_reference=provider_reference,
            status=status,
        )


@dataclass(frozen=True, slots=True)
class Harness:
    """Synthetic adapter and authenticator registered for one test."""

    adapter: RecordingAdapter
    authenticator: SyntheticAuthenticator


@pytest.fixture
def harness() -> Iterator[Harness]:
    """Register the synthetic provider surfaces for one test."""
    adapter = RecordingAdapter()
    authenticator = SyntheticAuthenticator()
    register_send_adapter(adapter)
    register_callback_authenticator(authenticator)
    try:
        yield Harness(adapter=adapter, authenticator=authenticator)
    finally:
        clear_integration_registrations()


@pytest.fixture
def eager_dispatch(settings: SettingsWrapper) -> None:
    """Route real task dispatch through Celery's synchronous eager path."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = False


@pytest.fixture
def recorded_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record dispatch attempts without executing the task body."""
    dispatched: list[str] = []
    monkeypatch.setattr(
        execute_operation_task,
        "apply_async",
        lambda **kwargs: dispatched.append(kwargs["kwargs"]["operation_id"]),
    )
    return dispatched


def _clinic_id(organization_id: UUID) -> UUID:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        return Clinic.objects.get(organization_id=organization_id).pk


def _enqueue(
    graph: TenantGraph,
    organization_id: UUID,
    actor_id: UUID,
    *,
    idempotency_key: UUID | None = None,
    max_attempts: int = 5,
) -> UUID:
    request = OperationRequest(
        channel=CHANNEL,
        provider=PROVIDER,
        clinic_id=_clinic_id(organization_id),
        subject_type=SUBJECT_TYPE,
        subject_id=uuid4(),
        idempotency_key=idempotency_key or uuid4(),
        max_attempts=max_attempts,
    )
    with tenant_context(actor_id, organization_id):
        return enqueue_operation(request)


def _run_task(operation_id: UUID) -> str:
    result = execute_operation_task.apply(kwargs={"operation_id": str(operation_id)})
    return str(result.result)


def _signed_callback(
    *,
    event_id: str,
    provider_reference: str,
    status: str,
    extra_claims: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], bytes]:
    payload: dict[str, object] = {
        "event_id": event_id,
        "provider_reference": provider_reference,
        "status": status,
    }
    if extra_claims:
        payload.update(extra_claims)
    body = json.dumps(payload).encode()
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return {"x-synthetic-signature": signature}, body


def _operation(organization_id: UUID, operation_id: UUID) -> IntegrationOperation:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        operation = IntegrationOperation.objects.filter(pk=operation_id).first()
    assert operation is not None
    return operation


def _audit_verbs(organization_id: UUID, operation_id: UUID) -> list[str]:
    rows = AuditEvent.objects.filter(
        organization_id=organization_id,
        affected_record_id=str(operation_id),
    ).values_list("payload__object_verb", flat=True)
    return sorted(rows)


def _gucs() -> tuple[str | None, str | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.current_user_id', true), "
            "current_setting('app.current_tenant', true)"
        )
        row = cursor.fetchone()
    assert row is not None
    return row[0], row[1]


def _assert_gucs_empty() -> None:
    assert all(value in (None, "") for value in _gucs())


def test_two_tenant_operations_execute_once_each_and_clear_context(
    tenant_graph: TenantGraph,
    harness: Harness,
    eager_dispatch: None,
) -> None:
    with runtime_role():
        operation_a = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
        operation_b = _enqueue(
            tenant_graph, tenant_graph.organization_b, tenant_graph.user_b
        )

    assert len(harness.adapter.sent) == 2
    sent_ids = {entry[0] for entry in harness.adapter.sent}
    assert sent_ids == {operation_a, operation_b}
    assert all(
        in_atomic is False and tenant_guc in (None, "")
        for _, _, in_atomic, tenant_guc in harness.adapter.sent
    )
    assert all(in_atomic for _, in_atomic in harness.adapter.prepared_calls)

    operation_a_row = _operation(tenant_graph.organization_a, operation_a)
    operation_b_row = _operation(tenant_graph.organization_b, operation_b)
    assert operation_a_row.status == IntegrationOperation.Status.SUCCEEDED
    assert operation_b_row.status == IntegrationOperation.Status.SUCCEEDED
    assert operation_a_row.attempt_count == 1
    assert operation_b_row.attempt_count == 1
    assert operation_a_row.organization_id == tenant_graph.organization_a
    assert operation_b_row.organization_id == tenant_graph.organization_b
    assert operation_a_row.provider_reference is not None
    assert operation_b_row.provider_reference is not None
    assert _audit_verbs(tenant_graph.organization_a, operation_a) == [
        "enqueued",
        "succeeded",
    ]
    assert _audit_verbs(tenant_graph.organization_b, operation_b) == [
        "enqueued",
        "succeeded",
    ]
    _assert_gucs_empty()


def test_repeated_jobs_reconcile_to_one_result(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )

    assert recorded_dispatch == [str(operation_id)]
    with runtime_role():
        assert _run_task(operation_id) == "succeeded"
        assert _run_task(operation_id) == "skipped"
        assert _run_task(operation_id) == "skipped"

    assert len(harness.adapter.sent) == 1
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.SUCCEEDED
    assert operation.attempt_count == 1
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "enqueued",
        "succeeded",
    ]
    _assert_gucs_empty()


def test_duplicate_enqueue_returns_one_operation_and_dispatches_once(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    idempotency_key = uuid4()
    request = OperationRequest(
        channel=CHANNEL,
        provider=PROVIDER,
        clinic_id=_clinic_id(tenant_graph.organization_a),
        subject_type=SUBJECT_TYPE,
        subject_id=uuid4(),
        idempotency_key=idempotency_key,
    )
    with (
        runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
    ):
        first = enqueue_operation(request)
        second = enqueue_operation(request)

    assert first == second
    assert recorded_dispatch == [str(first)]
    _assert_gucs_empty()


def _enqueue_then_rollback(graph: TenantGraph) -> None:
    with tenant_context(graph.user_a, graph.organization_a):
        enqueue_operation(
            OperationRequest(
                channel=CHANNEL,
                provider=PROVIDER,
                clinic_id=_clinic_id(graph.organization_a),
                subject_type=SUBJECT_TYPE,
                subject_id=uuid4(),
                idempotency_key=uuid4(),
            )
        )
        raise SyntheticRollbackError


def test_rolled_back_enqueue_dispatches_nothing(
    tenant_graph: TenantGraph,
    harness: Harness,
    eager_dispatch: None,
) -> None:
    with runtime_role(), pytest.raises(SyntheticRollbackError):
        _enqueue_then_rollback(tenant_graph)

    assert harness.adapter.sent == []
    assert harness.adapter.prepared_calls == []
    assert AuditEvent.objects.filter(event_type="comms.operation.enqueued").count() == 0
    _assert_gucs_empty()


def test_enqueue_outside_tenant_context_is_rejected(
    tenant_graph: TenantGraph,
    harness: Harness,
) -> None:
    with pytest.raises(IntegrationContextError):
        enqueue_operation(
            OperationRequest(
                channel=CHANNEL,
                provider=PROVIDER,
                clinic_id=_clinic_id(tenant_graph.organization_a),
                subject_type=SUBJECT_TYPE,
                subject_id=uuid4(),
                idempotency_key=uuid4(),
            )
        )


def test_forged_callback_is_rejected_before_scope_resolution(
    tenant_graph: TenantGraph,
    harness: Harness,
    eager_dispatch: None,
) -> None:
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.provider_reference is not None

    headers, body = _signed_callback(
        event_id="evt-forged",
        provider_reference=operation.provider_reference,
        status="delivered",
    )
    with pytest.raises(CallbackAuthenticationError):
        receive_provider_callback(
            provider=PROVIDER,
            headers={"x-synthetic-signature": "forged"},
            body=body,
        )
    with pytest.raises(CallbackAuthenticationError):
        receive_provider_callback(
            provider="unregistered-provider",
            headers=headers,
            body=body,
        )

    assert _operation(tenant_graph.organization_a, operation_id).status == (
        IntegrationOperation.Status.SUCCEEDED
    )
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "enqueued",
        "succeeded",
    ]
    _assert_gucs_empty()


def test_replayed_callback_produces_one_transition(
    tenant_graph: TenantGraph,
    harness: Harness,
    eager_dispatch: None,
) -> None:
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.provider_reference is not None
    headers, body = _signed_callback(
        event_id="evt-1",
        provider_reference=operation.provider_reference,
        status="delivered",
    )

    with runtime_role():
        assert (
            receive_provider_callback(provider=PROVIDER, headers=headers, body=body)
            == "applied"
        )
        assert (
            receive_provider_callback(provider=PROVIDER, headers=headers, body=body)
            == "duplicate"
        )
        other_headers, other_body = _signed_callback(
            event_id="evt-2",
            provider_reference=operation.provider_reference,
            status="failed",
        )
        assert (
            receive_provider_callback(
                provider=PROVIDER, headers=other_headers, body=other_body
            )
            == "duplicate"
        )

    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.DELIVERED
    assert operation.last_callback_event_id == "evt-1"
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "delivered",
        "enqueued",
        "succeeded",
    ]
    _assert_gucs_empty()


def test_callback_tenant_claims_are_ignored(
    tenant_graph: TenantGraph,
    harness: Harness,
    eager_dispatch: None,
) -> None:
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.provider_reference is not None
    headers, body = _signed_callback(
        event_id="evt-claim",
        provider_reference=operation.provider_reference,
        status="delivered",
        extra_claims={
            "organization_id": str(tenant_graph.organization_b),
            "tenant": str(tenant_graph.organization_b),
        },
    )

    with runtime_role():
        assert (
            receive_provider_callback(provider=PROVIDER, headers=headers, body=body)
            == "applied"
        )

    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.DELIVERED
    assert operation.organization_id == tenant_graph.organization_a
    assert _audit_verbs(tenant_graph.organization_b, operation_id) == []
    _assert_gucs_empty()


def test_unknown_provider_reference_is_rejected(
    tenant_graph: TenantGraph,
    harness: Harness,
) -> None:
    headers, body = _signed_callback(
        event_id="evt-unknown",
        provider_reference="ref-does-not-exist",
        status="delivered",
    )
    with runtime_role():
        assert (
            receive_provider_callback(provider=PROVIDER, headers=headers, body=body)
            == "rejected"
        )
    _assert_gucs_empty()


def test_revoked_authority_cancels_without_external_effect(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
    assert recorded_dispatch == [str(operation_id)]

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(tenant_graph.organization_a)],
        )
        UserClinicRole.objects.filter(
            user_id=tenant_graph.user_a,
            organization_id=tenant_graph.organization_a,
        ).delete()

    with runtime_role():
        assert _run_task(operation_id) == "cancelled"

    assert harness.adapter.sent == []
    assert harness.adapter.prepared_calls == []
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.CANCELLED
    assert operation.last_error == "authority_revoked"
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "cancelled",
        "enqueued",
    ]
    _assert_gucs_empty()


def test_bounded_attempts_fail_closed(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    harness.adapter.send_errors.extend(
        [TransientSendError("synthetic"), TransientSendError("synthetic")]
    )
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph,
            tenant_graph.organization_a,
            tenant_graph.user_a,
            max_attempts=2,
        )

    with runtime_role():
        assert _run_task(operation_id) == "retry"
        assert _operation(tenant_graph.organization_a, operation_id).status == (
            IntegrationOperation.Status.PENDING
        )
        assert _run_task(operation_id) == "retry"
        assert _run_task(operation_id) == "failed"

    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.FAILED
    assert operation.attempt_count == 2
    assert operation.last_error == "attempts_exhausted"
    assert len(harness.adapter.sent) == 2
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "enqueued",
        "failed",
    ]
    _assert_gucs_empty()


def test_abandoned_send_reconciles_without_resend(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    harness.adapter.crash_on_send = True
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph,
            tenant_graph.organization_a,
            tenant_graph.user_a,
            max_attempts=4,
        )
        with pytest.raises(SyntheticCrashError):
            execute_operation_task.run(operation_id=str(operation_id))

    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.IN_PROGRESS
    assert operation.attempt_count == 1
    assert len(harness.adapter.sent) == 1

    with runtime_role():
        assert _run_task(operation_id) == "reconcile"
        assert _run_task(operation_id) == "reconcile"
    assert len(harness.adapter.sent) == 1

    # A bare operation id is not a callback correlation key; only the
    # stored provider reference resolves an operation.
    headers, body = _signed_callback(
        event_id="evt-ambiguous",
        provider_reference=str(operation_id),
        status="delivered",
    )
    with runtime_role():
        assert (
            receive_provider_callback(provider=PROVIDER, headers=headers, body=body)
            == "rejected"
        )

    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.IN_PROGRESS
    assert operation.last_callback_event_id is None
    assert len(harness.adapter.sent) == 1
    _assert_gucs_empty()


def test_clinic_revoked_actor_cancels_without_external_effect(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    clinic_id = _clinic_id(tenant_graph.organization_a)
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
    assert recorded_dispatch == [str(operation_id)]

    # The actor keeps organization membership through a second clinic but
    # loses every membership in the operation's clinic before execution.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(tenant_graph.organization_a)],
        )
        other_clinic = Clinic.objects.create(
            organization_id=tenant_graph.organization_a,
            name="Other synthetic clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        UserClinicRole.objects.create(
            user_id=tenant_graph.user_a,
            organization_id=tenant_graph.organization_a,
            clinic=other_clinic,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
        UserClinicRole.objects.filter(
            user_id=tenant_graph.user_a,
            clinic_id=clinic_id,
        ).delete()

    with runtime_role():
        assert _run_task(operation_id) == "cancelled"

    assert harness.adapter.sent == []
    assert harness.adapter.prepared_calls == []
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.CANCELLED
    assert operation.last_error == "authority_revoked"
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "cancelled",
        "enqueued",
    ]
    _assert_gucs_empty()


def test_duplicate_deliveries_during_send_do_not_fail_active_send(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_send = harness.adapter.send
    duplicates: list[str] = []

    def send_with_duplicate_deliveries(
        prepared: object, *, operation_id: UUID
    ) -> SendResult:
        duplicates.extend(_run_task(operation_id) for _ in range(3))
        return original_send(prepared, operation_id=operation_id)

    monkeypatch.setattr(harness.adapter, "send", send_with_duplicate_deliveries)
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph,
            tenant_graph.organization_a,
            tenant_graph.user_a,
            max_attempts=3,
        )
        outcome = _run_task(operation_id)

    assert outcome == "succeeded"
    assert duplicates == ["reconcile", "reconcile", "reconcile"]
    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.SUCCEEDED
    assert operation.provider_reference is not None
    assert len(harness.adapter.sent) == 1
    assert _audit_verbs(tenant_graph.organization_a, operation_id) == [
        "enqueued",
        "succeeded",
    ]
    _assert_gucs_empty()


def test_callback_reference_collision_resolves_one_tenant(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with runtime_role():
        operation_a = _enqueue(
            tenant_graph, tenant_graph.organization_a, tenant_graph.user_a
        )
        operation_b = _enqueue(
            tenant_graph, tenant_graph.organization_b, tenant_graph.user_b
        )
    monkeypatch.setattr(
        harness.adapter,
        "send",
        lambda prepared, *, operation_id: SendResult(
            provider_reference=str(operation_b)
        ),
    )

    with runtime_role():
        assert _run_task(operation_a) == "succeeded"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT operation_id, organization_id "
                "FROM clinic_app.comms_operation_callback_scope(%s, %s)",
                [PROVIDER, str(operation_b)],
            )
            scopes = cursor.fetchall()
        headers, body = _signed_callback(
            event_id="evt-collision",
            provider_reference=str(operation_b),
            status="delivered",
        )
        outcome = receive_provider_callback(
            provider=PROVIDER, headers=headers, body=body
        )

    # The stored provider reference belongs to operation A alone; the
    # collision with operation B's id resolves exactly one tenant scope.
    assert scopes == [(operation_a, tenant_graph.organization_a)]
    assert outcome == "applied"
    operation_a_row = _operation(tenant_graph.organization_a, operation_a)
    operation_b_row = _operation(tenant_graph.organization_b, operation_b)
    assert operation_a_row.status == IntegrationOperation.Status.DELIVERED
    assert operation_a_row.last_callback_event_id == "evt-collision"
    assert operation_b_row.status == IntegrationOperation.Status.PENDING
    assert _audit_verbs(tenant_graph.organization_b, operation_b) == ["enqueued"]
    _assert_gucs_empty()


def test_ambiguous_send_exhausts_to_failed(
    tenant_graph: TenantGraph,
    harness: Harness,
    recorded_dispatch: list[str],
) -> None:
    harness.adapter.crash_on_send = True
    with runtime_role():
        operation_id = _enqueue(
            tenant_graph,
            tenant_graph.organization_a,
            tenant_graph.user_a,
            max_attempts=3,
        )
        with pytest.raises(SyntheticCrashError):
            execute_operation_task.run(operation_id=str(operation_id))

    with runtime_role():
        assert _run_task(operation_id) == "reconcile"
        assert _run_task(operation_id) == "reconcile"
        assert _run_task(operation_id) == "failed"

    operation = _operation(tenant_graph.organization_a, operation_id)
    assert operation.status == IntegrationOperation.Status.FAILED
    assert operation.attempt_count == 3
    assert operation.last_error == "attempts_exhausted"
    assert len(harness.adapter.sent) == 1
    _assert_gucs_empty()
