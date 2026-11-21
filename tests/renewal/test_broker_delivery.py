"""Real broker/worker delivery proof for the renewal release candidate.

Unlike the in-process integration boundary tests, these cases dispatch
through a real task-owned Redis broker into a separately spawned Celery
worker that connects to PostgreSQL as ``clinic_app``. The whole module
requires ``CLINIC_BROKER_GATE=required``. The gate must provide an owned
Redis endpoint or a ``redis-server`` binary; a missing prerequisite fails it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.adapters import CallbackAuthenticationError
from apps.comms.models import AppointmentReminder, IntegrationOperation
from apps.comms.tasks import (
    dispatch_due_reminders,
    recover_pending_operations,
)
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    receive_provider_callback,
    register_callback_authenticator,
)
from apps.intake.contacts import (
    enqueue_automated_message,
    save_contact_destination,
    set_purpose_channel,
    verify_contact,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role
from renewal.broker_support import (
    QUEUE,
    broker,
    broker_url,
    delivery,
    worker,
)
from renewal.test_integration_boundary import SECRET, SyntheticAuthenticator
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper
    from redis import Redis

    from conftest import RbacGraph
    from renewal.broker_support import Worker
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("CLINIC_BROKER_GATE", "") != "required",
        reason="requires the dedicated broker gate (CLINIC_BROKER_GATE=required)",
    ),
]

__all__ = ("broker", "broker_url", "worker")
CHANNEL = "sms"
PURPOSE = "appointment_reminder"


def _contact(setup: AppointmentSetup, channel: str) -> None:
    save_contact_destination(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        channel=channel,
        destination="+5511999990001",
        expected_version=None,
    )
    verify_contact(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        channel=channel,
        expected_version=1,
    )
    set_purpose_channel(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        purpose=PURPOSE,
        channel=channel,
    )


def _operation(organization_id: UUID, operation_id: UUID) -> IntegrationOperation:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        operation = IntegrationOperation.objects.filter(pk=operation_id).first()
    assert operation is not None
    return operation


def _status(setup: AppointmentSetup, operation_id: UUID) -> str:
    return _operation(setup.organization_id, operation_id).status


def _event_count(setup: AppointmentSetup, operation_id: UUID, event: str) -> int:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        return AuditEvent.objects.filter(
            event_type=event,
            payload__request_id=str(operation_id),
        ).count()


def make_due(setup: AppointmentSetup, operation_id: UUID) -> None:
    """Accelerate the stored schedule the same fixture-only way tests do."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        cursor.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET not_before=statement_timestamp()-interval '1 minute' "
            "WHERE id=%s",
            [operation_id],
        )


def book_reminder(setup: AppointmentSetup, channel: str) -> UUID:
    """Book an appointment through the real services; the trigger writes the op."""
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _contact(setup, channel)
        appointment = create_synthetic_appointment(setup)
        reminder = AppointmentReminder.objects.select_related("operation").get(
            appointment=appointment
        )
        return reminder.operation_id


@pytest.fixture
def setup(rbac_graph: RbacGraph, settings: SettingsWrapper) -> AppointmentSetup:
    settings.COMMS_SYNTHETIC_CHANNELS = ("email", "sms", "whatsapp")
    return seed_appointment_setup(rbac_graph)


