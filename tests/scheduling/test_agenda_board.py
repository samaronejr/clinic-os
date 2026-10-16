"""Todo 24 agenda board: views, server-side filters, saved views and actions.

The board is the todo 23 HTMX grid widened to day (professionals, rooms and
equipment), week (one lane), month (counts) and list. URLs carry only the
clinic, view and civil date; filters live in the session, saved views in
``identity.saved_views``, and every record selector in a POST body. Refusals
are the unknown-clinic 404 with no session or record write.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from queue import Queue
from threading import Barrier, Thread
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.core.patient_context import SESSION_PREFIX
from apps.identity.models import User, UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import AvailabilityAccessDeniedError
from apps.scheduling.agenda_board import (
    CROSS_ORGANIZATION_BUSY,
    AgendaFilters,
    view_agenda_board,
)
from apps.scheduling.agenda_board_presenter import (
    filters_from_session,
    session_key,
)
from apps.scheduling.agenda_board_views import (
    NO_PATIENT_MESSAGE,
    OUTCOMES,
    TAKEN_MESSAGE,
)
from apps.scheduling.agenda_queries import AgendaInputError
from apps.scheduling.models import Appointment
from apps.scheduling.resource_definitions import ResourceInput, create_resource
from apps.scheduling.services import (
    AppointmentLocalRange,
    create_appointment,
    create_availability,
)
from apps.tenancy.db import tenant_context
from django.db import connections, transaction
from django.utils.translation import gettext

from auth.stepup_test_support import create_role_actor
from core.test_ui_api import ApiSession, _error, _signed_in
from identity.permission_support import owner_context
from otp_test_support import OTP_RAW_CREDENTIAL, runtime_role
from patient_http_support import (
    audit_event_types,
    receptionist_client,
    verified_physician_client,
)
from scheduling.appointment_http_support import AGENDA_VIEWED_EVENT, seed_enrollment
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from scheduling.test_resource_role_matrix import _verified_client
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)

DAY: Final = "2035-06-02"
NONCE: Final = re.compile(rb'(nonce|value)="[^"]*"')
ROOM: Final = "Sala Sintetica Board"
EQUIPMENT: Final = "Ultrassom Sintetico Board"


class _Values(HTMLParser):
    """Collect the values of one named input (opaque ids in POST forms)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "input" and found.get("name") == self.name:
            self.values.append(found.get("value") or "")


def _values(body: bytes, name: str) -> list[str]:
    parser = _Values(name)
    parser.feed(body.decode())
    return parser.values


def _grid(clinic_id: UUID, day: str = DAY) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/grid/{day}/"


def _board(clinic_id: UUID, view: str, day: str = DAY) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/board/{view}/{day}/"


def _action(clinic_id: UUID) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/grid/action/"


def _refusal_body(response: _MonkeyPatchedWSGIResponse) -> bytes:
    return NONCE.sub(b'\\1=""', response.content)


def _world(graph: RbacGraph) -> tuple[Client, User, AppointmentSetup, Appointment]:
    client, receptionist = receptionist_client(graph)
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = create_synthetic_appointment(setup)
        booked.refresh_from_db()
        create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name=ROOM, kind="room"),
        )
        create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name=EQUIPMENT, kind="equipment"),
        )
    return client, receptionist, setup, booked


def _pin(client: Client, clinic_id: UUID, enrollment_id: UUID) -> None:
    with runtime_role():
        session = client.session
        session[f"{SESSION_PREFIX}{clinic_id}"] = str(enrollment_id)
        session.save()


def _reload(setup: AppointmentSetup, pk: UUID) -> Appointment:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        return Appointment.objects.get(pk=pk)


def _transition(booked: Appointment, intent: str, **extra: str) -> dict[str, str]:
    return {
        "intent": intent,
        "view": "list",
        "day": DAY,
        "appointment_id": str(booked.pk),
        "expected_revision": str(booked.revision),
        "command_id": str(uuid4()),
    } | extra


