"""Resolver-derived tripwire: a staff-clinic refusal cannot mutate its session."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core.navigation import DESTINATIONS
from apps.core.patient_context import PATIENT_BOUND_VIEWS
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from django.urls import URLResolver, get_resolver

from core.test_navigation import _client_for, _get, _post
from core.test_workspace_boundaries import (
    _normalized_denial,
    _pin_patient,
    _restore_session,
    remove_permission,
)
from identity.permission_support import owner_context
from workspace_refusal_support import WorkspaceRoute, workspace_routes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _registered_views() -> set[str]:
    names = set(PATIENT_BOUND_VIEWS)
    pending = list(DESTINATIONS)
    while pending:
        place = pending.pop()
        pending.extend(place.entries)
        names.update(place.views)
        if place.url_name is not None:
            names.add(place.url_name)
    return names


def _clinic_routes(resolver: URLResolver) -> Iterator[WorkspaceRoute]:
    registered = _registered_views()
    for route in workspace_routes(resolver):
        if "clinic_id" in route.kwargs or route.name in registered:
            yield route


def _prepared_client(graph: RbacGraph, role: str, state: str) -> Client:
    client, user = _client_for(graph, role)
    assert (
        _get(client, f"/scheduling/clinics/{graph.clinic_a}/agenda/").status_code == 200
    )
    if state != "revoked-unpinned":
        _pin_patient(client, graph, user, role)
    if state == "multi-clinic":
        with owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_b,
                user_id=user.pk,
                role=role,
            )
        assert (
            _get(client, f"/scheduling/clinics/{graph.clinic_b}/agenda/").status_code
            == 200
        )
    if state != "wrong-clinic":
        for permission in BUNDLES_V1[role]:
            remove_permission(graph, role, permission)
    return client


def _denial_differences(
    actual: _MonkeyPatchedWSGIResponse,
    unknown: _MonkeyPatchedWSGIResponse,
    *,
    complete: bool,
) -> list[str]:
    differences = []
    if unknown.status_code not in {403, 404}:
        differences.append("unknown was not refused")
    actual_body, actual_headers = _normalized_denial(actual)
    unknown_body, unknown_headers = _normalized_denial(unknown)
    if actual_headers.get("Set-Cookie") != unknown_headers.get("Set-Cookie"):
        differences.append("Set-Cookie")
    if complete and (
        actual.status_code != unknown.status_code
        or actual_body != unknown_body
        or actual_headers != unknown_headers
    ):
        differences.append("complete denial parity")
    return differences


@pytest.mark.parametrize("role", ["receptionist", "physician"])
@pytest.mark.parametrize(
    "state", ["revoked-unpinned", "revoked-pinned", "multi-clinic", "wrong-clinic"]
)
def test_registered_clinic_refusals_are_session_read_only(
    rbac_graph: RbacGraph,
    role: str,
    state: str,
    record_property: Callable[[str, object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.core.navigation.clinic_local_today", lambda _zone: "2033-05-18"
    )
    routes = tuple(_clinic_routes(get_resolver()))
    registered = _registered_views()
    assert routes
    # reverse() must not collapse distinct patterns onto the same named route.
    assert len(routes) == len({route.name for route in routes})
    assert registered <= {route.name for route in routes}
    client = _prepared_client(rbac_graph, role, state)
    snapshot = dict(client.session)
    target = rbac_graph.clinic_c if state == "wrong-clinic" else rbac_graph.clinic_a
    violations: list[tuple[str, str, str]] = []
    reached: set[str] = set()
    for route in routes:
        for method in ("GET", "POST"):
            responses = []
            for clinic_id in (target, uuid4()):
                _restore_session(client, snapshot)
                url = route.url(clinic_id)
                response = (
                    _get(client, url) if method == "GET" else _post(client, url, {})
                )
                match = response.wsgi_request.resolver_match
                assert match is not None
                assert match.view_name == route.name
                responses.append(response)
                if response.status_code in {403, 404}:
                    reached.add(route.name)
                    if response.wsgi_request.session.modified:
                        violations.append((route.name, method, "session.modified"))
                    if dict(client.session) != snapshot:
                        violations.append(
                            (route.name, method, "stored session changed")
                        )
            actual, unknown = responses
            if actual.status_code in {403, 404}:
                violations.extend(
                    (route.name, method, problem)
                    for problem in _denial_differences(
                        actual, unknown, complete=route.name in registered
                    )
                )
    record_property("discovered_routes", sorted(route.name for route in routes))
    record_property("refused_routes", sorted(reached))
    record_property("refusal_violations", json.dumps(violations))
    assert reached == {route.name for route in routes}, (
        "A discovered route was not probed under refusal"
    )
    # Values and cookie credentials never enter failure artifacts.
    assert not violations, violations
