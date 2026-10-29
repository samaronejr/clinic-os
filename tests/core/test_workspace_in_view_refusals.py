"""Authorised-actor probes prove that refusal branches run inside actual views."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import wraps
from hashlib import sha256
from itertools import product
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from apps.core.api.errors import UI_API_PREFIX
from apps.ehr.services import SOAP_FIELDS
from apps.identity.account_views import request_session_key
from apps.identity.models import UserClinicRole
from django.contrib.sessions.models import Session
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.test import Client, override_settings
from django.urls import get_resolver, path

from core.test_navigation import _client_for, _csrf, _get, _reauthenticate
from core.test_workspace_boundaries import _pin_patient, _restore_session
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, runtime_role
from workspace_refusal_support import workspace_routes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from datetime import datetime

    from django.http import HttpResponseBase
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
    "ui_api:inbox-query": (
        "For a valid staff User, refused, unknown and foreign clinics return the "
        "200 empty inbox page; invalid JSON/schema returns 400, not a refusal."
    ),
}


# The UI API autosave routes parse a strict JSON schema before their refusal
# branch, so the form-shaped requests below end in a 400 there. Schema-valid
# bodies naming an unknown record, in the actor's clinic and in the foreign
# clinic, reach each view's own unknown/wrong-scope refusal.
AUTOSAVE_ROUTES = frozenset({"ui_api:ehr-autosave", "ui_api:ehr-addendum-autosave"})


def _missing_framework_key(request: HttpRequest) -> HttpResponse:
    """Exercise the real boundary parser with SessionMiddleware's unsaved key."""
    return HttpResponse(request_session_key(request))


urlpatterns = [path("auth/login/", _missing_framework_key)]


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
            {
                "action": "retire",
                "retire-kind": "resource",
                "retire-record_id": unknown,
            },
            htmx,
        )
        yield (
            "POST",
            {
                "action": "cancel",
                "task_id": unknown,
                "run_id": unknown,
                "definition_id": unknown,
                "expected_revision": "1",
                "idempotency_key": str(uuid4()),
            },
            htmx,
        )
        yield (
            "POST",
            {
                "action": "invalid",
                "token": "invalid",
                "clinic_id": "invalid",
                "version_id": unknown,
            },
            htmx,
        )
        yield (
            "POST",
            {
                "action": "open",
                "enrollment_id": unknown,
                "encounter_id": unknown,
                "document_id": unknown,
                "session_id": unknown,
                "reset_id": unknown,
                "passkey_id": unknown,
                "token": unknown,
                "clinic_id": str(graph.clinic_c),
                "q": "Synthetic Unknown Patient",
                "view": "day",
                "date": "2033-05-18",
                "page": "1",
            },
            htmx,
        )


def _autosave_requests(
    graph: RbacGraph, route_name: str
) -> Iterator[tuple[str, Mapping[str, object], bool]]:
    if route_name not in AUTOSAVE_ROUTES:
        return
    unknown = str(uuid4())
    record: dict[str, object] = (
        {
            "version_id": unknown,
            "editor_session": str(uuid4()),
            "sections": dict.fromkeys(SOAP_FIELDS, ""),
        }
        if route_name == "ui_api:ehr-autosave"
        else {"addendum_id": unknown, "text": ""}
    )
    for htmx in (False, True):
        for clinic in (graph.clinic_a, graph.clinic_c):
            yield (
                "POST",
                {
                    **record,
                    "clinic_id": str(clinic),
                    "expected_revision": 1,
                    "editor_command_id": str(uuid4()),
                },
                htmx,
            )


