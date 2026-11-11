"""External API v1 acceptance on real PostgreSQL roles: tokens, scopes, ids, limits."""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import socket
import subprocess
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.billing.services import create_invoice, issue_invoice
from apps.interop import ratelimit
from apps.interop.external_ids import external_id, internal_id
from apps.interop.management import revoke_client, rotate_client_secret
from apps.interop.models import ApiClient
from apps.interop.ratelimit import RateLimitUnavailableError, RedisTokenBucket
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
    org_b_admin,
    request_token,
    token_post,
    webhook_transport,
)
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from apps.billing.models import Invoice
    from apps.scheduling.models import Appointment

    from interop.support import LocalBucket
    from rbac_fixtures import RbacGraph

__all__ = ("interop_secrets", "local_bucket", "webhook_transport")
pytestmark = pytest.mark.django_db(transaction=True)

WINDOW = "start=2035-06-01T00:00:00Z&end=2035-06-08T00:00:00Z"
UUID_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
GATED = pytest.mark.skipif(
    os.environ.get("CLINIC_BROKER_GATE", "") != "required",
    reason="requires the dedicated broker gate (CLINIC_BROKER_GATE=required)",
)
READY_MARKER = b"Ready to accept connections"


def _booked(graph: RbacGraph) -> tuple[AppointmentSetup, Appointment]:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointment = create_synthetic_appointment(setup)
    return setup, appointment


def _issued_invoice(
    graph: RbacGraph, patient_id: UUID, appointment_id: UUID
) -> Invoice:
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invoice = create_invoice(
            clinic_id=graph.clinic_a,
            patient_id=patient_id,
            amount_minor=15000,
            idempotency_key=uuid4(),
            appointment_id=appointment_id,
        )
        return issue_invoice(
            clinic_id=graph.clinic_a,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )


def _no_internal_ids(payload: object, *internal: UUID) -> None:
    text = json.dumps(payload)
    for value in internal:
        assert str(value) not in text