def _hold(practitioner: UUID, start: str = "10:30", **extra: str) -> dict[str, str]:
    return {
        "intent": "hold",
        "view": "day",
        "day": DAY,
        "practitioner": str(practitioner),
        "start": start,
        "duration": "30",
        "idempotency_key": str(uuid4()),
    } | extra


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def test_every_view_renders_the_authorized_board_once_without_url_selectors(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist, setup, booked = _world(rbac_graph)
    before = audit_event_types(rbac_graph, receptionist.pk)
    with runtime_role():
        pages = {
            "day": client.get(_grid(setup.clinic_id)),
            "week": client.get(_board(setup.clinic_id, "week")),
            "month": client.get(_board(setup.clinic_id, "month")),
            "list": client.get(_board(setup.clinic_id, "list")),
        }
    for view, page in pages.items():
        assert page.status_code == 200, view
        assert str(booked.pk).encode() not in page.request["PATH_INFO"].encode()
        assert b"2000-01-02" not in page.content, view
        assert f'data-view="{view}"'.encode() in page.content, view
    day = pages["day"].content
    assert ROOM.encode() in day
    assert EQUIPMENT.encode() in day
    assert f'data-appointment="{booked.pk}"'.encode() in day
    assert b"data-book" in day
    assert gettext("Busy (other unit)").encode() not in day
    assert f'data-appointment="{booked.pk}"'.encode() in pages["week"].content
    assert b'href="/scheduling/clinics/' in pages["month"].content
    assert f"/agenda/grid/{DAY}/".encode() in pages["month"].content
    assert gettext("Mark arrived").encode() in pages["list"].content
    assert b"Synthetic Booking Persona" in pages["list"].content
    after = audit_event_types(rbac_graph, receptionist.pk)
    assert after[len(before) :].count(AGENDA_VIEWED_EVENT) == len(pages)


def test_owner_reads_the_board_without_booking_or_lifecycle_hooks(
    rbac_graph: RbacGraph,
) -> None:
    _, _, setup, booked = _world(rbac_graph)
    owner = create_role_actor(rbac_graph, UserClinicRole.Role.OWNER)
    client = _verified_client(owner)
    with runtime_role():
        page = client.get(_board(setup.clinic_id, "list"))
    assert page.status_code == 200
    assert b"Synthetic Booking Persona" in page.content
    assert b"data-book" not in page.content
    assert gettext("Mark arrived").encode() not in page.content
    legacy_move_selector = f'value="{booked.pk}"'.encode()
    assert legacy_move_selector in page.content


def test_a_physician_sees_only_their_own_lane(rbac_graph: RbacGraph) -> None:
    _, _, setup, booked = _world(rbac_graph)
    client = verified_physician_client(rbac_graph)
    with runtime_role(), tenant_context(rbac_graph.physician, setup.organization_id):
        board = view_agenda_board(clinic_id=setup.clinic_id, view="day", date=DAY)
    assert [lane.lane_id for lane in board.professionals] == [rbac_graph.physician]
    assert board.own_practitioner == rbac_graph.physician
    with runtime_role():
        page = client.get(_grid(setup.clinic_id))
    assert page.status_code == 200
    assert f'data-appointment="{booked.pk}"'.encode() not in page.content


def test_a_physician_never_receives_another_lanes_appointment(
    rbac_graph: RbacGraph,
) -> None:
    _, _, setup, _ = _world(rbac_graph)
    colleague = create_role_actor(rbac_graph, UserClinicRole.Role.PHYSICIAN)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=colleague.pk,
            start_local=f"{DAY}T11:00",
            end_local=f"{DAY}T12:00",
            idempotency_key=uuid4(),
        )
        foreign = create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=colleague.pk,
            local_range=AppointmentLocalRange(f"{DAY}T11:00", f"{DAY}T12:00"),
            idempotency_key=uuid4(),
        )
    client = verified_physician_client(rbac_graph)
    with runtime_role():
        pages = {
            "day": client.get(_grid(setup.clinic_id)),
            "week": client.get(_board(setup.clinic_id, "week")),
            "list": client.get(_board(setup.clinic_id, "list")),
        }
    for view, page in pages.items():
        assert page.status_code == 200, view
        assert str(foreign.pk).encode() not in page.content, view


