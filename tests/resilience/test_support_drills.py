"""Todo 78 support drills, run as agent-executed synthetic simulations.

Each drill follows docs/RUNBOOK.md "Support drills": the customer reports a
problem, the clinic administrator approves time-limited masked access, the
named support user reads the diagnostics screen, and the fix stays with the
customer or the owner. Every drill proves the same boundary: support sees
counts and closed labels only, never the identity, patient, payment or AI
content of the incident, and cannot perform the customer's recovery itself.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.billing.adapters import PaymentEventAuthenticationError
from apps.billing.models import PaymentEvent
from apps.core.integration import execute_operation
from apps.identity.enterprise_access import approve_support_access
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.recovery import (
    issue_recovery_codes,
    recover_authenticator,
    request_authenticator_reset,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.test import override_settings
from django.utils import timezone

from ai.support import SENTINEL, enqueue, seed
from identity.enterprise_support import enterprise_setup
from identity.permission_support import permission_context
from identity.test_support_diagnostics import _marks, _post, _support_client
from patient_service_support import runtime_role
from renewal.test_payment_reconciliation import (
    _deliver,
    _signed_event,
    charged,
    scripted,
    synthetic,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.billing.models import Invoice, PixCharge
    from apps.identity.models import User
    from django.test import Client
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

__all__ = ("charged", "scripted", "synthetic")
pytestmark = pytest.mark.django_db(transaction=True)
OK = 200


def _open_support(
    graph: RbacGraph,
) -> tuple[Client, User, Client, Callable[[], tuple[int, str]]]:
    """Customer approval, then a reader for the named support user's screen."""
    setup = enterprise_setup(graph)
    support, client = _support_client(graph)
    with permission_context(graph, graph.clinic_admin):
        grant = approve_support_access(
            clinic_id=graph.clinic_a,
            support_user_id=support.pk,
            purpose="Synthetic support drill",
            expires_at=timezone.now() + timedelta(minutes=30),
            session_key=setup.session_key,
        )
    return (
        setup.client,
        support,
        client,
        lambda: _post(client, clinic=graph.clinic_a, grant=grant.pk),
    )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_drill_customer_reports_a_lost_authenticator(
    rbac_graph: RbacGraph, record_property: Callable[[str, object], None]
) -> None:
    graph = rbac_graph
    customer_client, support, support_client, read = _open_support(graph)
    # Before the loss the customer saved recovery codes from a verified session.
    from identity.lifecycle_test_support import session_key  # noqa: PLC0415

    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        issued = issue_recovery_codes(
            clinic_id=graph.clinic_b, session_key=session_key(customer_client)
        )
    # Support reads the screen: no account, device or code appears.
    status, body = read()
    assert status == OK
    assert all(code not in body for code in issued.codes)
    # Support cannot reset the customer's authenticator from its own account.
    with (
        runtime_role(),
        pytest.raises((TenantAccessDeniedError, LifecycleDeniedError)),
        tenant_context(support.pk, graph.organization_b),
    ):
        request_authenticator_reset(
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            session_key=session_key(support_client),
        )
    # The customer recovers with one saved code; the code works only once.
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        recovered = recover_authenticator(
            clinic_id=graph.clinic_b, code=issued.codes[0]
        )
        replayed = recover_authenticator(clinic_id=graph.clinic_b, code=issued.codes[0])
    assert recovered
    assert not replayed
    record_property(
        "support_drill",
        json.dumps(
            {
                "drill": "lost-authenticator",
                "support_screen": status,
                "support_reset_attempt": "refused",
                "customer_recovery": recovered,
                "code_replay": replayed,
            },
            sort_keys=True,
        ),
    )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_drill_failed_payment_callback(
    rbac_graph: RbacGraph,
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    record_property: Callable[[str, object], None],
) -> None:
    setup, invoice, charge = charged
    *_parties, read = _open_support(rbac_graph)
    # A callback with a forged signature arrives for the clinic's PIX charge.
    headers, body = _signed_event(
        event_id="evt-drill-forged", provider_reference=charge.provider_reference
    )
    with runtime_role(), pytest.raises(PaymentEventAuthenticationError):
        _deliver({"x-synthetic-pix-signature": "forged"}, body)
    # Support sees provider states and counts only, never the charge itself.
    status, page = read()
    assert status == OK
    marks = _marks(page)
    assert "data-support-ready" in marks
    assert "data-support-provider" in marks
    for hidden in (str(invoice.pk), charge.provider_reference, "12345", "123,45"):
        assert hidden not in page
    # The finance owner's correctly signed event still settles exactly once.
    with runtime_role():
        assert _deliver(headers, body) == "settled"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        events = PaymentEvent.objects.count()
    assert invoice.state == "paid"
    assert events == 1
    record_property(
        "support_drill",
        json.dumps(
            {
                "drill": "failed-payment-callback",
                "forged_callback": "rejected",
                "support_screen": status,
                "invoice_state": invoice.state,
                "payment_events": events,
            },
            sort_keys=True,
        ),
    )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_drill_ai_outage(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    record_property: Callable[[str, object], None],
) -> None:
    # One admitted AI request is on the action outbox when the model goes down.
    world = seed(rbac_graph, monkeypatch)
    result = enqueue(world)
    assert result.operation_id is not None
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "down"}
    with runtime_role():
        outcome = execute_operation(result.operation_id)
    assert outcome == "failed"
    *_parties, read = _open_support(rbac_graph)
    # Support sees one failed action and the model capability state, no text.
    status, page = read()
    assert status == OK
    marks = _marks(page)
    assert "data-support-queue" in marks
    assert "data-support-provider" in marks
    assert SENTINEL not in page
    assert str(result.invocation_id) not in page
    record_property(
        "support_drill",
        json.dumps(
            {
                "drill": "ai-outage",
                "operation": outcome,
                "support_screen": status,
                "content_shown": False,
            },
            sort_keys=True,
        ),
    )
