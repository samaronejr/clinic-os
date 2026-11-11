"""Signed partner webhooks through the action outbox: capture, sign, retry, revoke."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation as execute_as_owner
from apps.interop.external_ids import external_id
from apps.interop.management import list_deliveries
from apps.interop.models import ApiClient, WebhookAttempt, WebhookSubscription
from apps.interop.webhook_signature import (
    WebhookSignatureError,
    signature_header,
    verify,
)
from apps.interop.webhook_transport import SYNTHETIC_TRANSPORT
from apps.subscriptions.services import cancel_subscription
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import Client

from identity.permission_support import owner_context
from interop.support import (
    access_token,
    api_get,
    api_post,
    interop_secrets,
    local_bucket,
    make_client,
    org_admin,
    request_token,
    webhook_transport,
)
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
    seed_appointment_setup,
)
from subscriptions.support import event

if TYPE_CHECKING:
    from pathlib import Path

    from apps.interop.management import IssuedCredential

    from interop.support import LocalBucket
    from rbac_fixtures import RbacGraph

__all__ = ("interop_secrets", "local_bucket", "webhook_transport")
pytestmark = pytest.mark.django_db(transaction=True)

ENDPOINT = "https://parceiro.example.org/hooks/clinic"
EVENTS = ["appointment.created", "appointment.status_changed"]


def _subscribe(
    graph: RbacGraph, admin: UUID, events: list[str] = EVENTS
) -> tuple[IssuedCredential, str, dict[str, object]]:
    credential = make_client(graph, admin, ["appointments:read", "webhooks:manage"])
    token = access_token(credential)
    created = api_post(
        Client(),
        token,
        "/api/v1/webhook-subscriptions",
        {"url": ENDPOINT, "events": events},
        key=f"subscribe-{uuid4().hex}",
    )
    assert created.status_code == 201, created.content
    return credential, token, created.json()


def _book(graph: RbacGraph, setup: AppointmentSetup | None = None) -> UUID:
    chosen = setup or seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        if setup is None:
            return create_synthetic_appointment(chosen).pk
        return create_synthetic_appointment(
            chosen, start_local="2035-06-02T11:00", end_local="2035-06-02T11:30"
        ).pk


def execute_operation(operation_id: UUID) -> str:
    """Run the worker boundary exactly as the runtime role does."""
    with runtime_role():
        return execute_as_owner(operation_id)


def _operations(graph: RbacGraph) -> list[IntegrationOperation]:
    with owner_context(graph.organization_a):
        return list(
            IntegrationOperation.objects.filter(
                subject_type="interop.webhook_delivery"
            ).order_by("created_at")
        )


def test_booking_is_signed_and_delivered_through_the_action_outbox(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    credential, _token, subscription = _subscribe(graph, admin)
    appointment = _book(graph)
    (operation,) = _operations(graph)
    assert operation.kind == "action"
    assert operation.channel == ""
    assert operation.provider == "interop-webhook-v1"
    assert operation.actor_id == admin
    assert operation.status == "pending"
    stored = json.dumps(
        {
            field.name: str(getattr(operation, field.attname))
            for field in IntegrationOperation._meta.concrete_fields
        }
    )
    assert "parceiro.example.org" not in stored
    assert "Synthetic Booking Persona" not in stored
    assert execute_operation(operation.pk) == "succeeded"
    (request,) = SYNTHETIC_TRANSPORT.requests
    assert request.url == ENDPOINT
    verify(
        request.headers["Clinic-Signature"],
        request.body,
        str(subscription["secret"]),
        now=int(time.time()),
    )
    payload = json.loads(request.body)
    assert payload["type"] == "appointment.created"
    assert payload["id"] == request.headers["Clinic-Webhook-Id"]
    assert payload["data"]["appointment"] == external_id(
        "appointment", graph.organization_a, appointment
    )
    assert payload["data"]["status"] == "booked"
    assert str(appointment) not in request.body.decode()
    with owner_context(graph.organization_a):
        operation.refresh_from_db()
    with runtime_role(), tenant_context(admin, graph.organization_a):
        (attempt,) = WebhookAttempt.objects.all()
    assert operation.status == "succeeded"
    assert operation.provider_reference == f"webhook:{payload['id']}"
    assert (attempt.attempt, attempt.outcome, attempt.http_status) == (
        1,
        "delivered",
        200,
    )
    with runtime_role(), tenant_context(admin, graph.organization_a):
        (logged,) = list_deliveries(
            clinic_id=graph.clinic_a, subscription_id=UUID(str(subscription["id"]))
        )
    assert logged.state == "delivered"
    assert logged.event_type == "appointment.created"
    assert [line.outcome for line in logged.attempts] == ["delivered"]
    assert credential.client_id


def test_failed_attempts_back_off_in_the_outbox_and_then_deliver(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    _subscribe(graph, admin)
    _book(graph)
    (operation,) = _operations(graph)
    SYNTHETIC_TRANSPORT.script.extend([503, "timeout"])
    assert execute_operation(operation.pk) == "retry"
    with owner_context(graph.organization_a):
        operation.refresh_from_db()
    assert operation.status == "pending"
    assert operation.last_error == "transient_send"
    assert operation.not_before is not None
    first_wait = operation.not_before - operation.updated_at
    assert timedelta(seconds=59) <= first_wait <= timedelta(seconds=61)
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.comms_recover_pending_v1()")
        due = {row[0] for row in cursor.fetchall()}
    assert operation.pk not in due
    # A claim before not_before is skipped without another attempt.
    assert execute_operation(operation.pk) == "skipped"
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET not_before=statement_timestamp()-interval '1 second' WHERE id=%s",
            [operation.pk],
        )
    assert execute_operation(operation.pk) == "retry"
    with owner_context(graph.organization_a):
        operation.refresh_from_db()
    assert operation.not_before is not None
    second_wait = operation.not_before - operation.updated_at
    assert timedelta(seconds=119) <= second_wait <= timedelta(seconds=121)
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET not_before=statement_timestamp()-interval '1 second' WHERE id=%s",
            [operation.pk],
        )
    assert execute_operation(operation.pk) == "succeeded"
    with runtime_role(), tenant_context(admin, graph.organization_a):
        attempts = list(
            WebhookAttempt.objects.order_by("attempt").values_list(
                "attempt", "outcome", "http_status"
            )
        )
    assert attempts == [
        (1, "http_error", 503),
        (2, "timeout", None),
        (3, "delivered", 200),
    ]
    assert len(SYNTHETIC_TRANSPORT.requests) == 3
    assert len({request.body for request in SYNTHETIC_TRANSPORT.requests}) == 1


def test_revoked_subscription_or_client_cancels_queued_deliveries(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    _credential, token, subscription = _subscribe(graph, admin)
    setup = seed_appointment_setup(graph)
    _book(graph, setup)
    revoked = api_post(
        Client(),
        token,
        f"/api/v1/webhook-subscriptions/{subscription['id']}/revoke",
        {},
        key="revoke-1",
    )
    assert revoked.status_code == 200
    (operation,) = _operations(graph)
    assert execute_operation(operation.pk) == "cancelled"
    assert SYNTHETIC_TRANSPORT.requests == []
    # A revoked subscription captures no further events.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_synthetic_appointment(
            setup, start_local="2035-06-02T08:00", end_local="2035-06-02T08:30"
        )
    assert len(_operations(graph)) == 1


def test_lost_approver_authority_cancels_delivery_without_a_send(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    _subscribe(graph, admin)
    _book(graph)
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_userclinicrole SET revoked_at=now() "
            "WHERE user_id=%s AND clinic_id=%s",
            [admin, graph.clinic_b],
        )
    (operation,) = _operations(graph)
    assert execute_operation(operation.pk) == "cancelled"
    assert SYNTHETIC_TRANSPORT.requests == []


def test_reference_verifier_refuses_replays_tampering_and_malformed_headers(
    interop_secrets: Path,
) -> None:
    secret = "whsec_" + "a" * 64
    body = b'{"id":"synthetic","type":"appointment.created"}'
    now = 1_900_000_000
    header = signature_header([secret], now, body)
    verify(header, body, secret, now=now)
    verify(header, body, secret, now=now + 300)
    cases = {
        "timestamp_outside_tolerance": [
            (signature_header([secret], now - 301, body), body, secret),
            (signature_header([secret], now + 301, body), body, secret),
        ],
        "signature_mismatch": [
            (header, body + b" ", secret),
            (header, body, "whsec_" + "b" * 64),
            (header.replace(f"t={now}", f"t={now - 1}"), body, secret),
        ],
        "malformed": [
            ("", body, secret),
            (f"t={now}", body, secret),
            ("v1=" + "0" * 64, body, secret),
            (f"t={now},v1=xyz", body, secret),
            (f"t={now},t={now},v1=" + "0" * 64, body, secret),
            ("garbage", body, secret),
        ],
    }
    for reason, inputs in cases.items():
        for value, data, key in inputs:
            with pytest.raises(WebhookSignatureError) as refused:
                verify(value, data, key, now=now)
            assert refused.value.reason == reason
    rotated = signature_header([secret, "whsec_" + "c" * 64], now, body)
    verify(rotated, body, "whsec_" + "c" * 64, now=now)
    verify(f"{header},v9=future", body, secret, now=now)


def test_rotated_subscriptions_sign_with_both_secrets_during_overlap(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    _credential, token, subscription = _subscribe(graph, org_admin(graph))
    rotated = api_post(
        Client(),
        token,
        f"/api/v1/webhook-subscriptions/{subscription['id']}/rotate-secret",
        {},
        key="rotate-1",
    ).json()
    _book(graph)
    (operation,) = _operations(graph)
    assert execute_operation(operation.pk) == "succeeded"
    (request,) = SYNTHETIC_TRANSPORT.requests
    header = request.headers["Clinic-Signature"]
    assert header.count("v1=") == 2
    for secret in (subscription["secret"], rotated["secret"]):
        verify(header, request.body, str(secret), now=int(time.time()))


def test_offboarding_revokes_every_client_and_subscription(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    credential, token, _subscription = _subscribe(graph, admin)
    assert api_get(Client(), token, "/api/v1/webhook-subscriptions").status_code == 200
    with runtime_role(), tenant_context(admin, graph.organization_a):
        cancelled = cancel_subscription(
            clinic_id=graph.clinic_a,
            expected_revision=1,
            command_id=uuid4(),
        )
    assert cancelled.state == "cancelled"
    assert event(graph, admin, "offboard").state == "offboarded"
    with runtime_role(), tenant_context(admin, graph.organization_a):
        client = ApiClient.objects.get(pk=credential.client_id)
        subscriptions = list(
            WebhookSubscription.objects.values_list("state", flat=True)
        )
    assert (client.state, client.revocation_reason) == ("revoked", "offboarding")
    assert subscriptions == ["revoked"]
    refused = api_get(Client(), token, "/api/v1/webhook-subscriptions")
    assert refused.status_code == 401
    assert request_token(Client(), credential).status_code == 401