def test_beat_dispatch_reaches_real_worker_and_delivers_once(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    with worker.start(), runtime_role():
        assert dispatch_due_reminders() == 1
        assert delivery(broker, operation_id).outcome == "succeeded"
    operation = _operation(setup.organization_id, operation_id)
    assert operation.provider_reference == f"synthetic:{CHANNEL}:{operation_id}"
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


_ROLLED_BACK = "synthetic rollback"


def test_rolled_back_operation_is_never_delivered(
    setup: AppointmentSetup, broker: Redis
) -> None:
    rolled_back = uuid4()
    with (  # noqa: PT012 - the atomic block is the unit under test
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(RuntimeError, match="synthetic rollback"),
        transaction.atomic(),
    ):
        _contact(setup, CHANNEL)
        enqueue_automated_message(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose=PURPOSE,
            provider=f"reminder-{CHANNEL}-v1",
            idempotency_key=rolled_back,
        )
        raise RuntimeError(_ROLLED_BACK)
    assert IntegrationOperation.objects.filter(idempotency_key=rolled_back).count() == 0
    # No consumer is running: an erroneous publication cannot disappear.
    assert broker.llen(QUEUE) == 0


def test_on_commit_dispatch_reaches_worker(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    """A committed enqueue publishes on commit; the worker executes the task.

    The provider is deliberately unregistered, so the only possible outcome
    is a terminal ``adapter_unavailable`` failure recorded by the worker
    process — the pending-to-failed transition proves the task crossed the
    real broker boundary.
    """
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation_id = enqueue_operation(
            OperationRequest(
                channel=CHANNEL,
                provider="synthetic-unregistered",
                clinic_id=setup.clinic_id,
                subject_type="intake.patient_clinic_enrollment",
                subject_id=setup.enrollment_id,
                idempotency_key=uuid4(),
                max_attempts=2,
            )
        )
        assert broker.llen(QUEUE) == 0
    with worker.start():
        assert delivery(broker, operation_id).outcome == "failed"
    operation = _operation(setup.organization_id, operation_id)
    assert operation.last_error == "adapter_unavailable"
    assert _event_count(setup, operation_id, "comms.operation.failed") == 1


def test_lost_dispatch_is_recovered_by_pending_scan(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    """A discarded broker message does not lose the committed outbox row.

    Remove the published Redis message before starting a consumer, modeling
    loss of a volatile queue on restart without restarting the host broker.
    """
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    with runtime_role():
        assert dispatch_due_reminders() == 1
    assert broker.llen(QUEUE) == 1
    assert broker.delete(QUEUE) == 1
    assert _status(setup, operation_id) == "pending"
    with worker.start(), runtime_role():
        assert recover_pending_operations() >= 1
        assert delivery(broker, operation_id).outcome == "succeeded"
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


def test_duplicate_dispatch_reconciles_to_one_delivery(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    with runtime_role():
        dispatch_due_reminders()
    duplicate = execute_operation_task.apply_async(
        kwargs={"operation_id": str(operation_id)}
    )
    with worker.start():
        assert delivery(broker, operation_id).outcome == "succeeded"
        replay = delivery(broker, operation_id)
        assert (replay.task_id, replay.outcome) == (duplicate.id, "skipped")
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


def test_completed_operation_replay_after_worker_restart_is_skipped(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    with runtime_role():
        dispatch_due_reminders()
    with worker.start() as process:
        assert delivery(broker, operation_id).outcome == "succeeded"
        process.kill()
    replay = execute_operation_task.apply_async(
        kwargs={"operation_id": str(operation_id)}
    )
    with worker.start():
        completed = delivery(broker, operation_id)
        assert (completed.task_id, completed.outcome) == (replay.id, "skipped")
    assert _status(setup, operation_id) == "succeeded"
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


def test_revoked_contact_cancels_at_send_time(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        set_purpose_channel(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose=PURPOSE,
            channel=None,
        )
    with runtime_role():
        dispatch_due_reminders()
    with worker.start():
        assert delivery(broker, operation_id).outcome == "cancelled"
    assert _status(setup, operation_id) == "cancelled"
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 0


def test_retry_ceiling_reaches_terminal_failure(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    ceiling = _operation(setup.organization_id, operation_id).max_attempts
    with worker.start(COMMS_SYNTHETIC_FAILURE_CHANNELS=CHANNEL):
        with runtime_role():
            assert dispatch_due_reminders() == 1
        for _ in range(ceiling):
            assert delivery(broker, operation_id).outcome == "retry"
            assert _status(setup, operation_id) == "pending"
            execute_operation_task.apply_async(
                kwargs={"operation_id": str(operation_id)}
            )
        assert delivery(broker, operation_id).outcome == "failed"
    operation = _operation(setup.organization_id, operation_id)
    assert operation.status == "failed"
    assert operation.attempt_count == ceiling
    assert operation.last_error == "attempts_exhausted"
    assert _event_count(setup, operation_id, "comms.operation.failed") == 1


def test_forged_duplicate_and_terminal_callbacks_behave(
    setup: AppointmentSetup, broker: Redis, worker: Worker
) -> None:
    operation_id = book_reminder(setup, CHANNEL)
    make_due(setup, operation_id)
    with worker.start(), runtime_role():
        dispatch_due_reminders()
        assert delivery(broker, operation_id).outcome == "succeeded"
    operation = _operation(setup.organization_id, operation_id)
    authenticator = SyntheticAuthenticator()
    authenticator.provider = operation.provider
    register_callback_authenticator(authenticator)
    body = json.dumps(
        {
            "event_id": f"synthetic-receipt-{operation_id}",
            "provider_reference": operation.provider_reference,
            "status": "delivered",
        }
    ).encode()
    with pytest.raises(CallbackAuthenticationError):
        receive_provider_callback(
            provider=operation.provider,
            headers={"x-synthetic-signature": "forged"},
            body=body,
        )
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    headers = {"x-synthetic-signature": signature}
    with runtime_role():
        first = receive_provider_callback(
            provider=operation.provider, headers=headers, body=body
        )
        second = receive_provider_callback(
            provider=operation.provider, headers=headers, body=body
        )
    assert first == "applied"
    assert second == "duplicate"
    assert _status(setup, operation_id) == "delivered"
    assert _event_count(setup, operation_id, "comms.operation.delivered") == 1
