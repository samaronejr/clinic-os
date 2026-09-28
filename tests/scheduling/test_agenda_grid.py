"""Todo 23 agenda slice: the multi-resource day grid and its guarded move.

``move_appointment`` is ``reschedule_appointment`` plus two refusals: a stale
``expected_revision`` (no lost update) and an appointment of another clinic
(same denial as an unknown one). The grid view and its move POST are the
HTMX variant the slice selected (docs/adr/ADR-001).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from queue import Queue
from threading import Barrier, Thread
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID, uuid4

import pytest
from apps.identity.current_context import MANAGER_ROLES
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.scheduling.agenda_grid import DayGrid, GridAppointment, GridColumn
from apps.scheduling.agenda_grid_views import (
    MOVED_MESSAGE,
    REVISION_MESSAGE,
    grid_context,
)
from apps.scheduling.appointment_lifecycle import AppointmentLifecycleError
from apps.scheduling.models import Appointment
from apps.scheduling.resource_services import ResourceInput, create_resource
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentLocalRange,
    AppointmentRescheduleInputError,
    move_appointment,
    reschedule_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connections
from django.test import Client
from django.utils.translation import gettext

from auth.stepup_test_support import create_role_actor
from identity.permission_support import permission_actor, permission_context
from otp_test_support import runtime_role
from patient_http_support import audit_event_types, receptionist_client
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from scheduling.appointment_http_support import (
    AGENDA_VIEWED_EVENT,
    APPOINTMENT_RESCHEDULED_EVENT,
    BookingContext,
    seed_appointment,
    seed_enrollment,
)
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from scheduling.availability_http_support import FUTURE_DATE, LocalWindow, seed_block
from scheduling.test_resource_role_matrix import _verified_client
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)

DAY: Final = "2035-06-02"
WINDOW: Final = LocalWindow("08:00", "12:00")
NONCE: Final = re.compile(rb'(nonce|value)="[^"]*"')


def _range(start: str, end: str, day: str = DAY) -> AppointmentLocalRange:
    return AppointmentLocalRange(f"{day}T{start}", f"{day}T{end}")


def _stored(setup: AppointmentSetup, appointment_id: UUID) -> tuple[str, int]:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        row = Appointment.objects.get(pk=appointment_id)
    return row.start_at.isoformat(), row.revision


# --------------------------------------------------------------------------
# Service: move appointment with a revision
# --------------------------------------------------------------------------


def test_move_from_the_rendered_revision_moves_and_bumps_it(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = create_synthetic_appointment(setup)
        booked.refresh_from_db()
        moved = move_appointment(
            clinic_id=setup.clinic_id,
            appointment_id=booked.pk,
            expected_revision=booked.revision,
            local_range=_range("10:00", "11:00"),
        )
        moved.refresh_from_db()
    assert (moved.start_at.hour, moved.end_at.hour) == (13, 14)  # 10:00 BRT = 13Z
    assert moved.revision == booked.revision + 1


def test_a_stale_revision_is_refused_and_writes_nothing(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = create_synthetic_appointment(setup)
        booked.refresh_from_db()
        move_appointment(
            clinic_id=setup.clinic_id,
            appointment_id=booked.pk,
            expected_revision=booked.revision,
            local_range=_range("10:00", "11:00"),
        )
    after_first = _stored(setup, booked.pk)
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(AppointmentLifecycleError) as refused,
    ):
        move_appointment(
            clinic_id=setup.clinic_id,
            appointment_id=booked.pk,
            expected_revision=booked.revision,
            local_range=_range("11:00", "12:00"),
        )
    assert refused.value.code == "revision_conflict"
    assert _stored(setup, booked.pk) == after_first
    events = audit_event_types(rbac_graph, setup.actor_id)
    assert events.count(APPOINTMENT_RESCHEDULED_EVENT) == 1


def _mover(
    setup: AppointmentSetup,
    booked: Appointment,
    local_range: AppointmentLocalRange,
    gate: Barrier,
    outcomes: Queue[Appointment | Exception],
) -> None:
    try:
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            gate.wait(timeout=10)
            outcomes.put(
                move_appointment(
                    clinic_id=setup.clinic_id,
                    appointment_id=booked.pk,
                    expected_revision=booked.revision,
                    local_range=local_range,
                )
            )
    except (AppointmentLifecycleError, DatabaseError) as error:
        outcomes.put(error)
    finally:
        connections.close_all()


def test_two_concurrent_moves_from_one_revision_keep_exactly_one(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = create_synthetic_appointment(setup)
        booked.refresh_from_db()
    gate = Barrier(2)
    outcomes: Queue[Appointment | Exception] = Queue()
    threads = [
        Thread(
            target=_mover,
            args=(setup, booked, target, gate, outcomes),
        )
        for target in (_range("10:00", "11:00"), _range("11:00", "12:00"))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    results = [outcomes.get_nowait() for _ in threads]
    winners = [item for item in results if isinstance(item, Appointment)]
    losers = [item for item in results if isinstance(item, AppointmentLifecycleError)]
    assert len(winners) == 1, results
    assert [loser.code for loser in losers] == ["revision_conflict"], results
    start, revision = _stored(setup, booked.pk)
    assert start == winners[0].start_at.isoformat()
    assert revision == booked.revision + 1


def test_unknown_and_foreign_clinic_appointments_share_one_denial(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = create_synthetic_appointment(setup)
        booked.refresh_from_db()
    before = _stored(setup, booked.pk)
    for clinic_id, appointment_id in (
        (setup.clinic_id, uuid4()),
        (rbac_graph.clinic_b, booked.pk),
        (uuid4(), booked.pk),
    ):
        with (
            runtime_role(),
            tenant_context(setup.actor_id, setup.organization_id),
            pytest.raises(AppointmentAccessDeniedError) as denied,
        ):
            move_appointment(
                clinic_id=clinic_id,
                appointment_id=appointment_id,
                expected_revision=booked.revision,
                local_range=_range("10:00", "11:00"),
            )
        assert str(denied.value) == str(AppointmentAccessDeniedError())
    assert _stored(setup, booked.pk) == before


@pytest.mark.parametrize(
    ("clinic", "revision"),
    [("text", 1), (None, 0), (None, -1), (None, 2**31), (None, True), (None, "1")],
)
def test_malformed_scope_or_revision_is_refused_before_any_read(
    rbac_graph: RbacGraph, clinic: object, revision: object
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(AppointmentRescheduleInputError),
    ):
        move_appointment(
            clinic_id=cast("UUID", clinic or setup.clinic_id),
            appointment_id=uuid4(),
            expected_revision=cast("int", revision),
            local_range=_range("10:00", "11:00"),
        )


def _outcome(call: object) -> str:
    try:
        assert callable(call)
        call()
    except Exception as error:  # noqa: BLE001 - the exact type is the outcome
        return type(error).__name__
    return "moved"


@pytest.mark.parametrize("role", [*UserClinicRole.Role.values, "none"])
def test_move_authority_is_exactly_reschedule_authority_for_every_role(
    rbac_graph: RbacGraph, role: str
) -> None:
    """Differential: the grid's move refuses exactly the actors reschedule does."""
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        first = create_synthetic_appointment(setup)
        second = create_synthetic_appointment(
            setup, start_local=f"{DAY}T10:00", end_local=f"{DAY}T11:00"
        )
        first.refresh_from_db()
    actor, _ = permission_actor(rbac_graph, role)
    with permission_context(rbac_graph, actor):
        moved = _outcome(
            lambda: move_appointment(
                clinic_id=setup.clinic_id,
                appointment_id=first.pk,
                expected_revision=first.revision,
                local_range=_range("08:00", "09:00"),
            )
        )
    with permission_context(rbac_graph, actor):
        rescheduled = _outcome(
            lambda: reschedule_appointment(
                appointment_id=second.pk,
                local_range=_range("11:00", "12:00"),
            )
        )
    assert moved == rescheduled
    allowed = role in {"owner", "receptionist", "clinic_admin"}
    assert (moved == "moved") is allowed, (role, moved)


