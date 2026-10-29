"""Every scheduling write decides the scope of its own response first.

The tenant middleware commits any response below 500. A write view that
refuses the page it re-renders or redirects to only after the write keeps the
write and still answers with a refusal (worker-contract amendment 2026-09-28;
todo 23 gate B1). Todo 21 lets RP permission holders book and move service
bookings. Their response scopes must support that authority: scheduler and
clinic_manager can reach the agenda and a service transition, and a physician
can reach their own service transition. Legacy bookings retain manager-only
transition pages and the existing manager-all/physician-own agenda. Every
refusal still precedes writes and viewed audit.

Roles come from UserClinicRole.Role and permissions from BUNDLES_V1, and the
legacy response scopes come from MANAGER_ROLES and physician-own visibility.
Service response scopes follow the RP book/move authority. Both are covered. Every
refusal is byte-identical to the unknown-record refusal, sets no cookie, and
leaves every booking row and the audit ledger unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    require_permission,
)
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.scheduling.models import QueueEntry
from apps.scheduling.reception import (
    ReceptionAccessDeniedError,
    issue_display_device,
    mark_arrived,
)
from apps.scheduling.resource_services import ResourceInput, create_resource
from apps.tenancy.db import tenant_context
from django.db import connection
from django.urls import reverse
from psycopg import sql

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    arrive_appointment,
    create_synthetic_appointment,
    seed_appointment_setup,
)
from scheduling.test_resource_role_matrix import _verified_client
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog
from scheduling.write_scope_routes import write_routes

if TYPE_CHECKING:
    from apps.scheduling.models import Appointment, Resource
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

ROLES = tuple(UserClinicRole.Role.values)
PHYSICIAN = UserClinicRole.Role.PHYSICIAN
# Legacy response scopes. Service presentation follows the authorized writer.
TRANSITION_SCOPE = frozenset(MANAGER_ROLES)
AGENDA_SCOPE = frozenset({*MANAGER_ROLES, PHYSICIAN})


def holds(role: str, *permissions: str) -> bool:
    return any(permission in BUNDLES_V1[role] for permission in permissions)


def test_the_matrix_splits_every_write_and_response_scope() -> None:
    # Each refusal cause is exercised: permission, response scope, or both.
    move_without_page = {r for r in ROLES if holds(r, "appointment.move")}
    assert move_without_page - TRANSITION_SCOPE, "a mover outside the page scope"
    assert move_without_page & TRANSITION_SCOPE, "a mover inside the page scope"
    book_without_agenda = {r for r in ROLES if holds(r, "appointment.book")}
    assert book_without_agenda - AGENDA_SCOPE, "a booker outside the agenda scope"
    assert set(ROLES) - move_without_page - TRANSITION_SCOPE, "refused both ways"
    assert holds(PHYSICIAN, "appointment.move_own", "appointment.book_own")
    assert "scheduling:agenda-grid-move" in {route.name for route in write_routes()}
    assert {route.family for route in write_routes()} == {"appointment", "reception"}


def _snapshot(
    database_url: str,
) -> tuple[dict[tuple[str, UUID], tuple[object, ...]], int]:
    """Every booking row and the audit ledger size, read as the test superuser.

    A separate connection sees exactly what the request committed.
    """
    with psycopg.connect(database_url) as raw:
        rows = raw.execute(
            "SELECT id, start_at, end_at, status, service_type_id, resource_ids, "
            "buffer_before, buffer_after, revision "
            "FROM clinic_app.scheduling_appointment"
        ).fetchall()
        resources = raw.execute(
            "SELECT id, appointment_id, resource_id, unit, start_at, end_at, occupied "
            "FROM clinic_app.scheduling_appointmentresource"
        ).fetchall()
        reception_rows: dict[tuple[str, UUID], tuple[object, ...]] = {}
        for table in (
            "scheduling_queueentry",
            "scheduling_roomstatus",
            "scheduling_cliniciandelay",
            "scheduling_displaydevice",
        ):
            records = raw.execute(
                sql.SQL(
                    "SELECT id, to_jsonb(reception_row)-'token_hash' "
                    "FROM clinic_app.{} AS reception_row"
                ).format(sql.Identifier(table))
            ).fetchall()
            reception_rows.update({(table, row[0]): (row[1],) for row in records})
        audit = raw.execute("SELECT count(*) FROM clinic_app.audit_event").fetchone()
    assert audit is not None
    return {
        **{("appointment", row[0]): tuple(row[1:]) for row in rows},
        **{("resource", row[0]): tuple(row[1:]) for row in resources},
        **reception_rows,
    }, int(audit[0])


def _post(
    client: Client, url: str, data: dict[str, str | list[str]], *, htmx: bool = False
) -> _MonkeyPatchedWSGIResponse:
    with runtime_role():
        return client.post(url, data, headers={"HX-Request": "true"} if htmx else {})


def _refused_like(
    response: _MonkeyPatchedWSGIResponse, unknown: _MonkeyPatchedWSGIResponse
) -> bool:
    return (
        response.status_code == unknown.status_code == 404
        and response.content == unknown.content
        and not response.cookies
        and not response.wsgi_request.session.modified
    )


def _grid_move_data(
    setup: AppointmentSetup, appointment: Appointment
) -> dict[str, str | list[str]]:
    """Use the live revision and free times inside the buffered fixture window."""
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment.refresh_from_db(fields=["revision"])
    service = appointment.service_type_id is not None
    return {
        "appointment_id": str(appointment.pk),
        "expected_revision": str(appointment.revision),
        "day": "2035-06-02",
        "start": "09:10" if service else "08:30",
        "duration": "30" if service else "15",
    }


def _accepted_response(
    client: Client,
    response: _MonkeyPatchedWSGIResponse,
    mode: str,
    *,
    htmx: bool,
) -> None:
    """An accepted write returns its authorized partial or a viewable redirect."""
    expected_status = (
        200
        if mode == "prepare" or (mode == "grid_move" and htmx)
        else 204
        if htmx
        else 303
    )
    assert response.status_code == expected_status, (mode, response.status_code)
    if mode == "grid_move" and htmx:
        appointment = response.wsgi_request.POST["appointment_id"]
        assert f'data-appointment="{appointment}"'.encode() in response.content
    elif mode != "prepare":
        target = response["HX-Redirect" if htmx else "Location"]
        with runtime_role():
            assert client.get(target).status_code == 200, mode


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("htmx", [False, True], ids=["native", "htmx"])
def test_writes_decide_their_response_scope_before_writing(
    rbac_graph: RbacGraph, role: str, superuser_database_url: str, *, htmx: bool
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
        legacy = create_synthetic_appointment(
            setup, start_local="2035-06-02T08:00", end_local="2035-06-02T08:30"
        )
        serviced = book(setup, (room, equipment, service), start="09:00")
    # The physician role acts as the booked practitioner (own scope applies).
    if role == PHYSICIAN:
        user = User.objects.get(pk=rbac_graph.physician)
    else:
        user = create_role_actor(rbac_graph, UserClinicRole.Role(role))
    own = user.pk == setup.practitioner_id
    client = _verified_client(user)
    moves_service = holds(role, "appointment.move") or (
        own and holds(role, "appointment.move_own")
    )
    books_service = holds(role, "appointment.book") or (
        own and holds(role, "appointment.book_own")
    )
    for route in (row for row in write_routes() if row.family == "appointment"):
        for is_service, appointment in ((False, legacy), (True, serviced)):
            booking_allowed = books_service if is_service else role in MANAGER_ROLES
            moving_allowed = moves_service if is_service else role in MANAGER_ROLES
            selection: dict[str, str | list[str]] = {
                "enrollment_id": str(setup.enrollment_id),
                "practitioner": str(setup.practitioner_id),
            }
            if is_service:
                selection.update(
                    service_type_id=str(service.pk),
                    resource_ids=[str(room.pk), str(equipment.pk)],
                )
            create_data = {
                **selection,
                "start_local": "2035-06-02T10:30" if is_service else "2035-06-02T10:00",
                "end_local": "2035-06-02T11:00" if is_service else "2035-06-02T10:15",
                "idempotency_key": str(uuid4()),
            }
            cases: dict[str, tuple[dict[str, str | list[str]], bool]] = {
                "prepare": ({**selection, "mode": "prepare"}, booking_allowed),
                "create": ({**create_data, "mode": "create"}, booking_allowed),
                "reschedule": (
                    {
                        "start_local": (
                            "2035-06-02T11:20" if is_service else "2035-06-02T08:00"
                        ),
                        "end_local": (
                            "2035-06-02T11:50" if is_service else "2035-06-02T08:15"
                        ),
                    },
                    moving_allowed,
                ),
                "cancel": ({"reason": "clinic_request"}, moving_allowed),
                "grid_move": (_grid_move_data(setup, appointment), moving_allowed),
            }
            identifier = (
                setup.clinic_id if route.selector == "clinic_id" else appointment.pk
            )
            url = reverse(route.name, kwargs={route.selector: identifier})
            unknown_url = reverse(route.name, kwargs={route.selector: uuid4()})
            for mode in route.modes:
                assert mode in cases, ("unclassified scheduling mode", route.name, mode)
                data, write_allowed = cases[mode]
                response_scope = (
                    AGENDA_SCOPE if mode in {"prepare", "create"} else TRANSITION_SCOPE
                )
                if mode == "grid_move":
                    allowed = write_allowed and (
                        role in AGENDA_SCOPE or holds(role, "appointment.book")
                    )
                else:
                    allowed = write_allowed and (is_service or role in response_scope)
                name = (route.name, mode, "service" if is_service else "legacy", htmx)
                # Warm step-up and compare the unknown request's own refusal.
                unknown = _post(client, unknown_url, data, htmx=htmx)
                session_before = dict(client.session)
                before_rows, before_audit = _snapshot(superuser_database_url)
                response = _post(client, url, data, htmx=htmx)
                after_rows, after_audit = _snapshot(superuser_database_url)
                if allowed:
                    _accepted_response(client, response, mode, htmx=htmx)
                    assert (after_rows == before_rows) == (mode == "prepare"), (
                        role,
                        name,
                    )
                    assert after_audit > before_audit, (role, name)
                    if mode == "grid_move":
                        assert (
                            after_rows[("appointment", appointment.pk)][-1]
                            == appointment.revision + 1
                        ), (role, name)
                else:
                    assert _refused_like(response, unknown), (
                        role,
                        name,
                        response.status_code,
                    )
                    assert dict(client.session) == session_before, (role, name)
                    assert after_rows == before_rows, (role, name)
                    assert after_audit == before_audit, (role, name)
    # Own service moves succeed; legacy physician transition pages remain denied.
    if role == PHYSICIAN:
        assert moves_service
        assert PHYSICIAN not in TRANSITION_SCOPE


def _assert_nurse_queue_scope(
    setup: AppointmentSetup,
    legacy: Appointment,
    arrived: Appointment,
    location: Resource,
) -> None:
    with pytest.raises(CurrentActorError):
        require_permission("appointment.move", clinic_id=setup.clinic_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.reception_readiness(%s,%s)",
            [setup.clinic_id, legacy.pk],
        )
        assert cursor.fetchone() is None
    with pytest.raises(ReceptionAccessDeniedError):
        mark_arrived(
            clinic_id=setup.clinic_id,
            appointment_id=legacy.pk,
            location_id=location.pk,
            expected_revision=legacy.revision,
            command_id=uuid4(),
        )
    before_revision = arrived.revision
    inserted = QueueEntry.objects.create(
        organization_id=setup.organization_id,
        clinic_id=setup.clinic_id,
        appointment_id=arrived.pk,
        location_id=location.pk,
    )
    assert QueueEntry.objects.filter(pk=inserted.pk).exists()
    arrived.refresh_from_db()
    assert (arrived.status, arrived.revision) == ("arrived", before_revision)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("htmx", [False, True], ids=["native", "htmx"])
def test_reception_writes_decide_their_response_scope_before_writing(
    rbac_graph: RbacGraph, role: str, superuser_database_url: str, *, htmx: bool
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
        location = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="Response scope location", kind="location"),
        )
        legacy = create_synthetic_appointment(
            setup, start_local="2035-06-02T08:00", end_local="2035-06-02T08:30"
        )
        serviced = book(setup, (room, equipment, service), start="09:00")
        waiting = create_synthetic_appointment(
            setup, start_local="2035-06-02T11:00", end_local="2035-06-02T11:15"
        )
        queued = mark_arrived(
            clinic_id=setup.clinic_id,
            appointment_id=waiting.pk,
            location_id=location.pk,
            expected_revision=waiting.revision,
            command_id=uuid4(),
        )
        device = issue_display_device(clinic_id=setup.clinic_id)
        arrived = arrive_appointment(
            create_synthetic_appointment(
                setup, start_local="2035-06-02T10:00", end_local="2035-06-02T10:15"
            )
        )
    user = (
        User.objects.get(pk=rbac_graph.physician)
        if role == PHYSICIAN
        else create_role_actor(rbac_graph, UserClinicRole.Role(role))
    )
    client = _verified_client(user)
    if role == UserClinicRole.Role.NURSE:
        with runtime_role(), tenant_context(user.pk, setup.organization_id):
            _assert_nurse_queue_scope(setup, legacy, arrived, location)
    cases: dict[str, dict[str, str | list[str]]] = {
        "view": {},
        "arrive": {"location_id": str(location.pk)},
        "room": {
            "room_id": str(room.pk),
            "unit": "1",
            "state": "available",
            "expected_revision": "0",
        },
        "delay": {
            "practitioner_id": str(setup.practitioner_id),
            "minutes": "5",
            "expected_revision": "0",
        },
        "issue_device": {},
        "revoke_device": {"device_id": str(device.device_id)},
        "call": {
            "queue_entry_id": str(queued.pk),
            "room_id": str(room.pk),
            "unit": "1",
            "expected_revision": str(queued.revision),
        },
    }
    routes = [route for route in write_routes() if route.family == "reception"]
    assert routes, "no reception write aliases discovered"
    for route in routes:
        assert set(route.modes) == set(cases), (route.name, route.modes)
        url = reverse(route.name, kwargs={route.selector: setup.clinic_id})
        unknown_url = reverse(route.name, kwargs={route.selector: uuid4()})
        for intent, fields in cases.items():
            allowed = role == "receptionist" or (
                role == "nurse" and intent in {"view", "room", "call"}
            )
            for appointment in (legacy, serviced) if intent == "arrive" else (legacy,):
                data = {"intent": intent, "day": "2035-06-02", **fields}
                if intent == "arrive":
                    data.update(
                        appointment_id=str(appointment.pk),
                        expected_revision=str(appointment.revision),
                        command_id=str(uuid4()),
                    )
                unknown = _post(client, unknown_url, data, htmx=htmx)
                before_session = dict(client.session)
                before_rows, before_audit = _snapshot(superuser_database_url)
                response = _post(client, url, data, htmx=htmx)
                after_rows, after_audit = _snapshot(superuser_database_url)
                case = (
                    role,
                    route.name,
                    intent,
                    "service" if appointment.service_type_id is not None else "legacy",
                    htmx,
                )
                if allowed:
                    assert response.status_code == 200, case
                    if role == UserClinicRole.Role.NURSE:
                        screen = response.context["screen"]
                        assert screen.arrivals == ()
                        assert screen.devices == ()
                        assert screen.pending_tasks is None
                        assert queued.pk in {entry.pk for entry in screen.queue}
                        assert room.pk in {entry.room.pk for entry in screen.rooms}
                    assert (after_rows == before_rows) == (intent == "view"), case
                    assert (after_audit == before_audit) == (intent == "view"), case
                    if intent != "view":
                        assert after_audit > before_audit, case
                else:
                    assert _refused_like(response, unknown), case
                    assert dict(client.session) == before_session, case
                    assert after_rows == before_rows, case
                    assert after_audit == before_audit, case
