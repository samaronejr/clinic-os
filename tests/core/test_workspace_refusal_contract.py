"""Resolver-derived tripwire: a staff-clinic refusal cannot mutate its session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.core.navigation import DESTINATIONS
from apps.core.patient_context import PATIENT_BOUND_VIEWS
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.tenancy.middleware import PATIENT_PREFIX
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.urls.converters import IntConverter, StringConverter, UUIDConverter

from core.test_navigation import _client_for, _get, _post
from core.test_workspace_boundaries import (
    _normalized_denial,
    _pin_patient,
    _restore_session,
    remove_permission,
)
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True)
class ClinicRoute:
    name: str
    kwargs: dict[str, object]

    def url(self, clinic_id: UUID) -> str:
        kwargs = dict(self.kwargs)
        if "clinic_id" in kwargs:
            kwargs["clinic_id"] = clinic_id
        return reverse(self.name, kwargs=kwargs)


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


def _sample(converter: object) -> object:
    if isinstance(converter, UUIDConverter):
        return uuid4()
    if isinstance(converter, IntConverter):
        return 1
    if isinstance(converter, StringConverter):
        return "synthetic"
    pytest.fail(f"Uninspected URL converter: {type(converter).__name__}")


def _clinic_routes(
    resolver: URLResolver,
    namespace: str = "",
    inherited: dict[str, object] | None = None,
) -> Iterator[ClinicRoute]:
    for entry in resolver.url_patterns:
        converters = {**(inherited or {}), **entry.pattern.converters}
        if isinstance(entry, URLResolver):
            prefix = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from _clinic_routes(entry, prefix, converters)
        else:
            assert isinstance(entry, URLPattern)
            parameters = entry.pattern.regex.groupindex.keys() | converters.keys()
            if (
                "clinic_id" not in parameters
                and f"{namespace}{entry.name}" not in _registered_views()
            ):
                continue
            assert entry.name is not None, "Clinic routes must be named and reversible"
            assert parameters <= converters.keys(), "Uninspected regex route parameters"
            route = ClinicRoute(
                f"{namespace}{entry.name}",
                {key: _sample(value) for key, value in converters.items()},
            )
            # Unregistered patient-portal entrypoints use a different authority.
            if route.name in _registered_views() or not route.url(uuid4()).startswith(
                PATIENT_PREFIX
            ):
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