def test_client_credentials_token_lists_appointments_with_opaque_ids(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    setup, appointment = _booked(graph)
    admin = org_admin(graph)
    credential = make_client(graph, admin)
    http = Client()
    issued = request_token(http, credential)
    assert issued.status_code == 200
    body = issued.json()
    assert body["token_type"] == "Bearer"  # noqa: S105 - the RFC 6750 scheme name
    assert body["expires_in"] == 900
    assert body["scope"].split() == sorted(
        [
            "appointments:read",
            "availability:read",
            "invoices.status:read",
            "patients.demographics:read",
            "webhooks:manage",
        ]
    )
    assert "no-store" in issued["Cache-Control"]
    token = body["access_token"]
    listed = api_get(http, token, f"/api/v1/appointments?{WINDOW}")
    assert listed.status_code == 200
    page = listed.json()
    assert page["next_cursor"] is None
    (item,) = page["data"]
    assert item["id"] == external_id(
        "appointment", graph.organization_a, appointment.pk
    )
    assert item["status"] == "booked"
    assert item["patient"].startswith("pat_")
    assert item["practitioner"].startswith("prc_")
    assert item["start_at"].endswith("Z")
    _no_internal_ids(page, appointment.pk, setup.patient_id, graph.physician)
    detail = api_get(http, token, f"/api/v1/appointments/{item['id']}")
    assert detail.status_code == 200
    assert detail.json() == item
    patient = api_get(http, token, f"/api/v1/patients/{item['patient']}")
    assert patient.status_code == 200
    assert patient.json() == {
        "id": item["patient"],
        "display_name": "Synthetic Booking Persona",
        "birth_date": "2000-01-02",
    }
    invoice = _issued_invoice(graph, setup.patient_id, appointment.pk)
    invoices = api_get(
        http,
        token,
        "/api/v1/invoices?start=2000-01-01T00:00:00Z&end=2000-01-31T00:00:00Z",
    )
    assert invoices.status_code == 200
    assert invoices.json()["data"] == []
    issued_at = invoice.issued_at
    assert issued_at is not None
    window = (
        f"start={issued_at.date().isoformat()}T00:00:00Z"
        f"&end={issued_at.date().isoformat()}T23:59:59Z"
    )
    (row,) = api_get(http, token, f"/api/v1/invoices?{window}").json()["data"]
    assert row["status"] == "open"
    assert row["amount_minor"] == 15000
    assert row["appointment"] == item["id"]
    single = api_get(http, token, f"/api/v1/invoices/{row['id']}")
    assert single.status_code == 200
    assert single.json() == row
    availability = api_get(http, token, f"/api/v1/availability?{WINDOW}")
    assert availability.status_code == 200
    kinds = {entry["kind"] for entry in availability.json()["data"]}
    assert kinds == {"window", "busy"}
    _no_internal_ids(availability.json(), graph.physician, appointment.pk)


def test_keyset_cursor_pages_through_appointments(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        for hour in ("08", "09", "10"):
            create_synthetic_appointment(
                setup,
                start_local=f"2035-06-02T{hour}:00",
                end_local=f"2035-06-02T{hour}:30",
            )
    token = access_token(make_client(graph, org_admin(graph)))
    http = Client()
    first = api_get(http, token, f"/api/v1/appointments?{WINDOW}&limit=2").json()
    assert len(first["data"]) == 2
    assert first["next_cursor"]
    second = api_get(
        http,
        token,
        f"/api/v1/appointments?{WINDOW}&limit=2&cursor={first['next_cursor']}",
    ).json()
    assert len(second["data"]) == 1
    assert second["next_cursor"] is None
    starts = [item["start_at"] for item in first["data"] + second["data"]]
    assert starts == sorted(starts)
    forged = api_get(http, token, f"/api/v1/appointments?{WINDOW}&cursor=e30")
    assert forged.status_code == 400
    assert forged.json() == {
        "code": "invalid_input",
        "message_key": "api.error.invalid_input",
    }


def test_token_endpoint_refuses_with_rfc6749_bodies(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    credential = make_client(graph, org_admin(graph), ["appointments:read"])
    http = Client()
    wrong = token_post(
        http,
        {
            "grant_type": "client_credentials",
            "client_id": str(credential.client_id),
            "client_secret": "cs_" + "0" * 64,
        },
    )
    unknown = token_post(
        http,
        {
            "grant_type": "client_credentials",
            "client_id": str(uuid4()),
            "client_secret": credential.client_secret,
        },
    )
    grant = token_post(
        http, {"grant_type": "password", "client_id": str(credential.client_id)}
    )
    missing = token_post(http, {})
    posted = token_post(
        http,
        {
            "grant_type": "client_credentials",
            "client_id": str(credential.client_id),
            "client_secret": credential.client_secret,
        },
    )
    both = token_post(
        http,
        {
            "grant_type": "client_credentials",
            "client_id": str(credential.client_id),
            "client_secret": credential.client_secret,
        },
        authorization="Basic " + "e30=",
    )
    with runtime_role():
        as_json = http.post(
            "/api/v1/oauth/token",
            json.dumps({"grant_type": "client_credentials"}),
            content_type="application/json",
        )
    for refusal in (wrong, unknown, both):
        assert refusal.status_code == 401
        assert refusal.json() == {"error": "invalid_client"}
        assert refusal["WWW-Authenticate"] == 'Basic realm="clinic-ops"'
    assert grant.status_code == 400
    assert grant.json() == {"error": "unsupported_grant_type"}
    assert missing.status_code == 400
    assert missing.json() == {"error": "invalid_request"}
    assert as_json.status_code == 400
    assert as_json.json() == {"error": "invalid_request"}
    assert posted.status_code == 200
    assert posted.json()["scope"] == "appointments:read"
    widened = request_token(http, credential, "appointments:read invoices.status:read")
    assert widened.status_code == 400
    assert widened.json() == {"error": "invalid_scope"}
    unknown_scope = request_token(http, credential, "clinical:read")
    assert unknown_scope.status_code == 400
    assert unknown_scope.json() == {"error": "invalid_scope"}


def test_scopes_bound_every_resource_and_malformed_tokens_are_identical(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    _booked(graph)
    token = access_token(make_client(graph, org_admin(graph), ["appointments:read"]))
    http = Client()
    item = api_get(http, token, f"/api/v1/appointments?{WINDOW}").json()["data"][0]
    for path in (
        f"/api/v1/patients/{item['patient']}",
        f"/api/v1/invoices?{WINDOW}",
        f"/api/v1/availability?{WINDOW}",
        "/api/v1/webhook-subscriptions",
    ):
        denied = api_get(http, token, path)
        assert denied.status_code == 403
        assert denied.json()["code"] == "insufficient_scope"
        assert 'error="insufficient_scope"' in denied["WWW-Authenticate"]
    responses = [
        api_get(http, value, f"/api/v1/appointments?{WINDOW}")
        for value in ("", "at_" + "0" * 64, "garbage", token + "0")
    ]
    with runtime_role():
        responses.append(http.get(f"/api/v1/appointments?{WINDOW}"))
    for refusal in responses:
        assert refusal.status_code == 401
        assert refusal.json() == {
            "code": "invalid_token",
            "message_key": "api.error.invalid_token",
        }
    elsewhere = api_get(http, token, "/api/v1/clinical-notes")
    assert elsewhere.status_code == 404
    assert elsewhere.json()["code"] == "not_found"


def test_foreign_and_unknown_external_ids_return_the_identical_404(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    first, second = seed_cross_clinic_appointment_setups(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        own = create_synthetic_appointment(first)
        other_clinic = create_synthetic_appointment(
            second, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
    org_a_admin = org_admin(graph)
    org_a_token = access_token(make_client(graph, org_a_admin))
    org_b_token = access_token(
        make_client(
            graph,
            org_b_admin(graph),
            organization=graph.organization_b,
            clinic=graph.clinic_c,
        )
    )
    http = Client()
    visible = external_id("appointment", graph.organization_a, own.pk)
    assert (
        api_get(http, org_a_token, f"/api/v1/appointments/{visible}").status_code == 200
    )
    probes = [
        api_get(http, org_b_token, f"/api/v1/appointments/{visible}"),
        api_get(
            http,
            org_a_token,
            "/api/v1/appointments/"
            + external_id("appointment", graph.organization_a, other_clinic.pk),
        ),
        api_get(
            http,
            org_a_token,
            "/api/v1/appointments/"
            + external_id("appointment", graph.organization_a, uuid4()),
        ),
        api_get(http, org_a_token, f"/api/v1/appointments/{own.pk}"),
        api_get(
            http,
            org_a_token,
            "/api/v1/appointments/"
            + external_id("patient", graph.organization_a, own.pk),
        ),
        api_get(http, org_a_token, "/api/v1/appointments/apt_" + "a" * 52),
        api_get(
            http,
            org_a_token,
            "/api/v1/patients/"
            + external_id("patient", graph.organization_b, first.patient_id),
        ),
    ]
    bodies = {json.dumps(probe.json(), sort_keys=True) for probe in probes}
    assert {probe.status_code for probe in probes} == {404}
    assert bodies == {
        json.dumps({"code": "not_found", "message_key": "api.error.not_found"})
    }
    varying = {"date", "x-request-id"}
    headers = [
        {k: v for k, v in probe.items() if k.lower() not in varying} for probe in probes
    ]
    assert all(header == headers[0] for header in headers), [
        {k: v for k, v in header.items() if headers[0].get(k) != v}
        for header in headers
    ]
    assert internal_id("appointment", graph.organization_b, visible) is None
    assert internal_id("appointment", graph.organization_a, visible) == own.pk


def test_rate_limit_answers_429_with_retry_after_until_refill(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    token = access_token(make_client(graph, org_admin(graph), rate=2))
    http = Client()
    path = f"/api/v1/appointments?{WINDOW}"
    assert api_get(http, token, path).status_code == 200
    assert api_get(http, token, path).status_code == 200
    limited = api_get(http, token, path)
    assert limited.status_code == 429
    assert limited.json() == {
        "code": "rate_limited",
        "message_key": "api.error.rate_limited",
    }
    assert limited["Retry-After"] == "30"
    local_bucket.now += 29
    assert api_get(http, token, path).status_code == 429
    local_bucket.now += 1
    assert api_get(http, token, path).status_code == 200


def test_metering_outage_fails_closed_with_retry_after(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    credential = make_client(graph, org_admin(graph))
    token = access_token(credential)
    local_bucket.unavailable = True
    refused = api_get(Client(), token, f"/api/v1/appointments?{WINDOW}")
    assert refused.status_code == 503
    assert refused["Retry-After"] == "30"
    assert refused.json()["code"] == "temporarily_unavailable"
    issued = request_token(Client(), credential)
    assert issued.status_code == 503
    assert issued.json() == {"error": "temporarily_unavailable"}
    assert issued["Retry-After"] == "30"


def test_token_endpoint_rate_limit_is_per_client(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    credential = make_client(graph, org_admin(graph))
    http = Client()
    for _ in range(ratelimit.TOKEN_REQUESTS_PER_MINUTE):
        assert request_token(http, credential).status_code == 200
    limited = request_token(http, credential)
    assert limited.status_code == 429
    assert limited.json() == {"error": "temporarily_unavailable"}
    assert int(limited["Retry-After"]) >= 1


def test_revocation_rotation_and_approver_authority_take_effect_immediately(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    credential = make_client(graph, admin)
    token = access_token(credential)
    http = Client()
    path = f"/api/v1/appointments?{WINDOW}"
    assert api_get(http, token, path).status_code == 200
    with runtime_role(), tenant_context(admin, graph.organization_a):
        rotated = rotate_client_secret(
            clinic_id=graph.clinic_a,
            client_id=credential.client_id,
            expected_revision=credential.revision,
        )
    assert rotated.client_secret != credential.client_secret
    # Both secrets issue tokens during the 24-hour overlap.
    assert request_token(http, credential).status_code == 200
    assert request_token(http, rotated).status_code == 200
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "UPDATE clinic_app.interop_apiclientsecret "
            "SET expires_at=created_at+interval '1 microsecond' "
            "WHERE client_id=%s AND version=1",
            [credential.client_id],
        )
    assert request_token(http, credential).status_code == 401
    assert request_token(http, rotated).status_code == 200
    # Losing organization authority suspends the approving admin's clients.
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_userclinicrole SET revoked_at=now() "
            "WHERE user_id=%s AND clinic_id=%s",
            [admin, graph.clinic_b],
        )
    assert api_get(http, token, path).status_code == 401
    assert request_token(http, rotated).status_code == 401
    other = org_admin(graph)
    with runtime_role(), tenant_context(other, graph.organization_a):
        client = ApiClient.objects.get(pk=credential.client_id)
        restored = rotate_client_secret(
            clinic_id=graph.clinic_a,
            client_id=credential.client_id,
            expected_revision=client.revision,
        )
    fresh = access_token(restored)
    assert api_get(http, fresh, path).status_code == 200
    with runtime_role(), tenant_context(other, graph.organization_a):
        result = revoke_client(
            clinic_id=graph.clinic_a,
            client_id=credential.client_id,
            expected_revision=restored.revision,
        )
    assert result.state == "revoked"
    assert api_get(http, fresh, path).status_code == 401
    assert request_token(http, restored).status_code == 401


def test_expired_tokens_are_refused(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    credential = make_client(graph, org_admin(graph))
    token = access_token(credential)
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "UPDATE clinic_app.interop_apiaccesstoken SET expires_at=issued_at "
            "+interval '1 microsecond' WHERE client_id=%s",
            [credential.client_id],
        )
    refused = api_get(Client(), token, f"/api/v1/appointments?{WINDOW}")
    assert refused.status_code == 401


def test_writes_require_idempotency_keys_and_replay_exactly(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    token = access_token(make_client(graph, org_admin(graph)))
    http = Client()
    body = {
        "url": "https://parceiro.example.org/hooks/clinic",
        "events": ["appointment.created", "appointment.status_changed"],
    }
    missing = api_post(http, token, "/api/v1/webhook-subscriptions", body)
    assert missing.status_code == 400
    assert missing.json()["code"] == "idempotency_key_required"
    malformed = api_post(http, token, "/api/v1/webhook-subscriptions", body, key="a b")
    assert malformed.status_code == 400
    created = api_post(http, token, "/api/v1/webhook-subscriptions", body, key="k-1")
    assert created.status_code == 201
    first = created.json()
    assert first["secret"].startswith("whsec_")
    assert first["events"] == sorted(body["events"])
    replay = api_post(http, token, "/api/v1/webhook-subscriptions", body, key="k-1")
    assert replay.status_code == 201
    assert replay["Idempotent-Replayed"] == "true"
    assert replay.json() == first
    conflict = api_post(
        http,
        token,
        "/api/v1/webhook-subscriptions",
        {**body, "events": ["appointment.created"]},
        key="k-1",
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    listed = api_get(http, token, "/api/v1/webhook-subscriptions").json()["data"]
    assert [row["id"] for row in listed] == [first["id"]]
    assert "secret" not in listed[0]
    rotate = f"/api/v1/webhook-subscriptions/{first['id']}/rotate-secret"
    rotated = api_post(http, token, rotate, {}, key="k-2")
    assert rotated.status_code == 200
    assert rotated.json()["secret_version"] == 2
    assert rotated.json()["secret"] != first["secret"]
    revoke = f"/api/v1/webhook-subscriptions/{first['id']}/revoke"
    revoked = api_post(http, token, revoke, {}, key="k-3")
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    again = api_post(http, token, rotate, {}, key="k-4")
    assert again.status_code == 409
    assert again.json()["code"] == "subscription_revoked"
    unknown = api_post(
        http, token, f"/api/v1/webhook-subscriptions/{uuid4()}/revoke", {}, key="k-5"
    )
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://parceiro.example.org/hooks",
        "https://parceiro.example.org/hooks?patient=1",
        "https://parceiro.example.org/hooks#frag",
        "https://user:pass@parceiro.example.org/hooks",
        "https://127.0.0.1/hooks",
        "https://localhost/hooks",
        "https://parceiro.example.org:99999/hooks",
    ],
)
def test_webhook_endpoints_must_be_https_without_query_or_credentials(
    rbac_graph: RbacGraph, interop_secrets: Path, local_bucket: LocalBucket, url: str
) -> None:
    token = access_token(make_client(rbac_graph, org_admin(rbac_graph)))
    refused = api_post(
        Client(),
        token,
        "/api/v1/webhook-subscriptions",
        {"url": url, "events": ["appointment.created"]},
        key=f"url-{uuid4().hex}",
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_input"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def redis_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run one task-owned redis-server for the Lua token-bucket cases."""
    binary = shutil.which("redis-server")
    if binary is None:
        if os.environ.get("CLINIC_BROKER_GATE") == "required":
            pytest.fail("broker gate requires the redis-server binary")
        pytest.skip("redis-server is not installed")
    port = _free_port()
    root = tmp_path_factory.mktemp("interop-redis")
    process = subprocess.Popen(  # noqa: S603 - fixed binary, closed argv
        (
            binary,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(root),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    ready, _write, _error = select.select([process.stdout], [], [], 15.0)
    assert ready, "redis-server did not announce readiness"
    for line in process.stdout:
        if READY_MARKER in line:
            break
    try:
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()


@GATED
def test_redis_lua_bucket_bursts_refills_and_isolates_clients(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(ratelimit, "_broker_url", lambda: redis_url)
    monkeypatch.setattr(ratelimit, "_now", lambda: clock["t"])
    bucket = RedisTokenBucket()
    client, other = uuid4(), uuid4()
    decisions = [
        bucket.spend(client_id=client, per_minute=3, purpose="api") for _ in range(4)
    ]
    assert [decision.allowed for decision in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after_seconds == 20
    assert bucket.spend(client_id=other, per_minute=3, purpose="api").allowed
    assert bucket.spend(client_id=client, per_minute=3, purpose="token").allowed
    clock["t"] += 19
    assert not bucket.spend(client_id=client, per_minute=3, purpose="api").allowed
    clock["t"] += 1
    assert bucket.spend(client_id=client, per_minute=3, purpose="api").allowed


def test_redis_bucket_unreachable_or_non_redis_broker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bucket = RedisTokenBucket()
    monkeypatch.setattr(
        ratelimit, "_broker_url", lambda: f"redis://127.0.0.1:{_free_port()}/0"
    )
    with pytest.raises(RateLimitUnavailableError):
        bucket.spend(client_id=uuid4(), per_minute=10, purpose="api")
    monkeypatch.setattr(ratelimit, "_broker_url", lambda: "memory://")
    with pytest.raises(RateLimitUnavailableError):
        bucket.spend(client_id=uuid4(), per_minute=10, purpose="api")
    with pytest.raises(RateLimitUnavailableError):
        bucket.spend(client_id=uuid4(), per_minute=10, purpose="unknown")