# --------------------------------------------------------------------------
# Presentation: slot x column layout
# --------------------------------------------------------------------------


def _item(
    start: str, end: str, practitioner: UUID, rooms: tuple[UUID, ...]
) -> GridAppointment:
    return GridAppointment(
        appointment_id=uuid4(),
        patient_display_name="Sintetico",
        practitioner_id=practitioner,
        room_ids=rooms,
        start_local=f"{DAY}T{start}",
        end_local=f"{DAY}T{end}",
        status="scheduled",
        revision=1,
        movable=True,
    )


def test_grid_places_each_appointment_in_its_practitioner_and_room_columns() -> None:
    practitioner, room, other = uuid4(), uuid4(), uuid4()
    first = _item("09:00", "10:00", practitioner, (room,))
    second = _item("10:15", "10:45", practitioner, ())
    grid = DayGrid(
        date=date(2035, 6, 2),
        timezone_key="America/Sao_Paulo",
        columns=(
            GridColumn("practitioner", practitioner, "Dra. Sintetica"),
            GridColumn("room", room, "Sala 1"),
            GridColumn("room", other, "Sala 2"),
        ),
        slots=("09:00", "09:30", "10:00", "10:30"),
        appointments=(first, second),
        can_move=True,
        truncated=False,
    )
    rows = cast("list[dict[str, Any]]", grid_context(uuid4(), grid)["rows"])
    kinds = [[cell["kind"] for cell in row["cells"]] for row in rows]
    assert kinds == [
        ["booked", "booked", "free"],
        ["continues", "continues", "free"],
        ["booked", "free", "free"],
        ["continues", "free", "free"],
    ]


