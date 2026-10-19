"""Todo 24 board refusals use the worker database, not scheduling worlds.

The refusal-exit gate credits only exits that the parallel phase reaches, and
``tests/scheduling``'s autouse world keeps the board module serial; these
witnesses therefore live here and reach every board and board-API exit.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.scheduling.access import AvailabilityAccessDeniedError
from apps.scheduling.agenda_board import AgendaFilters, view_agenda_board
from apps.scheduling.agenda_board_views import NO_PATIENT_MESSAGE
from apps.scheduling.agenda_queries import AgendaInputError
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context
from django.utils.translation import gettext

from core.test_ui_api import ApiSession, _error, _signed_in
from otp_test_support import OTP_RAW_CREDENTIAL, runtime_role
from patient_http_support import audit_event_types
from scheduling.test_agenda_board import (
    BOARD_API,
    DAY,
    _action,
    _board,
    _grid,
    _hold,
    _pin,
    _reload,
    _transition,
    _world,
)

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)

NONCE: Final = re.compile(rb'(nonce|value)="[^"]*"')


def _refusal_body(response: _MonkeyPatchedWSGIResponse) -> bytes:
    return NONCE.sub(b'\\1=""', response.content)


def _board_refusals(
    setup: AppointmentSetup, booked: Appointment, graph: RbacGraph
) -> dict[str, tuple[str, str, dict[str, str]]]:
    clinic = setup.clinic_id
    move = _transition(booked, "arrive")
    hold = _hold(setup.practitioner_id)
    find = {
        "intent": "find",
        "view": "day",
        "day": DAY,
        "appointment_id": str(booked.pk),
        "expected_revision": str(booked.revision),
        "duration": "60",
    }
    return {
        "unknown clinic board": ("get", _grid(uuid4()), {}),
        "unknown view": ("get", _board(clinic, "synthetic"), {}),
        "malformed day": ("get", _grid(clinic, "2035-02-30"), {}),
        "extra preference field": ("post", _grid(clinic), {"intent": "clear", "x": ""}),
        "preference view": ("post", _board(clinic, "synthetic"), {"intent": "clear"}),
        "preference day": ("post", _grid(clinic, "x"), {"intent": "clear"}),
        "noncanonical day": ("post", _grid(clinic, "20350602"), {"intent": "clear"}),
        "unknown intent": ("post", _grid(clinic), {"intent": "delete"}),
        "foreign clinic": ("post", _grid(graph.clinic_b), {"intent": "clear"}),
        "malformed unit": ("post", _grid(clinic), {"intent": "unit", "unit": "x"}),
        "unknown saved view": (
            "post",
            _grid(clinic),
            {"intent": "open", "saved_view": str(uuid4())},
        ),
        "archive unknown": (
            "post",
            _grid(clinic),
            {"intent": "archive", "saved_view": str(uuid4())},
        ),
        "status filter": ("post", _grid(clinic), {"intent": "filter", "status": "x"}),
        "unknown professional": (
            "post",
            _grid(clinic),
            {"intent": "filter", "professional": str(uuid4())},
        ),
        "extra action field": ("post", _action(clinic), move | {"note": "x"}),
        "action intent": ("post", _action(clinic), move | {"intent": "delete"}),
        "action day": ("post", _action(clinic), move | {"day": "2035-13-01"}),
        "action unknown clinic": ("post", _action(uuid4()), move),
        "unknown appointment": (
            "post",
            _action(clinic),
            move | {"appointment_id": str(uuid4())},
        ),
        "malformed revision": (
            "post",
            _action(clinic),
            move | {"expected_revision": "x"},
        ),
        "malformed command": ("post", _action(clinic), move | {"command_id": "x"}),
        "hold practitioner": ("post", _action(clinic), hold | {"practitioner": ""}),
        "hold duration": ("post", _action(clinic), hold | {"duration": "x"}),
        "hold clock": ("post", _action(clinic), hold | {"start": "25:00"}),
        "hold resources": (
            "post",
            _action(clinic),
            hold | {"resources": str(uuid4())},
        ),
        "find revision": ("post", _action(clinic), find | {"expected_revision": "x"}),
        "find duration": ("post", _action(clinic), find | {"duration": "0"}),
    }


def test_every_board_refusal_is_the_unknown_clinic_404_with_no_side_effect(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist, setup, booked = _world(rbac_graph)
    _pin(client, setup.clinic_id, setup.enrollment_id)
    with runtime_role():
        baseline = client.get(_grid(uuid4()))
    before_events = audit_event_types(rbac_graph, receptionist.pk)
    with runtime_role():
        responses = {
            name: client.get(url) if method == "get" else client.post(url, data)
            for name, (method, url, data) in _board_refusals(
                setup, booked, rbac_graph
            ).items()
        }
    assert baseline.status_code == 404
    for name, response in responses.items():
        assert response.status_code == 404, name
        assert _refusal_body(response) == _refusal_body(baseline), name
        assert "sessionid" not in response.cookies, name
        assert not response.wsgi_request.session.modified, name
    assert audit_event_types(rbac_graph, receptionist.pk) == before_events
    current = _reload(setup, booked.pk)
    assert (current.status, current.revision) == (booked.status, booked.revision)


def test_a_hold_without_a_patient_in_context_writes_nothing(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist, setup, _ = _world(rbac_graph)
    with runtime_role():
        refused = client.post(_action(setup.clinic_id), _hold(setup.practitioner_id))
    assert refused.status_code == 409
    assert str(NO_PATIENT_MESSAGE).encode() in refused.content
    with runtime_role(), tenant_context(receptionist.pk, setup.organization_id):
        assert not Appointment.objects.filter(status=Appointment.Status.HELD).exists()


def test_a_no_show_before_the_start_and_a_stale_revision_answer_409(
    rbac_graph: RbacGraph,
) -> None:
    client, _, setup, booked = _world(rbac_graph)
    with runtime_role():
        early = client.post(
            _action(setup.clinic_id),
            _transition(booked, "no_show"),
            HTTP_HX_REQUEST="true",
        )
        stale = client.post(
            _action(setup.clinic_id),
            _transition(booked, "arrive", expected_revision=str(booked.revision + 5)),
        )
    assert early.status_code == 409
    assert stale.status_code == 409
    assert gettext("Appointment not changed").encode() in stale.content
    current = _reload(setup, booked.pk)
    assert (current.status, current.revision) == (booked.status, booked.revision)


def test_the_board_api_refuses_unknown_clinics_and_unknown_filters(
    rbac_graph: RbacGraph,
) -> None:
    _, receptionist, setup, _ = _world(rbac_graph)
    with runtime_role():
        api = _signed_in(receptionist.username, OTP_RAW_CREDENTIAL)
        unknown = api.post(
            json.dumps({"clinic_id": str(uuid4()), "view": "day", "date": DAY}),
            path=BOARD_API,
        )
        bad_filter = api.post(
            json.dumps(
                {
                    "clinic_id": str(setup.clinic_id),
                    "view": "week",
                    "date": DAY,
                    "filters": {"professional_id": str(uuid4())},
                }
            ),
            path=BOARD_API,
        )
    assert unknown.status_code == 403
    assert json.loads(unknown.content) == _error("access_denied")
    assert bad_filter.status_code == 400
    assert json.loads(bad_filter.content) == _error("invalid_input")


def test_the_board_api_needs_a_clinic_from_the_body_or_the_shell(
    rbac_graph: RbacGraph,
) -> None:
    _, receptionist, _, _ = _world(rbac_graph)
    with runtime_role():
        api: ApiSession = _signed_in(receptionist.username, OTP_RAW_CREDENTIAL)
        response = api.post(json.dumps({"view": "month", "date": DAY}), path=BOARD_API)
    assert response.status_code == 403
    assert json.loads(response.content) == _error("access_denied")


def test_board_service_refuses_malformed_scope_and_filters(
    rbac_graph: RbacGraph,
) -> None:
    _, receptionist, setup, _ = _world(rbac_graph)
    with runtime_role(), tenant_context(receptionist.pk, setup.organization_id):
        with pytest.raises(AvailabilityAccessDeniedError):
            view_agenda_board(clinic_id=uuid4(), view="day", date=DAY)
        with pytest.raises(AgendaInputError):
            view_agenda_board(clinic_id=setup.clinic_id, view="year", date=DAY)
        with pytest.raises(AgendaInputError):
            view_agenda_board(
                clinic_id=setup.clinic_id,
                view="day",
                date=DAY,
                filters=AgendaFilters(status="unknown"),
            )
