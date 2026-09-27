"""Authorised-actor probes prove that refusal branches run inside actual views."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import wraps
from itertools import product
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.core.api.errors import UI_API_PREFIX
from django.urls import get_resolver

from core.test_navigation import _client_for, _csrf, _get
from core.test_workspace_boundaries import _pin_patient, _restore_session
from otp_test_support import fixed_otp_time, runtime_role
from workspace_refusal_support import workspace_routes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.http import HttpRequest, HttpResponseBase
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph
    from workspace_refusal_support import WorkspaceRoute

pytestmark = pytest.mark.django_db(transaction=True)

# These are semantic classifications, not exclusions from discovery or execution.
# A new refusal makes a classification stale; a new unclassified route fails closed.
NO_IN_VIEW_REFUSAL = {
    "workspace-home": "Agenda redirect or no-clinic page; no refusal branch.",
    "identity:protected": "Authenticated landing has no input or refusal branch.",
    "identity:preferences": "Own preference form returns 200 or validation 400.",
    "service-worker": "GET-only static worker rendering; no refusal branch.",
    "ui_api:command-search": (
        "For a valid staff User, unknown/foreign clinics return 200 []; "
        "invalid JSON/schema returns 400, not a clinic refusal."
    ),
}


def _requests(
    graph: RbacGraph, known_encounter: str | None
) -> Iterator[tuple[str, dict[str, str], bool]]:
    unknown = str(uuid4())
    for htmx in (False, True):
        if known_encounter is not None:
            # Pass encounter authorization, then refuse the requested action.
            yield (
                "POST",
                {"action": "invalid", "encounter_id": known_encounter},
                htmx,
            )
        yield "GET", {}, htmx
        yield "POST", {}, htmx
        yield (
            "POST",
            {"action": "invalid", "token": "invalid", "clinic_id": "invalid"},
            htmx,
        )
        yield (
            "POST",
            {
                "action": "open",
                "enrollment_id": unknown,
                "encounter_id": unknown,
                "document_id": unknown,
                "token": unknown,
                "clinic_id": str(graph.clinic_c),
                "q": "Synthetic Unknown Patient",
                "view": "day",
                "date": "2033-05-18",
                "page": "1",
            },
            htmx,
        )


def _send(
    client: Client,
    url: str,
    method: str,
    data: dict[str, str],
    htmx: bool,
) -> _MonkeyPatchedWSGIResponse:
    headers = {"HX-Request": "true"} if htmx else {}
    with runtime_role(), fixed_otp_time():
        if method == "GET":
            return client.get(url, headers=headers)
        headers.update(_csrf(client))
        if url.startswith(UI_API_PREFIX):
            return client.post(
                url,
                data=json.dumps(data),
                content_type="application/json",
                headers=headers,
            )
        return client.post(url, data, headers=headers)


def _probe_route(
    route: WorkspaceRoute,
    client: Client,
    graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, set[int]]:
    original = cast("Callable[..., HttpResponseBase]", route.pattern.callback)
    entries: list[HttpRequest] = []

    @wraps(original)
    def entered(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        entries.append(request)
        return original(request, *args, **kwargs)

    snapshot = dict(client.session)
    known_encounter: str | None = client.session.get(f"ehr.encounter.{graph.clinic_a}")
    if known_encounter is not None and "encounter_id" in route.kwargs:
        route = replace(route, kwargs={**route.kwargs, "encounter_id": known_encounter})
    refusals = 0
    statuses = set()
    with monkeypatch.context() as patch:
        patch.setattr(route.pattern, "callback", entered)
        for method, data, htmx in _requests(graph, known_encounter):
            _restore_session(client, snapshot)
            before = len(entries)
            response = _send(client, route.url(graph.clinic_a), method, data, htmx)
            assert response.status_code < 500, (route.name, method, htmx)
            statuses.add(response.status_code)
            if response.status_code in {403, 404} and len(entries) > before:
                refusals += 1
                assert response.wsgi_request.session.modified is False
                assert dict(client.session) == snapshot
                assert not response.cookies
    return refusals, statuses


def test_authorised_in_view_refusals_are_session_read_only(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setattr(
        "apps.core.navigation.clinic_local_today", lambda _zone: "2033-05-18"
    )
    routes = tuple(workspace_routes(get_resolver()))
    names = {route.name for route in routes}
    assert len(routes) == len(names)
    assert NO_IN_VIEW_REFUSAL.keys() <= names
    observed: dict[str, int] = dict.fromkeys(names, 0)
    statuses: dict[str, set[int]] = {name: set() for name in names}
    profiles = [
        *product(("receptionist", "physician", "clinic_admin"), (False, True)),
        ("finance", False),
    ]
    for role, pinned in profiles:
        client, user = _client_for(rbac_graph, role)
        # Finance is authenticated but has no visible workspace clinic. This
        # executes _require_clinic's refusal inside the session-scoped views.
        assert _get(client, "/auth/protected/").status_code == 200
        if pinned:
            _pin_patient(client, rbac_graph, user, role)
            if role == "physician":
                assert f"ehr.encounter.{rbac_graph.clinic_a}" in client.session
        for route in routes:
            refused, codes = _probe_route(route, client, rbac_graph, monkeypatch)
            observed[route.name] += refused
            statuses[route.name].update(codes)
    record_property("in_view_refusals", json.dumps(observed, sort_keys=True))
    record_property(
        "response_statuses",
        json.dumps({n: sorted(c) for n, c in statuses.items()}, sort_keys=True),
    )
    record_property(
        "no_in_view_refusal", json.dumps(NO_IN_VIEW_REFUSAL, sort_keys=True)
    )
    missing = (
        names
        - {name for name, count in observed.items() if count}
        - NO_IN_VIEW_REFUSAL.keys()
    )
    assert not missing, (
        "No in-view refusal or reviewed classification",
        sorted(missing),
    )
    assert not {name for name in NO_IN_VIEW_REFUSAL if observed[name]}, (
        "Stale no-refusal classification"
    )