def _send(
    client: Client,
    url: str,
    method: str,
    data: Mapping[str, object],
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
    *,
    attempts: tuple[tuple[str, Mapping[str, object], bool], ...] | None = None,
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
        clinics = (
            (graph.clinic_a, uuid4())
            if route.name == "identity:recovery"
            else (graph.clinic_a,)
        )
        for clinic_id in clinics:
            requests = (
                (
                    *_requests(graph, known_encounter),
                    *_autosave_requests(graph, route.name),
                )
                if attempts is None
                else attempts
            )
            for method, data, htmx in requests:
                _restore_session(client, snapshot)
                before = len(entries)
                response = _send(client, route.url(clinic_id), method, data, htmx)
                assert response.status_code < 500, (route.name, method, htmx)
                statuses.add(response.status_code)
                if response.status_code in {403, 404} and len(entries) > before:
                    refusals += 1
                    assert response.wsgi_request.session.modified is False
                    assert dict(client.session) == snapshot
                    assert not response.cookies
    return refusals, statuses


def _passkey_page_refusal(
    graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> tuple[int, set[int]]:
    # Given a real authenticated member whose page is initially available.
    client, user = _client_for(graph, "receptionist")
    route = next(
        route
        for route in workspace_routes(get_resolver())
        if route.name == "identity:passkeys"
    )
    assert _get(client, route.url(graph.clinic_a)).status_code == 200
    session_key = client.session.session_key
    assert session_key is not None
    stored_states: list[tuple[bytes, datetime]] = []
    original = cast("Callable[..., HttpResponseBase]", route.pattern.callback)

    @wraps(original)
    def revoke_then_enter(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        # As in the resource/workflow probes, another connection revokes real
        # authority after middleware admission, before the unchanged view.
        with psycopg.connect(
            superuser_database_url,
            dbname=str(connection.settings_dict["NAME"]),
            autocommit=True,
        ) as owner:
            changed = owner.execute(
                "UPDATE clinic_app.identity_userclinicrole "
                "SET revoked_at=statement_timestamp() "
                "WHERE user_id=%s AND organization_id=%s AND revoked_at IS NULL",
                [user.pk, graph.organization_a],
            )
            assert changed.rowcount == 1
            row = owner.execute(
                "SELECT session_data,expire_date FROM clinic_app.django_session "
                "WHERE session_key=%s",
                [session_key],
            ).fetchone()
            assert row is not None
            stored_states.append((sha256(row[0].encode()).digest(), row[1]))
        return original(request, *args, **kwargs)

    # When the actual HTTP view rechecks its now-empty clinic membership.
    with monkeypatch.context() as patch:
        patch.setattr(route.pattern, "callback", revoke_then_enter)
        result = _probe_route(
            route, client, graph, patch, attempts=(("GET", {}, False),)
        )
    # Then the ordinary in-view oracle sees a read-only 403, not an early
    # middleware rejection; persisted session bytes and expiry also stay put.
    assert result == (1, {403})
    assert len(stored_states) == 1
    stored = Session.objects.get(session_key=session_key)
    assert (
        sha256(stored.session_data.encode()).digest(),
        stored.expire_date,
    ) == stored_states[0]
    return result


def test_passkey_page_refuses_membership_lost_after_admission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    _passkey_page_refusal(rbac_graph, monkeypatch, superuser_database_url)


def test_authorised_in_view_refusals_are_session_read_only(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    superuser_database_url: str,
) -> None:
    monkeypatch.setattr(
        "apps.core.navigation.clinic_local_today", lambda _zone: "2033-05-18"
    )
    monkeypatch.setattr("apps.identity.stepup._utc_now_seconds", lambda: 2_000_000_000)
    routes = tuple(workspace_routes(get_resolver()))
    names = {route.name for route in routes}
    assert len(routes) == len(names)
    assert NO_IN_VIEW_REFUSAL.keys() <= names
    assert names >= AUTOSAVE_ROUTES
    observed: dict[str, int] = dict.fromkeys(names, 0)
    statuses: dict[str, set[int]] = {name: set() for name in names}
    profiles = [
        *(
            (role, pinned, 1)
            for role, pinned in product(
                ("receptionist", "physician", "clinic_admin"), (False, True)
            )
        ),
        ("finance", False, 1),
        ("clinic_admin", False, 2),
        ("clinic_admin", True, 2),
    ]
    authorized_targets = {
        "scheduling:resource-settings",
        "workflows:designer",
        "workflows:exceptions",
        "workflows:run-exceptions",
        "workflows:runs",
        "workflows:tasks",
    }
    for role, pinned, bundle in profiles:
        client, user = _client_for(rbac_graph, role)
        if bundle == 2:
            # Owner-side issuance is enforced by the real database trigger.
            # _client_for performs real confirmed-device HTTP verification.
            with owner_context(rbac_graph.organization_a):
                assert (
                    UserClinicRole.objects.filter(
                        user_id=user.pk, clinic_id=rbac_graph.clinic_a
                    ).update(workflow_bundle_version=bundle)
                    == 1
                )
            _reauthenticate(client, user.pk, role)
        # Finance is authenticated but has no visible workspace clinic. This
        # executes _require_clinic's refusal inside the session-scoped views.
        assert _get(client, "/auth/protected/").status_code == 200
        if pinned:
            _pin_patient(client, rbac_graph, user, role)
            if role == "physician":
                assert f"ehr.encounter.{rbac_graph.clinic_a}" in client.session
        for route in routes:
            if bundle == 2 and route.name in authorized_targets:
                assert _get(client, route.url(rbac_graph.clinic_a)).status_code == 200
            refused, codes = _probe_route(route, client, rbac_graph, monkeypatch)
            observed[route.name] += refused
            statuses[route.name].update(codes)
    refused, codes = _passkey_page_refusal(
        rbac_graph, monkeypatch, superuser_database_url
    )
    observed["identity:passkeys"] += refused
    statuses["identity:passkeys"].update(codes)
    # The authenticated routes cannot have an absent framework key. A thin
    # HTTP adapter covers the parser's denial using a real anonymous session,
    # without changing the resolver-derived oracle or pretending auth passed.
    with override_settings(ROOT_URLCONF=__name__):
        missing_key = Client(enforce_csrf_checks=True).get("/auth/login/")
    assert missing_key.status_code == 403
    assert missing_key.wsgi_request.session.session_key is None
    assert missing_key.wsgi_request.session.modified is False
    assert not missing_key.cookies
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
