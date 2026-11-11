"""Todo 72: a faulted messaging provider never blocks scheduling or resends."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms.adapters import (
    AmbiguousSendError,
    SendResult,
    TransientSendError,
)
from apps.comms.channel_adapters import EmailReminderAdapter
from apps.comms.inbox_adapters import (
    EmailInboxAdapter,
    OutboundMessage,
    PortalInboxAdapter,
)
from apps.core.integration import execute_operation, register_send_adapter
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from renewal.test_reminders import _book, _due, _operation
from renewal.test_reminders import setup as reminder_setup
from scheduling.appointment_service_support import create_synthetic_appointment

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from scheduling.appointment_service_support import AppointmentSetup

__all__ = ("reminder_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


class CountingEmailAdapter(EmailReminderAdapter):
    """The real email reminder contract, counting provider calls."""

    def __init__(self) -> None:
        self.sends = 0

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        self.sends += 1
        return super().send(prepared, operation_id=operation_id)


@pytest.mark.parametrize("mode", ["down", "slow"])
def test_unreachable_messaging_retries_and_scheduling_continues(
    reminder_setup: AppointmentSetup,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    mode: str,
) -> None:
    setup = reminder_setup
    adapter = CountingEmailAdapter()
    register_send_adapter(adapter)
    _, operation_id = _book(setup, "email")
    _due(setup, operation_id, monkeypatch)
    settings.CLINIC_SYNTHETIC_FAULTS = {"messaging": mode}
    with runtime_role():
        assert execute_operation(operation_id) == "retry"
    held = _operation(setup, operation_id)
    assert (held.status, held.last_error, held.provider_reference) == (
        "pending",
        "transient_send",
        None,
    )
    # Booking keeps working while the provider is faulted.
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
        assert booked.status == "scheduled"
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    with runtime_role():
        assert execute_operation(operation_id) == "succeeded"
        assert execute_operation(operation_id) == "skipped"
    sent = _operation(setup, operation_id)
    assert sent.provider_reference == f"synthetic:email:{operation_id}"
    assert adapter.sends == 2


def test_an_ambiguous_message_is_held_for_reconciliation_and_never_resent(
    reminder_setup: AppointmentSetup,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    setup = reminder_setup
    adapter = CountingEmailAdapter()
    register_send_adapter(adapter)
    _, operation_id = _book(setup, "email")
    _due(setup, operation_id, monkeypatch)
    settings.CLINIC_SYNTHETIC_FAULTS = {"messaging": "ambiguous"}
    with runtime_role():
        assert execute_operation(operation_id) == "reconcile"
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    with runtime_role():
        assert execute_operation(operation_id) == "reconcile"
    held = _operation(setup, operation_id)
    assert (held.status, held.provider_reference) == ("in_progress", None)
    assert adapter.sends == 1


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("down", TransientSendError),
        ("slow", TransientSendError),
        ("ambiguous", AmbiguousSendError),
    ],
)
def test_inbox_replies_share_the_messaging_fault_vocabulary(
    settings: SettingsWrapper, mode: str, error: type[Exception]
) -> None:
    settings.COMMS_SYNTHETIC_CHANNELS = ("email",)
    settings.CLINIC_SYNTHETIC_FAULTS = {"messaging": mode}
    operation_id = uuid4()
    message = OutboundMessage(
        "email", "synthetic@example.invalid", "Mensagem sintética.", {}
    )
    with pytest.raises(error):
        EmailInboxAdapter().send(message, operation_id=operation_id)
    # Portal delivery stays inside the system, so no provider fault applies.
    portal = OutboundMessage("portal", "", "Mensagem sintética.", {})
    assert PortalInboxAdapter().send(portal, operation_id=operation_id) == SendResult(
        f"portal:{operation_id}"
    )