# --------------------------------------------------------------------------
# Filters, saved views and unit
# --------------------------------------------------------------------------


def test_filters_are_kept_server_side_and_a_hidden_booking_still_occupies(
    rbac_graph: RbacGraph,
) -> None:
    client, _, setup, booked = _world(rbac_graph)
    with runtime_role():
        applied = client.post(
            _grid(setup.clinic_id), {"intent": "filter", "status": "cancelled"}
        )
        filtered = client.get(_grid(setup.clinic_id))
        stored = filters_from_session(client.session.get(session_key(setup.clinic_id)))
    assert applied.status_code == 303
    assert applied["Location"] == _grid(setup.clinic_id)
    assert stored == AgendaFilters(status="cancelled")
    assert f'data-appointment="{booked.pk}"'.encode() not in filtered.content
    assert gettext("Busy").encode() in filtered.content
    assert b'<option value="cancelled" selected>' in filtered.content
    with runtime_role():
        cleared = client.post(_grid(setup.clinic_id), {"intent": "clear"})
        again = client.get(_grid(setup.clinic_id))
    assert cleared.status_code == 303
    assert f'data-appointment="{booked.pk}"'.encode() in again.content


def test_saved_views_reopen_their_filters_and_archive(rbac_graph: RbacGraph) -> None:
    client, _, setup, _ = _world(rbac_graph)
    professional = str(setup.practitioner_id)
    with runtime_role():
        client.post(
            _board(setup.clinic_id, "week"),
            {"intent": "filter", "professional": professional, "status": "in_progress"},
        )
        saved = client.post(_board(setup.clinic_id, "week"), {"intent": "save"})
        client.post(_board(setup.clinic_id, "week"), {"intent": "clear"})
        page = client.get(_board(setup.clinic_id, "week"))
    assert saved.status_code == 303
    [view_id] = _values(page.content, "saved_view")
    assert gettext("Week").encode() in page.content
    with runtime_role():
        opened = client.post(
            _board(setup.clinic_id, "day"), {"intent": "open", "saved_view": view_id}
        )
        stored = filters_from_session(client.session.get(session_key(setup.clinic_id)))
        archived = client.post(
            _grid(setup.clinic_id), {"intent": "archive", "saved_view": view_id}
        )
        gone = client.post(
            _grid(setup.clinic_id), {"intent": "open", "saved_view": view_id}
        )
    assert opened.status_code == 303
    assert opened["Location"] == _board(setup.clinic_id, "week")
    assert stored == AgendaFilters(
        professional_id=setup.practitioner_id, status="in_progress"
    )
    assert archived.status_code == 303
    assert gone.status_code == 404


def test_the_unit_filter_opens_the_same_view_of_another_clinic(
    rbac_graph: RbacGraph,
) -> None:
    client, _, setup, _ = _world(rbac_graph)
    with runtime_role():
        moved = client.post(
            _board(setup.clinic_id, "month"),
            {"intent": "unit", "unit": str(rbac_graph.clinic_b)},
        )
    assert moved.status_code == 303
    assert moved["Location"] == _board(rbac_graph.clinic_b, "month")


