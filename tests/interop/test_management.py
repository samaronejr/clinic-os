"""Private management API over HTTP: TOTP session, JSON-only commands, one denial."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.interop.management import IssuedCredential
from django.test import Client

from auth.stepup_test_support import create_role_actor
from interop.support import (
    access_token,
    api_post,
    interop_secrets,
    local_bucket,
    org_admin,
    request_token,
    webhook_transport,
)
from renewal.test_retention import staff_client

if TYPE_CHECKING:
    from pathlib import Path

    from django.test.client import _MonkeyPatchedWSGIResponse

    from interop.support import LocalBucket
    from rbac_fixtures import RbacGraph

__all__ = ("interop_secrets", "local_bucket", "webhook_transport")
pytestmark = pytest.mark.django_db(transaction=True)

MANAGEMENT = "/api/interop/v1/"
DENIED = (403, {"code": "not_available"})


def _manage(
    http: Client, action: str, clinic_id: UUID, **terms: object
) -> _MonkeyPatchedWSGIResponse:
    body = {"action": action, "clinic_id": str(clinic_id), **terms}
    return http.post(MANAGEMENT, json.dumps(body), content_type="application/json")


def _refusal(response: _MonkeyPatchedWSGIResponse) -> tuple[int, object]:
    return response.status_code, response.json()


def test_client_and_webhook_commands_run_over_the_privileged_json_api(
    rbac_graph: RbacGraph,
    interop_secrets: Path,
    local_bucket: LocalBucket,
    webhook_transport: None,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    with staff_client(admin) as http:
        created = _manage(
            http,
            "clients.create",
            graph.clinic_a,
            name="Parceiro Sintetico",
            scopes=["webhooks:manage", "appointments:read"],
            rate_limit_per_minute=60,
            command_id=str(uuid4()),
        )
        assert created.status_code == 201
        assert "no-store" in created.headers["Cache-Control"]
        issued = created.json()
        (listed,) = _manage(http, "clients.list", graph.clinic_a).json()["clients"]
        rotated = _manage(
            http,
            "clients.rotate",
            graph.clinic_a,
            client_id=issued["client_id"],
            expected_revision=issued["revision"],
        )
        stale = _manage(
            http,
            "clients.rotate",
            graph.clinic_a,
            client_id=issued["client_id"],
            expected_revision=issued["revision"],
        )
    assert (listed["client_id"], listed["scopes"], listed["state"]) == (
        issued["client_id"],
        ["appointments:read", "webhooks:manage"],
        "active",
    )
    assert issued["client_secret"] not in json.dumps(listed)
    assert rotated.status_code == 200
    fresh = rotated.json()
    assert fresh["client_secret"] != issued["client_secret"]
    assert _refusal(stale) == (409, {"code": "conflict"})
    credential = IssuedCredential(
        UUID(fresh["client_id"]), fresh["client_secret"], fresh["revision"]
    )
    subscribed = api_post(
        Client(),
        access_token(credential),
        "/api/v1/webhook-subscriptions",
        {
            "url": "https://parceiro.example.org/hooks",
            "events": ["appointment.created"],
        },
        key="management-subscribe",
    )
    assert subscribed.status_code == 201
    subscription = subscribed.json()
    with staff_client(admin) as http:
        (registered,) = _manage(
            http, "webhooks.list", graph.clinic_a, client_id=fresh["client_id"]
        ).json()["subscriptions"]
        deliveries = _manage(
            http,
            "deliveries.list",
            graph.clinic_a,
            subscription_id=subscription["id"],
        ).json()["deliveries"]
        stopped = _manage(
            http,
            "webhooks.revoke",
            graph.clinic_a,
            subscription_id=subscription["id"],
            expected_revision=registered["revision"],
        ).json()
        revoked = _manage(
            http,
            "clients.revoke",
            graph.clinic_a,
            client_id=fresh["client_id"],
            expected_revision=fresh["revision"],
        ).json()
    assert (registered["subscription_id"], registered["state"]) == (
        subscription["id"],
        "active",
    )
    assert deliveries == []
    assert (stopped["record_id"], stopped["state"]) == (subscription["id"], "revoked")
    assert (revoked["record_id"], revoked["state"]) == (fresh["client_id"], "revoked")
    assert request_token(Client(), credential).status_code == 401


def test_malformed_unknown_foreign_and_clinic_scoped_requests_are_refused(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    clinic_only = create_role_actor(graph, UserClinicRole.Role.ORG_ADMIN).pk
    with staff_client(admin) as http:
        form = http.post(MANAGEMENT, {"action": "clients.list"})
        extra = _manage(http, "clients.list", graph.clinic_a, name="extra")
        unknown = _manage(http, "clients.export", graph.clinic_a)
        foreign = _manage(http, "clients.list", graph.clinic_c)
        missing = _manage(http, "clients.list", uuid4())
        fetched = http.get(MANAGEMENT)
    with staff_client(clinic_only) as http:
        scoped = _manage(http, "clients.list", graph.clinic_a)
    assert _refusal(form) == (400, {"code": "invalid_input"})
    assert _refusal(extra) == (400, {"code": "invalid_input"})
    assert fetched.status_code == 405
    for refused in (unknown, foreign, missing, scoped):
        assert _refusal(refused) == DENIED