# --------------------------------------------------------------------------
# HTTP: grid page and move POST (variant A)
# --------------------------------------------------------------------------


def _grid(clinic_id: UUID, day: str = FUTURE_DATE) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/grid/{day}/"


def _move(clinic_id: UUID) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/grid/move/"


def _seeded(graph: RbacGraph) -> tuple[Client, BookingContext, Appointment]:
    client, receptionist = receptionist_client(graph)
    context = BookingContext(graph, receptionist.pk, graph.clinic_a)
    seed_block(graph, receptionist.pk, graph.clinic_a, graph.physician, WINDOW)
    enrollment = seed_enrollment(graph, receptionist.pk, graph.clinic_a)
    booked = seed_appointment(
        context,
        enrollment,
        graph.physician,
        (f"{FUTURE_DATE}T09:00", f"{FUTURE_DATE}T09:30"),
    )
    with runtime_role(), tenant_context(receptionist.pk, graph.organization_a):
        create_resource(
            clinic_id=graph.clinic_a,
            content=ResourceInput(name="Sala Sintetica 1", kind="room"),
        )
    return client, context, _reload(context, booked.pk)


def _payload(booked: Appointment, **overrides: str) -> dict[str, str]:
    return {
        "appointment_id": str(booked.pk),
        "expected_revision": str(booked.revision),
        "day": FUTURE_DATE,
        "start": "10:00",
        "duration": "30",
    } | overrides


def _reload(context: BookingContext, pk: UUID) -> Appointment:
    with runtime_role(), tenant_context(context.actor, context.graph.organization_a):
        return Appointment.objects.get(pk=pk)