def test_other_unit_capacity_is_busy_without_any_patient_detail(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist, setup, _ = _world(rbac_graph)
    with transaction.atomic(), owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with runtime_role(), tenant_context(rbac_graph.shared_user, setup.organization_id):
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            patient_id=setup.patient_id,
            idempotency_key=uuid4(),
            create_fingerprint=b"u" * 32,
        )
        create_availability(
            clinic_id=rbac_graph.clinic_b,
            practitioner_id=rbac_graph.physician,
            start_local=f"{DAY}T14:00",
            end_local=f"{DAY}T16:00",
            idempotency_key=uuid4(),
        )
        create_appointment(
            clinic_id=rbac_graph.clinic_b,
            enrollment_id=enrollment.pk,
            practitioner_id=rbac_graph.physician,
            local_range=AppointmentLocalRange(f"{DAY}T14:00", f"{DAY}T15:00"),
            idempotency_key=uuid4(),
        )
    with runtime_role():
        page = client.get(_grid(setup.clinic_id))
    assert page.status_code == 200
    assert gettext("Busy (other unit)").encode() in page.content
    only_the_local_booking_is_named = 1
    assert (
        page.content.count(b"Synthetic Booking Persona")
        == only_the_local_booking_is_named
    )
    with runtime_role(), tenant_context(receptionist.pk, setup.organization_id):
        board = view_agenda_board(clinic_id=setup.clinic_id, view="day", date=DAY)
    other = [busy for busy in board.occupancy if busy.other_unit]
    assert [(busy.lane_id, busy.start_local) for busy in other] == [
        (rbac_graph.physician, f"{DAY}T14:00")
    ]
    assert CROSS_ORGANIZATION_BUSY is False


# --------------------------------------------------------------------------
# Actions: hold -> confirm, lifecycle, conflicts and the slot finder
# --------------------------------------------------------------------------


