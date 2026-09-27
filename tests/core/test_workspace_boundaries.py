"""Regression receipts for the todo-13 independent gate blockers."""

from __future__ import annotations

import re
from datetime import timedelta
from difflib import unified_diff
from itertools import chain, combinations
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.core.navigation import DESTINATIONS, destination_url
from apps.identity.models import RoleGrant, SavedView, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.identity.saved_views import archive_saved_view, save_view
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from core.test_navigation import (
    PATIENT,
    RUN,
    _client_for,
    _get,
    _main,
    _patient_token,
    _post,
)
from identity.permission_support import owner_context
from otp_test_support import runtime_role
from patient_http_support import seed_patients

if TYPE_CHECKING:
    from apps.core.navigation import Destination
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

# Discover the delivered destinations and every stored role from live registries.
PLACES = tuple(chain.from_iterable(d.section() for d in DESTINATIONS))
ROUTE_CASES = [
    (place, role, removed)
    for place in PLACES
    for role in UserClinicRole.Role.values
    if place.clinic_scoped
    and (place.route_roles is None or role in place.route_roles)
    and (not place.permission or set(place.permission) & BUNDLES_V1[role])
    for size in range(len(set(place.permission) & BUNDLES_V1[role]) + 1)
    for removed in combinations(sorted(set(place.permission) & BUNDLES_V1[role]), size)
]


def remove_permission(graph: RbacGraph, role: str, permission: str) -> None:
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role=role,
            permission=permission,
            valid_from=timezone.now() - timedelta(minutes=1),
        )


def _normalized_denial(
    response: _MonkeyPatchedWSGIResponse,
) -> tuple[bytes, dict[str, str]]:
    """Normalize only X-Request-ID, the CSRF form token and CSP nonces.

    Paths, clinic identifiers and every other body/header byte remain intact.
    """
    body = response.content
    headers = dict(response.headers.items())
    # Django stores Set-Cookie separately from response.headers.
    if response.cookies:
        headers["Set-Cookie"] = response.cookies.output()
    # X-Request-ID is generated per request; preserve the header's presence.
    if request_id := headers.get("X-Request-ID"):
        body = body.replace(request_id.encode(), b"REQUEST_ID")
        headers["X-Request-ID"] = "REQUEST_ID"
    # Django masks each csrfmiddlewaretoken form value independently.
    body = re.sub(
        rb'(name="csrfmiddlewaretoken" value=")[A-Za-z0-9]+(")',
        rb"\1CSRF_TOKEN\2",
        body,
    )
    # Normalize only nonce values declared by a CSP header, if any.
    for name in ("Content-Security-Policy", "Content-Security-Policy-Report-Only"):
        if name in headers:
            for nonce in re.findall(r"'nonce-([^']+)'", headers[name]):
                body = body.replace(nonce.encode(), b"CSP_NONCE")
            headers[name] = re.sub(r"'nonce-[^']+'", "'nonce-CSP_NONCE'", headers[name])
    return body, headers


@pytest.mark.parametrize(("place", "role", "removed"), ROUTE_CASES)
def test_every_destination_enforces_its_permission_over_http(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    place: Destination,
    role: str,
    removed: tuple[str, ...],
) -> None:
    # The date badge must not change across a midnight boundary; authority is real.
    monkeypatch.setattr(
        "apps.core.navigation.clinic_local_today", lambda _zone: "2033-05-18"
    )
    # Freeze cookie expiry generation, not its value in the compared headers.
    monkeypatch.setattr(
        "django.http.response.time", SimpleNamespace(time=lambda: 2_000_000_000)
    )
    client, _ = _client_for(rbac_graph, role)
    url = destination_url(place, rbac_graph.clinic_a)
    assert _get(client, url).status_code == 200
    permissions = set(place.permission) & BUNDLES_V1[role]
    for permission in removed:
        remove_permission(rbac_graph, role, permission)
    actual = _get(client, url)
    if permissions - set(removed):
        assert actual.status_code == 200
        for attribute in ("data-command-next", "data-combobox-context"):
            assert f'{attribute}="{url}"' in actual.content.decode()
    else:
        assert actual.status_code in (403, 404)
        actual_body, actual_headers = _normalized_denial(actual)
        for clinic_id in (uuid4(), rbac_graph.clinic_b, rbac_graph.clinic_c):
            # Unknown, same-org without membership, and other-org destinations.
            denied = _get(client, destination_url(place, clinic_id))
            assert actual.status_code == denied.status_code
            assert _main(actual) == _main(denied)
            denied_body, denied_headers = _normalized_denial(denied)
            assert actual_body == denied_body, "\n".join(
                unified_diff(
                    actual_body.decode().splitlines(),
                    denied_body.decode().splitlines(),
                    fromfile="permission-denied",
                    tofile="unknown-or-foreign",
                )
            )
            assert actual_headers == denied_headers