def test_grid_page_shows_practitioner_and_room_columns_with_move_hooks(
    rbac_graph: RbacGraph,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    with runtime_role():
        response = client.get(_grid(context.clinic_id))
    body = response.content
    assert response.status_code == 200
    assert b"Sala Sintetica 1" in body
    assert f'data-appointment="{booked.pk}"'.encode() in body
    assert f'data-revision="{booked.revision}"'.encode() in body
    assert b"data-move-dialog" in body
    assert gettext("Move appointment").encode() in body
    assert b"1988-04-05" not in body
    assert str(booked.pk).encode() not in response.request["PATH_INFO"].encode()
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(AGENDA_VIEWED_EVENT) == 1


def test_htmx_move_answers_the_fresh_grid_and_a_worded_success(
    rbac_graph: RbacGraph,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    with runtime_role():
        response = client.post(
            _move(context.clinic_id), _payload(booked), HTTP_HX_REQUEST="true"
        )
    assert response.status_code == 200
    assert b"<html" not in response.content
    assert str(MOVED_MESSAGE % {"start": "10:00"}).encode() in response.content
    moved = _reload(context, booked.pk)
    assert moved.revision == booked.revision + 1
    assert f'data-revision="{moved.revision}"'.encode() in response.content


def test_native_move_redirects_to_the_grid_of_that_day(
    rbac_graph: RbacGraph,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    with runtime_role():
        response = client.post(_move(context.clinic_id), _payload(booked))
    assert response.status_code == 303
    assert response["Location"] == _grid(context.clinic_id)
    assert _reload(context, booked.pk).revision == booked.revision + 1


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


def _refusal_body(response: object) -> bytes:
    content = getattr(response, "content", b"")
    assert isinstance(content, bytes)
    return NONCE.sub(b'\\1=""', content)


def test_every_refused_move_is_the_unknown_clinic_404_with_no_side_effect(
    rbac_graph: RbacGraph,
) -> None:
    client, context, booked = _seeded(rbac_graph)
    with runtime_role():
        # The first request after sign-in may rotate the session; refuse after it.
        warmup = client.get(_grid(context.clinic_id))
        baseline = client.post(_move(uuid4()), _payload(booked))
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
            name: client.post(url, _payload(booked, **overrides))
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
    assert APPOINTMENT_RESCHEDULED_EVENT not in events


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


# --------------------------------------------------------------------------
# HTTP role matrix: legacy and service bookings, every catalog role (review B1)
# --------------------------------------------------------------------------

GRID_ROLES: Final = frozenset(str(role) for role in MANAGER_ROLES)
MOVE_HOLDERS: Final = frozenset(
    role for role, bundle in BUNDLES_V1.items() if "appointment.move" in bundle
)
RESCHEDULED: Final = APPOINTMENT_RESCHEDULED_EVENT


def _matrix_world(graph: RbacGraph) -> tuple[AppointmentSetup, dict[str, Appointment]]:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        legacy = create_synthetic_appointment(setup)
        service = book(setup, _catalog(setup), start="11:00")
        legacy.refresh_from_db()
        service.refresh_from_db()
    assert service.service_type_id is not None
    assert legacy.service_type_id is None
    return setup, {"legacy": legacy, "service": service}


def _matrix_payload(booked: Appointment, start: str) -> dict[str, str]:
    minutes = int((booked.end_at - booked.start_at).total_seconds() // 60)
    return {
        "appointment_id": str(booked.pk),
        "expected_revision": str(booked.revision),
        "day": DAY,
        "start": start,
        "duration": str(minutes),
    }


def _state(
    setup: AppointmentSetup, booked: Appointment
) -> tuple[datetime, datetime, int]:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        row = Appointment.objects.get(pk=booked.pk)
    return row.start_at, row.end_at, row.revision


def _role_client(graph: RbacGraph, role: str) -> Client:
    if role == "none":
        client = Client()
        with runtime_role():
            assert client.login(
                username=graph.no_membership_username, password=RBAC_RAW_CREDENTIAL
            )
        return client
    return _verified_client(create_role_actor(graph, UserClinicRole.Role(role)))


@pytest.mark.usefixtures("resource_clock")
@pytest.mark.parametrize("role", [*UserClinicRole.Role.values, "none"])
def test_grid_move_refuses_every_role_outside_scope_before_any_write(
    rbac_graph: RbacGraph, role: str
) -> None:
    """Allowed = grid (manager) scope AND the booking's own move authority.

    Legacy bookings move under the manager roles; service bookings under
    appointment.move. Every other role gets the unknown-clinic 404 on both the
    HTMX and the native POST, with no cookie, no write and no audit row.
    """
    setup, bookings = _matrix_world(rbac_graph)
    client = _role_client(rbac_graph, role)
    targets = {"legacy": "08:00", "service": "10:30"}
    allowed = {
        "legacy": role in GRID_ROLES,
        "service": role in GRID_ROLES and role in MOVE_HOLDERS,
    }
    before_audit = audit_event_types(rbac_graph, setup.actor_id).count(RESCHEDULED)
    with runtime_role():
        # The first request after sign-in may rotate the session; refuse after it.
        client.get("/auth/protected/")
        baseline = client.post(
            _move(uuid4()), _matrix_payload(bookings["legacy"], "08:00")
        )
    # A member gets the unknown-clinic 404; an actor with no membership at all
    # is refused earlier by the tenant middleware (403), for every clinic.
    assert baseline.status_code == (403 if role == "none" else 404), role
    assert "sessionid" not in baseline.cookies, role
    moved = 0
    for kind, booked in bookings.items():
        before = _state(setup, booked)
        payload = _matrix_payload(booked, targets[kind])
        with runtime_role():
            hx = client.post(_move(setup.clinic_id), payload, HTTP_HX_REQUEST="true")
        if allowed[kind]:
            assert hx.status_code == 200, (role, kind, hx.status_code)
            assert _state(setup, booked)[2] == before[2] + 1, (role, kind)
            moved += 1
            continue
        with runtime_role():
            native = client.post(_move(setup.clinic_id), payload)
        for response in (hx, native):
            assert response.status_code == baseline.status_code, (role, kind)
            assert _refusal_body(response) == _refusal_body(baseline), (role, kind)
            assert set(response.cookies) == set(baseline.cookies), (role, kind)
        assert _state(setup, booked) == before, (role, kind)
    after_audit = audit_event_types(rbac_graph, setup.actor_id).count(RESCHEDULED)
    assert after_audit - before_audit == moved, role
    assert moved == sum(allowed.values()), role


def test_matrix_expectations_are_the_live_catalog() -> None:
    # Derived from the catalog, cross-checked against the review's finding:
    # scheduler and clinic_manager hold appointment.move without grid scope.
    assert frozenset({"scheduler", "clinic_manager"}) <= MOVE_HOLDERS - GRID_ROLES
    assert {"owner", "receptionist", "clinic_admin"} == GRID_ROLES


def test_owner_sees_no_move_hook_on_a_service_booking_it_cannot_move(
    rbac_graph: RbacGraph, resource_clock: object
) -> None:
    del resource_clock
    setup, bookings = _matrix_world(rbac_graph)
    owner = create_role_actor(rbac_graph, UserClinicRole.Role.OWNER)
    client = _verified_client(owner)
    with runtime_role():
        page = client.get(_grid(setup.clinic_id, DAY))
    assert page.status_code == 200
    assert f'data-appointment="{bookings["legacy"].pk}"'.encode() in page.content
    assert f'data-appointment="{bookings["service"].pk}"'.encode() not in page.content
