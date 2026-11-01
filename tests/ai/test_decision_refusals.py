"""Native and enhanced decision refusals after real middleware admission."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from apps.ai import decision_views
from apps.ai.decision_forms import DecisionSupportForm
from apps.ai.decision_support import request_decision_support
from django.conf import settings
from django.urls import get_resolver, reverse

from ai.decision_support_helpers import enable, world_for
from ai.refusal_support import refusal_state
from ehr.test_addenda import signed_in
from workspace_refusal_support import workspace_routes

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.ai.decision_support import DecisionPanel
    from django.http import HttpRequest, HttpResponseBase

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_authority_loss_after_generation_rolls_back_every_effect(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
    enhanced: bool,
) -> None:
    graph = rbac_graph
    world = world_for(graph, monkeypatch)
    enable(world)
    attempted: list[UUID] = []

    def revoke_after_generation(
        *,
        clinic_id: UUID,
        encounter_id: UUID,
        session_binding: str,
        opt_in: bool = False,
    ) -> DecisionPanel:
        panel = request_decision_support(
            clinic_id=clinic_id,
            encounter_id=encounter_id,
            session_binding=session_binding,
            opt_in=opt_in,
        )
        assert panel.invocation_id is not None
        attempted.append(panel.invocation_id)
        with psycopg.connect(superuser_database_url, autocommit=True) as owner:
            changed = owner.execute(
                "UPDATE clinic_app.identity_professionalregistration "
                "SET revoked_at=statement_timestamp() "
                "WHERE user_id=%s AND clinic_id=%s AND revoked_at IS NULL",
                [graph.physician, clinic_id],
            )
            assert changed.rowcount >= 1
        return panel

    monkeypatch.setattr(
        decision_views, "request_decision_support", revoke_after_generation
    )
    with signed_in(graph.physician) as client:
        session = dict(client.session)
        before = refusal_state(superuser_database_url)
        publications = list(world.gateway.publications)
        response = client.post(
            reverse("ai:decision-support", args=(graph.clinic_a,)),
            {
                "encounter_id": str(world.chart.version.document.encounter_id),
                "command": "generate",
                "opt_in": "yes",
            },
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert len(attempted) == 1
        assert response.status_code == 403
        assert response.wsgi_request.session.modified is False
        assert dict(client.session) == session
        assert settings.SESSION_COOKIE_NAME not in response.cookies
        assert refusal_state(superuser_database_url) == before
        assert world.gateway.publications == publications


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_decision_missing_session_key_refuses_without_side_effects(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
    enhanced: bool,
) -> None:
    graph = rbac_graph
    world = world_for(graph, monkeypatch)
    route = next(
        route
        for route in workspace_routes(get_resolver())
        if route.name == "ai:decision-support"
    )
    original = cast("Callable[..., HttpResponseBase]", route.pattern.callback)

    def lose_framework_key(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
        # Authentication, OTP and workspace middleware have already admitted the
        # real actor. Model a lost framework key without changing cached session
        # contents or its modified flag; SessionMiddleware must not repair it.
        assert request.user.pk == graph.physician
        assert request.session.session_key is not None
        assert dict(request.session)
        assert request.session.modified is False
        request.session._session_key = None
        return original(request, clinic_id)

    with signed_in(graph.physician) as client:
        session = dict(client.session)
        before = refusal_state(superuser_database_url)
        monkeypatch.setattr(route.pattern, "callback", lose_framework_key)
        bodies = []
        for encounter in (world.chart.version.document.encounter_id, uuid4()):
            response = client.post(
                reverse("ai:decision-support", args=(graph.clinic_a,)),
                {"encounter_id": str(encounter), "command": "open"},
                HTTP_HX_REQUEST="true" if enhanced else "false",
            )
            assert response.status_code == 403
            assert response.wsgi_request.session.session_key is None
            assert response.wsgi_request.session.modified is False
            assert dict(response.wsgi_request.session) == session
            assert dict(client.session) == session
            assert settings.SESSION_COOKIE_NAME not in response.cookies
            assert str(encounter).encode() not in response.content
            assert refusal_state(superuser_database_url) == before
            bodies.append(response.content)
        assert bodies[0] == bodies[1]
    assert world.gateway.publications == []


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_decision_input_bound_refuses_without_side_effects(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
    enhanced: bool,
) -> None:
    graph = rbac_graph
    world = world_for(graph, monkeypatch)
    enable(world)
    body = {
        "encounter_id": str(world.chart.version.document.encounter_id),
        "command": "generate",
        "opt_in": "yes",
    }
    assert DecisionSupportForm(body).is_valid()
    # Keep real chart assembly, parser and nested rollback. A reduced source
    # ceiling makes this stored chart exceed the protocol bound without
    # replacing a service with an unconditional exception.
    monkeypatch.setattr("apps.ai.brief_contracts.MAX_SOURCES", 0)
    with signed_in(graph.physician) as client:
        session = dict(client.session)
        before = refusal_state(superuser_database_url)
        bodies = []
        for data in (body, {**body, "command": "unsupported"}):
            response = client.post(
                reverse("ai:decision-support", args=(graph.clinic_a,)),
                data,
                HTTP_HX_REQUEST="true" if enhanced else "false",
            )
            assert response.status_code == 400
            assert response.wsgi_request.session.modified is False
            assert dict(client.session) == session
            assert settings.SESSION_COOKIE_NAME not in response.cookies
            assert body["encounter_id"].encode() not in response.content
            assert refusal_state(superuser_database_url) == before
            bodies.append(response.content)
        assert bodies[0] == bodies[1]
    assert world.gateway.publications == []
