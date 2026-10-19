"""Real subjects and exhaustive role decisions for the two board delegations.

``view_agenda_board`` reads under the agenda scope and asks the move terms
(move hooks) and the booking terms (confirm and booking hooks).
``agenda_board_action_view`` authorizes the same scope, records an arrival
through ``authorize_transition``'s move terms, then renders the board.
"""

from __future__ import annotations

import inspect
from datetime import date, timedelta
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.current_context import MANAGER_ROLES
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.intake.services import create_patient
from apps.scheduling import agenda_board, agenda_board_views
from apps.scheduling.access import AvailabilityAccessDeniedError
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.models import Appointment
from apps.scheduling.resource_definitions import ServiceInput, create_service_type
from apps.scheduling.services import (
    ServiceBooking,
    create_availability,
    create_service_appointment,
)
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.http import Http404
from django.test import RequestFactory

from identity.delegation_probe_support import Case, Seeded, checked_permissions
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

PREFIX = "apps.scheduling."
TARGETS = (
    "agenda_board.view_agenda_board",
    "agenda_board_views.agenda_board_action_view",
)
ASKED = frozenset(
    {
        "appointment.book",
        "appointment.book_own",
        "appointment.move",
        "appointment.move_own",
    }
)


def _booking(graph: RbacGraph) -> tuple[str, Appointment]:
    with (
        runtime_role(),
        owner_context(graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT current_setting('app.current_user_id', true)")
        (previous,) = cursor.fetchone()
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [str(graph.shared_user)],
        )
        try:
            day = (
                date(2036, 3, 2) + timedelta(days=Appointment.objects.count())
            ).isoformat()
            registered = create_patient(
                clinic_id=graph.clinic_a,
                full_name="Sintetico board delegation",
                birth_date=date(1990, 1, 1),
                idempotency_key=uuid4(),
            )
            create_availability(
                clinic_id=graph.clinic_a,
                practitioner_id=graph.physician,
                start_local=f"{day}T08:00",
                end_local=f"{day}T12:00",
                idempotency_key=uuid4(),
            )
            service = create_service_type(
                clinic_id=graph.clinic_a,
                content=ServiceInput(name="Sintetico board", duration_min=30),
            )
            booked = create_service_appointment(
                clinic_id=graph.clinic_a,
                enrollment_id=registered.enrollment.pk,
                practitioner_id=graph.physician,
                booking=ServiceBooking(
                    AppointmentLocalRange(f"{day}T08:00", f"{day}T08:30"),
                    service.pk,
                    (),
                ),
                idempotency_key=uuid4(),
            )
        finally:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [previous or ""]
            )
    return day, booked


def _appointments(graph: RbacGraph) -> list[tuple[object, ...]]:
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, start_at, end_at, status, revision "
            "FROM clinic_app.scheduling_appointment ORDER BY id"
        )
        return list(cursor.fetchall())


def _seed(symbol: str, graph: RbacGraph, actor: UUID) -> Seeded:
    actor_user = User.objects.get(pk=actor)
    day, booked = _booking(graph)

    def state() -> object:
        rows = _appointments(graph)
        with owner_context(graph.organization_a), connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event")
            return rows, cursor.fetchone()

    def call() -> object:
        if symbol.endswith(".view_agenda_board"):
            board = agenda_board.view_agenda_board(
                clinic_id=graph.clinic_a, view="day", date=day
            )
            return booked.pk in {row.appointment_id for row in board.appointments}
        request = RequestFactory().post(
            "/",
            {
                "intent": "arrive",
                "view": "day",
                "day": day,
                "appointment_id": str(booked.pk),
                "expected_revision": str(booked.revision),
                "command_id": str(uuid4()),
            },
            HTTP_HX_REQUEST="true",
        )
        request.user = actor_user
        request.session = SessionStore()
        response = inspect.unwrap(agenda_board_views.agenda_board_action_view)(
            request, graph.clinic_a
        )
        assert response.status_code == 200
        return f'data-revision="{booked.revision + 1}"'.encode() in response.content

    denial = (
        AvailabilityAccessDeniedError
        if symbol.endswith(".view_agenda_board")
        else Http404
    )
    return Seeded(call, state, returns=lambda _actor: True, denial=denial)


def _expected(symbol: str, role: str) -> tuple[bool, list[str]]:
    bundle = BUNDLES_V1[role]
    legacy = role in {*MANAGER_ROLES, UserClinicRole.Role.PHYSICIAN}
    readable = legacy or "appointment.book" in bundle
    agenda = [] if legacy else ["appointment.book"]
    booking = ["appointment.book"]
    if "appointment.book" not in bundle:
        booking.append("appointment.book_own")
    board = [*agenda, "appointment.move", "appointment.move_own", *booking]
    if symbol.endswith(".view_agenda_board"):
        return readable, board if readable else agenda
    transition = ["appointment.move"]
    if "appointment.move" not in bundle:
        transition.append("appointment.move_own")
    moving = "appointment.move" in bundle or (
        role == UserClinicRole.Role.PHYSICIAN and "appointment.move_own" in bundle
    )
    if not readable:
        return False, agenda
    if not moving:
        return False, agenda + transition
    return True, agenda + transition + board


def _exercise(graph: RbacGraph, symbol: str) -> None:
    for role in (*UserClinicRole.Role.values, "none", "inactive_owner"):
        actor = (
            graph.physician
            if role == UserClinicRole.Role.PHYSICIAN
            else permission_actor(graph, "owner" if role == "inactive_owner" else role)[
                0
            ]
        )
        if role == "inactive_owner":
            User.objects.filter(pk=actor).update(is_active=False)
        seeded = _seed(symbol, graph, actor)
        catalog = role in UserClinicRole.Role.values
        allowed, expected = _expected(symbol, role) if catalog else (False, [])
        before = _appointments(graph)
        with checked_permissions() as asked, permission_context(graph, actor):
            if allowed:
                assert seeded.call() is True, (symbol, role)
            else:
                with pytest.raises(seeded.denial):
                    seeded.call()
        if catalog:
            assert asked == expected, (symbol, role, asked)
        else:
            assert set(asked) <= ASKED
        changed = _appointments(graph) != before
        assert changed is (allowed and symbol.endswith(".agenda_board_action_view")), (
            symbol,
            role,
        )


CASES = {
    PREFIX + target: Case(
        partial(_seed, PREFIX + target),
        database_role="clinic_app",
        permission="appointment.book"
        if target.endswith(".view_agenda_board")
        else "appointment.move",
        exercise=_exercise,
        permission_role="receptionist",
    )
    for target in TARGETS
}
