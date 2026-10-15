"""Todo 23 grid refusals use the worker database, not scheduling worlds."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.scheduling.agenda_grid_views import REVISION_MESSAGE

from identity.permission_support import owner_context
from patient_http_support import audit_event_types
from patient_service_support import runtime_role
from scheduling.appointment_http_support import APPOINTMENT_RESCHEDULED_EVENT
from scheduling.test_agenda_grid import (
    _grid,
    _move,
    _payload,
    _refusal_body,
    _reload,
    _seeded,
)
from scheduling.test_resource_role_matrix import _verified_client

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_stale_move_answers_409_with_the_other_change_and_writes_nothing(
    rbac_graph: RbacGraph,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    with runtime_role():
        first = client.post(
            _move(context.clinic_id), _payload(booked), HTTP_HX_REQUEST="true"
        )
        after = _reload(context, booked.pk)
        stale = client.post(
            _move(context.clinic_id),
            _payload(booked, start="11:00"),
            HTTP_HX_REQUEST="true",
        )
    assert first.status_code == 200
    assert stale.status_code == 409
    assert str(REVISION_MESSAGE).encode() in stale.content
    current = _reload(context, booked.pk)
    assert (current.start_at, current.revision) == (after.start_at, after.revision)
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(APPOINTMENT_RESCHEDULED_EVENT) == 1


@pytest.mark.parametrize("selection", [False, True])
def test_every_refused_move_is_the_unknown_clinic_404_with_no_side_effect(
    rbac_graph: RbacGraph,
    selection: bool,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    fields = {"select": "1"} if selection else {}
    with runtime_role():
        warmup = client.get(_grid(context.clinic_id))
    before_events = audit_event_types(rbac_graph, context.actor)
    with runtime_role():
        baseline = client.post(_move(uuid4()), _payload(booked, **fields))
        cases = {
            "unknown appointment": (
                _move(context.clinic_id),
                {"appointment_id": str(uuid4())},
            ),
            "foreign clinic": (_move(rbac_graph.clinic_b), {}),
            "extra field": (_move(context.clinic_id), {"note": "x"}),
            "malformed start": (_move(context.clinic_id), {"start": "25:00"}),
            "malformed day": (_move(context.clinic_id), {"day": "2031-02-30"}),
            "zero duration": (_move(context.clinic_id), {"duration": "0"}),
            "empty revision": (_move(context.clinic_id), {"expected_revision": ""}),
        }
        responses = {
            name: client.post(url, _payload(booked, **(fields | overrides)))
            for name, (url, overrides) in cases.items()
        }
    assert warmup.status_code == 200
    assert baseline.status_code == 404
    assert "sessionid" not in baseline.cookies
    for name, response in responses.items():
        assert response.status_code == 404, name
        assert _refusal_body(response) == _refusal_body(baseline), name
        assert "sessionid" not in response.cookies, name
        assert set(response.cookies) == set(baseline.cookies), name
    assert _reload(context, booked.pk).revision == booked.revision
    events = audit_event_types(rbac_graph, context.actor)
    assert events == before_events


def test_native_selection_refuses_unknown_and_other_clinic_ids_without_a_view_event(
    rbac_graph: RbacGraph,
) -> None:
    _, context, booked = _seeded(rbac_graph)
    # The actor may open both clinics: only the explicit selector scope, not
    # lack of permission in the appointment's clinic, can refuse this request.
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            user_id=rbac_graph.clinic_admin,
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    client = _verified_client(User.objects.get(pk=rbac_graph.clinic_admin))
    before_events = audit_event_types(rbac_graph, rbac_graph.clinic_admin)
    with runtime_role():
        unknown = client.post(
            _move(rbac_graph.clinic_b),
            _payload(booked, select="1", appointment_id=str(uuid4())),
        )
        foreign = client.post(_move(rbac_graph.clinic_b), _payload(booked, select="1"))
    assert [unknown.status_code, foreign.status_code] == [404, 404]
    assert _refusal_body(unknown) == _refusal_body(foreign)
    assert set(unknown.cookies) == set(foreign.cookies) == set()
    assert audit_event_types(rbac_graph, rbac_graph.clinic_admin) == before_events
    assert _reload(context, booked.pk).revision == booked.revision


def test_native_selection_keeps_the_rendered_revision_for_the_move(
    rbac_graph: RbacGraph,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    with runtime_role():
        selected = client.post(_move(context.clinic_id), _payload(booked, select="1"))
    assert selected.status_code == 200
    assert b"data-native-move-form" in selected.content
    assert selected.context["expected_revision"] == booked.revision
    assert _reload(context, booked.pk).revision == booked.revision
    with runtime_role():
        winner = client.post(_move(context.clinic_id), _payload(booked))
        stale = client.post(_move(context.clinic_id), _payload(booked, start="11:00"))
    assert winner.status_code == 303
    assert stale.status_code == 409
    assert str(REVISION_MESSAGE).encode() in stale.content
    assert _reload(context, booked.pk).revision == booked.revision + 1
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(APPOINTMENT_RESCHEDULED_EVENT) == 1


def test_grid_refuses_unknown_clinics_and_malformed_days_identically(
    rbac_graph: RbacGraph,
) -> None:
    client, context, _ = _seeded(rbac_graph)
    with runtime_role():
        unknown = client.get(_grid(uuid4()))
        foreign = client.get(_grid(rbac_graph.clinic_b))
        malformed = client.get(_grid(context.clinic_id, "2031-02-30"))
    assert [unknown.status_code, foreign.status_code, malformed.status_code] == [
        404,
        404,
        404,
    ]
    assert _refusal_body(foreign) == _refusal_body(unknown)
    assert _refusal_body(malformed) == _refusal_body(unknown)
