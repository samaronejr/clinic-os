"""Reminder contracts through real RLS, booking hooks and integration boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from apps.comms.adapters import OperationScope, PermanentSendError
from apps.comms.capabilities import channel_capability
from apps.comms.channel_adapters import (
    EmailReminderAdapter,
    SMSReminderAdapter,
    WhatsAppReminderAdapter,
)
from apps.comms.models import AppointmentReminder, IntegrationOperation
from apps.comms.services import reminder_send_eligible
from apps.comms.tasks import dispatch_due_reminders
from apps.comms.tasks import execute_operation as task
from apps.core import integration
from apps.core.integration import (
    execute_operation,
    receive_provider_callback,
    register_callback_authenticator,
    register_send_adapter,
)
from apps.intake.contacts import (
    save_contact_destination,
    set_purpose_channel,
    verify_contact,
)
from apps.scheduling.services import (
    AppointmentLocalRange,
    cancel_appointment,
    create_availability,
    reschedule_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.utils import timezone

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from renewal.test_integration_boundary import SECRET, SyntheticAuthenticator

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.adapters import SendAdapter
    from pytest_django.fixtures import SettingsWrapper

    from appointment_service_support import AppointmentSetup
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ADAPTERS = {
    "email": EmailReminderAdapter,
    "sms": SMSReminderAdapter,
    "whatsapp": WhatsAppReminderAdapter,
}
DESTINATIONS = {
    "email": "synthetic@example.invalid",
    "sms": "+5511999990001",
    "whatsapp": "+5511999990002",
}


@pytest.fixture
def setup(rbac_graph: RbacGraph, settings: SettingsWrapper) -> AppointmentSetup:
    settings.COMMS_SYNTHETIC_CHANNELS = tuple(ADAPTERS)
    for adapter_class in ADAPTERS.values():
        register_send_adapter(adapter_class())
    return seed_appointment_setup(rbac_graph)


def _contact(setup: AppointmentSetup, channel: str) -> None:
    save_contact_destination(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        channel=channel,
        destination=DESTINATIONS[channel],
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
        purpose="appointment_reminder",
        channel=channel,
    )


def _book(setup: AppointmentSetup, channel: str) -> tuple[UUID, UUID]:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _contact(setup, channel)
        appointment = create_synthetic_appointment(setup)
        reminder = AppointmentReminder.objects.select_related("operation").get(
            appointment=appointment
        )
        assert reminder.operation.actor_id == setup.practitioner_id
        assert reminder.operation.not_before == appointment.start_at - timedelta(
            hours=24
        )
        assert reminder.timezone == "America/Sao_Paulo"
        return appointment.pk, reminder.operation_id


def _due(
    setup: AppointmentSetup, operation_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        due = IntegrationOperation.objects.get(pk=operation_id).not_before
    assert due is not None
    monkeypatch.setattr(
        "apps.core.integration.timezone", SimpleNamespace(now=lambda: due)
    )


def _operation(setup: AppointmentSetup, operation_id: UUID) -> IntegrationOperation:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        return IntegrationOperation.objects.get(pk=operation_id)


@pytest.mark.parametrize(
    "channel",
    ["email", "sms", "whatsapp"],
    ids=["email-contract", "sms-contract", "whatsapp-contract"],
)
def test_channel_delivery_contract(
    setup: AppointmentSetup, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    appointment_id, operation_id = _book(setup, channel)
    with runtime_role():
        assert execute_operation(operation_id) == "skipped"
    assert _operation(setup, operation_id).attempt_count == 0
    _due(setup, operation_id, monkeypatch)
    adapter = ADAPTERS[channel]()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = IntegrationOperation.objects.get(pk=operation_id)
        message = adapter.prepare(operation)
        assert message.channel == channel
        assert message.destination == DESTINATIONS[channel]
        assert "09:00" in message.body
        assert "02/06/2035" in message.body
        assert str(appointment_id) not in message.body
        assert "Synthetic Booking Persona" not in message.body
        assert set(message.__dataclass_fields__) == {
            "channel",
            "destination",
            "body",
            "template_version",
        }
    with runtime_role():
        assert not connection.in_atomic_block
        assert execute_operation(operation_id) == "succeeded"
        assert execute_operation(operation_id) == "skipped"
    operation = _operation(setup, operation_id)
    assert operation.attempt_count == 1
    assert operation.provider_reference == f"synthetic:{channel}:{operation_id}"
    authenticator = SyntheticAuthenticator()
    authenticator.provider = adapter.provider
    register_callback_authenticator(authenticator)
    body = json.dumps(
        {
            "event_id": f"receipt-{operation_id}",
            "provider_reference": operation.provider_reference,
            "status": "delivered",
        }
    ).encode()
    headers = {
        "x-synthetic-signature": hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    }
    with runtime_role():
        assert (
            receive_provider_callback(
                provider=adapter.provider, headers=headers, body=body
            )
            == "applied"
        )
        assert (
            receive_provider_callback(
                provider=adapter.provider, headers=headers, body=body
            )
            == "duplicate"
        )
    assert _operation(setup, operation_id).status == "delivered"


@pytest.mark.parametrize("channel", ["email", "sms", "whatsapp"])
def test_real_channel_blocked_independently(
    setup: AppointmentSetup,
    channel: str,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operation_id = _book(setup, channel)
    settings.COMMS_SYNTHETIC_CHANNELS = tuple(c for c in ADAPTERS if c != channel)
    assert not channel_capability(channel).real_enabled
    assert not channel_capability(channel).synthetic_enabled
    assert all(
        channel_capability(c).synthetic_enabled for c in ADAPTERS if c != channel
    )
    _due(setup, operation_id, monkeypatch)
    with runtime_role():
        assert execute_operation(operation_id) == "failed"
    operation = _operation(setup, operation_id)
    assert operation.provider_reference is None
    assert operation.last_error == "provider_rejected"


@pytest.mark.parametrize(
    "mutation",
    ["opt-out", "destination", "template", "cancel", "reschedule", "opt-out-in"],
)
def test_pending_notice_invalidated(
    setup: AppointmentSetup,
    mutation: str,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appointment_id, operation_id = _book(setup, "email")
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        if mutation in {"opt-out", "opt-out-in"}:
            set_purpose_channel(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                purpose="appointment_reminder",
                channel=None,
            )
            if mutation == "opt-out-in":
                set_purpose_channel(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    purpose="appointment_reminder",
                    channel="email",
                )
        elif mutation == "destination":
            save_contact_destination(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                channel="email",
                destination="changed@example.invalid",
                expected_version=1,
            )
            verify_contact(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                channel="email",
                expected_version=2,
            )
        elif mutation == "template":
            settings.COMMS_REVOKED_REMINDER_TEMPLATES = ("email:1",)
        elif mutation == "cancel":
            cancel_appointment(appointment_id=appointment_id, reason="patient_request")
        else:
            reschedule_appointment(
                appointment_id=appointment_id,
                local_range=AppointmentLocalRange(
                    "2035-06-02T10:00", "2035-06-02T11:00"
                ),
            )
            reminders = AppointmentReminder.objects.filter(
                appointment_id=appointment_id
            )
            assert reminders.count() == 2
            assert (
                reminders.exclude(operation_id=operation_id).get().operation.status
                == "pending"
            )
    _due(setup, operation_id, monkeypatch)
    with runtime_role():
        assert execute_operation(operation_id) in {"cancelled", "skipped"}
    operation = _operation(setup, operation_id)
    assert operation.status == "cancelled"
    assert operation.provider_reference is None


def test_retry_bound_and_duplicate_job(
    setup: AppointmentSetup, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operation_id = _book(setup, "sms")
    settings.COMMS_SYNTHETIC_FAILURE_CHANNELS = ("sms",)
    _due(setup, operation_id, monkeypatch)
    with runtime_role():
        for _ in range(3):
            assert execute_operation(operation_id) == "retry"
        assert execute_operation(operation_id) == "failed"
        assert execute_operation(operation_id) == "skipped"
    operation = _operation(setup, operation_id)
    assert operation.attempt_count == operation.max_attempts == 3
    assert operation.last_error == "attempts_exhausted"
    assert operation.provider_reference is None


def test_no_preference_no_notice_and_replay_stable(setup: AppointmentSetup) -> None:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        assert not AppointmentReminder.objects.exists()
        assert (
            create_synthetic_appointment(
                setup, idempotency_key=appointment.idempotency_key
            ).pk
            == appointment.pk
        )
        assert not AppointmentReminder.objects.exists()


def test_transaction_rollback_removes_notices(setup: AppointmentSetup) -> None:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _contact(setup, "whatsapp")
        with transaction.atomic():
            create_synthetic_appointment(setup)
            assert AppointmentReminder.objects.count() == 1
            transaction.set_rollback(True)
        assert not AppointmentReminder.objects.exists()
        assert not IntegrationOperation.objects.exists()


def test_clinic_scope_and_reminder_rls(
    setup: AppointmentSetup, rbac_graph: RbacGraph
) -> None:
    _, operation_id = _book(setup, "email")
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert not reminder_send_eligible(
            OperationScope(
                operation_id, setup.organization_id, rbac_graph.clinic_b, setup.actor_id
            )
        )
    with runtime_role():
        assert not AppointmentReminder.objects.exists()


def test_dispatcher_entrypoint_has_no_early_jobs(
    setup: AppointmentSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _book(setup, "email")
    dispatched: list[str] = []
    monkeypatch.setattr(
        task,
        "apply_async",
        lambda **kw: dispatched.append(kw["kwargs"]["operation_id"]),
    )
    with runtime_role():
        assert dispatch_due_reminders() == 0
    assert dispatched == []


def test_wrong_channel_message_rejected(setup: AppointmentSetup) -> None:
    _, operation_id = _book(setup, "email")
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = IntegrationOperation.objects.get(pk=operation_id)
        with pytest.raises(PermanentSendError):
            SMSReminderAdapter().prepare(operation)


def test_booking_within_24_hours_has_no_late_reminder(setup: AppointmentSetup) -> None:
    start = (
        timezone.now().astimezone(ZoneInfo("America/Sao_Paulo")) + timedelta(hours=2)
    ).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    start_local, end_local = (
        value.strftime("%Y-%m-%dT%H:%M") for value in (start, end)
    )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _contact(setup, "sms")
        create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
            start_local=start_local,
            end_local=end_local,
            idempotency_key=uuid4(),
        )
        create_synthetic_appointment(
            setup, start_local=start_local, end_local=end_local
        )
        assert not AppointmentReminder.objects.exists()


@pytest.mark.parametrize("mutation", ["cancel", "reschedule", "optout", "destination"])
def test_committed_mutation_after_prepare_cannot_send(
    setup: AppointmentSetup,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    appointment_id, operation_id = _book(setup, "email")
    _due(setup, operation_id, monkeypatch)
    original = integration._send_and_finish

    def at_cut(
        scope: OperationScope, adapter: SendAdapter, prepared: object
    ) -> integration.ExecutionResult:
        assert not connection.in_atomic_block
        with tenant_context(setup.actor_id, setup.organization_id):
            if mutation == "cancel":
                cancel_appointment(
                    appointment_id=appointment_id, reason="clinic_request"
                )
            elif mutation == "reschedule":
                reschedule_appointment(
                    appointment_id=appointment_id,
                    local_range=AppointmentLocalRange(
                        "2035-06-02T10:00", "2035-06-02T11:00"
                    ),
                )
            elif mutation == "optout":
                set_purpose_channel(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    purpose="appointment_reminder",
                    channel=None,
                )
            else:
                save_contact_destination(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    channel="email",
                    destination="changed@example.invalid",
                    expected_version=1,
                )
        return original(scope, adapter, prepared)

    def forbidden_send(
        self: EmailReminderAdapter, prepared: object, *, operation_id: UUID
    ) -> None:
        pytest.fail("committed mutation overtaken by reminder send")

    monkeypatch.setattr(integration, "_send_and_finish", at_cut)
    monkeypatch.setattr(EmailReminderAdapter, "send", forbidden_send)
    with runtime_role():
        assert execute_operation(operation_id) == "cancelled"
    assert _operation(setup, operation_id).provider_reference is None


def test_reminder_resolver_and_snapshot_acl(setup: AppointmentSetup) -> None:
    _book(setup, "email")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proname, prosecdef, proowner::regrole::text, proconfig, "
            "has_function_privilege('clinic_app', oid, 'EXECUTE') "
            "FROM pg_proc WHERE proname IN "
            "('comms_schedule_reminders_v1', 'comms_due_reminders_v1')"
        )
        rows = {row[0]: row[1:] for row in cursor.fetchall()}
        assert rows == {
            "comms_schedule_reminders_v1": (
                True,
                "clinic_resolver",
                ["search_path=pg_catalog, clinic_app, pg_temp"],
                False,
            ),
            "comms_due_reminders_v1": (
                True,
                "clinic_resolver",
                ["search_path=pg_catalog, clinic_app, pg_temp"],
                True,
            ),
        }
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_name='comms_appointmentreminder'"
        )
        assert cursor.fetchall() == [("SELECT",)]
        cursor.execute(
            "SELECT has_column_privilege('clinic_app', "
            "'clinic_app.comms_integrationoperation', 'not_before', 'UPDATE')"
        )
        assert cursor.fetchone() == (False,)


def test_dispatcher_recovers_committed_due_job(
    setup: AppointmentSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operation_id = _book(setup, "email")
    # Fixture-only accelerated schedule; the worker still connects as clinic_app.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        cursor.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET not_before=statement_timestamp()-interval '1 minute' WHERE id=%s",
            [operation_id],
        )
    dispatched: list[str] = []
    monkeypatch.setattr(
        task,
        "apply_async",
        lambda **kw: dispatched.append(kw["kwargs"]["operation_id"]),
    )
    with runtime_role():
        assert dispatch_due_reminders() == 1
    assert dispatched == [str(operation_id)]
    assert _operation(setup, operation_id).status == "pending"


@pytest.mark.parametrize(
    "state", ["pending", "blocked", "cancelled", "succeeded", "failed"]
)
def test_status_screen_uses_stored_facts_without_destinations(
    setup: AppointmentSetup,
    rbac_graph: RbacGraph,
    state: str,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operation_id = _book(setup, "email")
    client, _ = receptionist_client(rbac_graph)
    if state == "blocked":
        settings.COMMS_SYNTHETIC_CHANNELS = ()
    elif state == "cancelled":
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            set_purpose_channel(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                purpose="appointment_reminder",
                channel=None,
            )
    elif state in {"succeeded", "failed"}:
        _due(setup, operation_id, monkeypatch)
        if state == "failed":
            settings.COMMS_SYNTHETIC_CHANNELS = ()
        with runtime_role():
            assert execute_operation(operation_id) == state
    with runtime_role():
        response = client.get(f"/scheduling/clinics/{setup.clinic_id}/reminders/")
        denied = client.get(f"/scheduling/clinics/{rbac_graph.clinic_b}/reminders/")
    assert response.status_code == 200
    assert response.context["rows"][0]["state"] == state
    assert DESTINATIONS["email"].encode() not in response.content
    assert "no-store" in response.headers["Cache-Control"]
    assert denied.status_code == 404
    assert str(operation_id).encode() not in denied.content