def test_revoked_patient_cannot_be_reopened_or_repinned(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    seed_patients(graph, user.pk, graph.clinic_a, (PATIENT,))
    agenda = f"/scheduling/clinics/{graph.clinic_a}/agenda/"
    _get(client, agenda)
    _post(client, RUN, {"token": _patient_token(client, graph), "next": agenda})
    key = f"workspace.patient.{graph.clinic_a}"
    enrollment = client.session[key]
    remove_permission(graph, "receptionist", "demographics.read")
    response = _post(
        client,
        f"/intake/clinics/{graph.clinic_a}/contacts/",
        {"action": "manage", "enrollment_id": enrollment},
    )
    assert response.status_code == 404
    assert PATIENT.encode() not in response.content
    assert key not in client.session


def test_palette_uses_the_typed_ui_endpoint(rbac_graph: RbacGraph) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    html = _get(client, "/auth/protected/").content.decode()
    assert 'data-combobox-endpoint="/api/ui/v1/command/search/"' in html


def test_saved_view_transitions_append_metadata_only_events(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _, user = _client_for(graph, "receptionist")
    with runtime_role(), tenant_context(user.pk, graph.organization_a):
        record = save_view(
            clinic_id=graph.clinic_a, destination="agenda", params={"view": "week"}
        )
        assert (
            save_view(
                clinic_id=graph.clinic_a, destination="agenda", params={"view": "week"}
            ).id
            == record.id
        )
        archive_saved_view(clinic_id=graph.clinic_a, view_id=record.id)
    with owner_context(graph.organization_a):
        events = list(
            AuditEvent.objects.filter(
                event_type__startswith="identity.saved_view."
            ).order_by("seq")
        )
    assert [event.event_type for event in events] == [
        "identity.saved_view.created",
        "identity.saved_view.archived",
    ]
    for event in events:
        assert set(event.payload) <= {
            "clinic_id",
            "http_method",
            "http_status",
            "object_verb",
            "reason_code",
            "request_id",
        }
        assert event.affected_record_id == str(record.id)


@pytest.mark.parametrize("action", ["create", "archive"])
def test_failed_audit_rolls_back_saved_view_transition(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    graph = rbac_graph
    _, user = _client_for(graph, "receptionist")
    with runtime_role(), tenant_context(user.pk, graph.organization_a):
        existing = save_view(clinic_id=graph.clinic_a, destination="agenda", params={})

        def fail_audit(*args: object, **kwargs: object) -> None:
            message = "synthetic audit refusal"
            raise RuntimeError(message)

        monkeypatch.setattr("apps.identity.saved_views.record_phase1_event", fail_audit)
        if action == "create":
            with pytest.raises(RuntimeError, match="synthetic audit refusal"):
                save_view(
                    clinic_id=graph.clinic_a,
                    destination="agenda",
                    params={"view": "week"},
                )
        else:
            with pytest.raises(RuntimeError, match="synthetic audit refusal"):
                archive_saved_view(clinic_id=graph.clinic_a, view_id=existing.id)
        assert SavedView.objects.count() == 1
        assert SavedView.objects.get(pk=existing.id).archived_at is None


def test_runtime_cannot_resurrect_an_archived_saved_view(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    _, user = _client_for(graph, "receptionist")
    with runtime_role(), tenant_context(user.pk, graph.organization_a):
        record = save_view(clinic_id=graph.clinic_a, destination="agenda", params={})
        archive_saved_view(clinic_id=graph.clinic_a, view_id=record.id)
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.identity_savedview SET archived_at = NULL "
                "WHERE id = %s",
                [record.id],
            )
        assert SavedView.objects.get(pk=record.id).archived_at is not None