def test_a_hold_for_the_patient_in_context_is_confirmed_from_the_board(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist, setup, _ = _world(rbac_graph)
    _pin(client, setup.clinic_id, setup.enrollment_id)
    with runtime_role():
        held = client.post(
            _action(setup.clinic_id),
            _hold(setup.practitioner_id),
            HTTP_HX_REQUEST="true",
        )
    assert held.status_code == 200
    assert b"<html" not in held.content
    with runtime_role(), tenant_context(receptionist.pk, setup.organization_id):
        hold = Appointment.objects.get(
            clinic_id=setup.clinic_id, status=Appointment.Status.HELD
        )
    assert hold.hold_expires_at is not None
    assert gettext("Confirm booking").encode() in held.content
    with runtime_role():
        confirmed = client.post(
            _action(setup.clinic_id),
            _transition(hold, "confirm", view="day"),
            HTTP_HX_REQUEST="true",
        )
    assert confirmed.status_code == 200
    assert str(OUTCOMES["confirm"]).encode() in confirmed.content
    assert _reload(setup, hold.pk).status == Appointment.Status.SCHEDULED
    events = audit_event_types(rbac_graph, receptionist.pk)
    assert "scheduling.appointment.held" in events
    assert "scheduling.appointment.booked" in events


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


def _racer(
    client: Client,
    url: str,
    data: dict[str, str | list[str]],
    gate: Barrier,
    outcomes: Queue[int | bytes],
) -> None:
    try:
        with runtime_role():
            gate.wait(timeout=10)
            response = client.post(url, data, HTTP_HX_REQUEST="true")
        outcomes.put(response.status_code)
        outcomes.put(response.content)
    finally:
        connections.close_all()


def test_two_receptionists_racing_the_last_room_slot_book_once(
    rbac_graph: RbacGraph, resource_clock: object
) -> None:
    del resource_clock
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
    first, first_user = receptionist_client(rbac_graph)
    second, _ = receptionist_client(rbac_graph)
    rival = seed_enrollment(
        rbac_graph, setup.actor_id, setup.clinic_id, full_name="Sintetico Rival"
    )
    _pin(first, setup.clinic_id, setup.enrollment_id)
    _pin(second, setup.clinic_id, rival)
    gate = Barrier(2)
    outcomes: Queue[int | bytes] = Queue()

    def payload() -> dict[str, str | list[str]]:
        fields: dict[str, str | list[str]] = {
            **_hold(setup.practitioner_id, start="10:00", service=str(service.pk))
        }
        fields["resources"] = [str(room.pk), str(equipment.pk)]
        return fields

    threads = [
        Thread(
            target=_racer,
            args=(client, _action(setup.clinic_id), payload(), gate, outcomes),
        )
        for client in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    results = [(outcomes.get_nowait(), outcomes.get_nowait()) for _ in threads]
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409], statuses
    [(_, loser)] = [item for item in results if item[0] == 409]
    assert isinstance(loser, bytes)
    assert str(TAKEN_MESSAGE).encode() in loser
    with runtime_role(), tenant_context(first_user.pk, setup.organization_id):
        holds = list(
            Appointment.objects.filter(
                clinic_id=setup.clinic_id, status=Appointment.Status.HELD
            )
        )
    assert len(holds) == 1


def test_arrival_and_cancellation_post_body_selectors_from_the_list(
    rbac_graph: RbacGraph,
) -> None:
    client, _, setup, booked = _world(rbac_graph)
    with runtime_role():
        arrived = client.post(_action(setup.clinic_id), _transition(booked, "arrive"))
    assert arrived.status_code == 303
    assert arrived["Location"] == _board(setup.clinic_id, "list")
    current = _reload(setup, booked.pk)
    assert current.status == Appointment.Status.ARRIVED
    with runtime_role():
        cancelled = client.post(
            _action(setup.clinic_id), _transition(current, "cancel")
        )
    assert cancelled.status_code == 303
    assert _reload(setup, booked.pk).status == Appointment.Status.CANCELLED


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


def test_the_slot_finder_lists_free_starts_for_one_movable_booking(
    rbac_graph: RbacGraph,
) -> None:
    client, _, setup, booked = _world(rbac_graph)
    find = {
        "intent": "find",
        "view": "day",
        "day": DAY,
        "appointment_id": str(booked.pk),
        "expected_revision": str(booked.revision),
        "duration": "60",
    }
    with runtime_role():
        options = client.post(_action(setup.clinic_id), find, HTTP_HX_REQUEST="true")
        native = client.post(_action(setup.clinic_id), find)
        unknown = client.post(
            _action(setup.clinic_id),
            find | {"appointment_id": str(uuid4())},
            HTTP_HX_REQUEST="true",
        )
    assert options.status_code == 200
    starts = re.findall(rb'<option value="(\d\d:\d\d)"', options.content)
    assert starts == [
        b"08:00",
        b"08:30",
        b"09:00",
        b"09:30",
        b"10:00",
        b"10:30",
        b"11:00",
    ]
    assert native.status_code == 200
    assert b"data-native-move-form" in native.content
    assert unknown.status_code == 404


# --------------------------------------------------------------------------
# Refusals: every exit is the unknown-clinic 404 with no side effect
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Typed UI API: POST agenda/board/ (body-only scope, closed filters)
# --------------------------------------------------------------------------


BOARD_API: Final = "/api/ui/v1/agenda/board/"


def test_the_board_api_answers_slots_and_appointments_without_extra_phi(
    rbac_graph: RbacGraph,
) -> None:
    _, receptionist, setup, booked = _world(rbac_graph)
    with runtime_role():
        api = _signed_in(receptionist.username, OTP_RAW_CREDENTIAL)
        answer = api.post(
            json.dumps({"clinic_id": str(setup.clinic_id), "view": "day", "date": DAY}),
            path=BOARD_API,
        )
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
    assert answer.status_code == 200
    body = json.loads(answer.content)
    assert set(body) == {
        "view",
        "anchor",
        "first_day",
        "last_day",
        "lanes",
        "slots",
        "appointments",
        "occupancy",
        "counts",
        "can_move",
        "can_book",
        "truncated",
    }
    [item] = body["appointments"]
    assert item["appointment_id"] == str(booked.pk)
    assert "birth_date" not in item
    assert b"2000-01-02" not in answer.content
    assert {lane["label"] for lane in body["lanes"]} >= {ROOM, EQUIPMENT}
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
