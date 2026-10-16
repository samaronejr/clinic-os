"""Real subjects and exhaustive role decisions for the four grid delegations."""

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
from apps.scheduling import agenda_grid, agenda_grid_views, appointment_rescheduling
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    AvailabilityAccessDeniedError,
)
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
    "agenda_grid._holds",
    "agenda_grid.view_day_grid",
    "agenda_grid_views.agenda_grid_move_view",
    "appointment_rescheduling._reschedule",
)


def _seed(symbol: str, graph: RbacGraph, actor: UUID) -> Seeded:
    actor_user = User.objects.get(pk=actor)
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
                date(2035, 8, 1) + timedelta(days=Appointment.objects.count())
            ).isoformat()
            registered = create_patient(
                clinic_id=graph.clinic_a,
                full_name="Sintetico grid delegation",
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
                content=ServiceInput(name="Sintetico delegation", duration_min=30),
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

    def state() -> object:
        with owner_context(graph.organization_a), connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, start_at, end_at, status, revision, resource_ids "
                "FROM clinic_app.scheduling_appointment ORDER BY id"
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT id, appointment_id, resource_id, unit, start_at, "
                "end_at, occupied "
                "FROM clinic_app.scheduling_appointmentresource ORDER BY id"
            )
            resources = cursor.fetchall()
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event")
            return rows, resources, cursor.fetchone()

    def call() -> object:
        if symbol.endswith("._holds"):
            held = agenda_grid._holds("appointment.move", graph.clinic_a)
            assert held is None or held == actor
            return held is not None
        if symbol.endswith(".view_day_grid"):
            grid = agenda_grid.view_day_grid(clinic_id=graph.clinic_a, date=day)
            return booked.pk in {row.appointment_id for row in grid.appointments}
        if symbol.endswith("._reschedule"):
            moved = appointment_rescheduling._reschedule(
                booked.pk,
                AppointmentLocalRange(f"{day}T09:00", f"{day}T09:30"),
                clinic_id=graph.clinic_a,
                revision=booked.revision,
            )
            moved.refresh_from_db()
            assert moved.revision == booked.revision + 1
            return moved.pk == booked.pk
        request = RequestFactory().post(
            "/",
            {
                "appointment_id": str(booked.pk),
                "expected_revision": str(booked.revision),
                "day": day,
                "start": "09:00",
                "duration": "30",
            },
            HTTP_HX_REQUEST="true",
        )
        request.user = actor_user
        request.session = SessionStore()
        response = inspect.unwrap(agenda_grid_views.agenda_grid_move_view)(
            request, graph.clinic_a
        )
        assert response.status_code == 200
        return f'data-revision="{booked.revision + 1}"'.encode() in response.content

    denial = (
        Http404
        if symbol.endswith(".agenda_grid_move_view")
        else AvailabilityAccessDeniedError
        if symbol.endswith(".view_day_grid")
        else AppointmentAccessDeniedError
    )
    return Seeded(call, state, returns=lambda _actor: True, denial=denial)


def _decisions(role: str) -> tuple[bool, bool, list[str], list[str]]:
    bundle = BUNDLES_V1[role]
    legacy = role in {*MANAGER_ROLES, UserClinicRole.Role.PHYSICIAN}
    readable = legacy or "appointment.book" in bundle
    moving = "appointment.move" in bundle or (
        role == "physician" and "appointment.move_own" in bundle
    )
    agenda = [] if legacy else ["appointment.book"]
    transition = ["appointment.move"]
    if "appointment.move" not in bundle:
        transition.append("appointment.move_own")
    return readable, moving, agenda, transition


def _expected(symbol: str, role: str) -> tuple[bool, list[str]]:
    catalog = role in UserClinicRole.Role.values
    readable, moving, agenda, transition = (
        _decisions(role) if catalog else (False, False, [], [])
    )
    if symbol.endswith("._holds"):
        return catalog and "appointment.move" in BUNDLES_V1[role], ["appointment.move"]
    if symbol.endswith(".view_day_grid"):
        return readable, agenda + (
            ["appointment.move", "appointment.move_own"] if readable else []
        )
    if symbol.endswith("._reschedule"):
        return moving, transition * (2 if moving else 1)
    allowed = readable and moving
    expected = agenda + (transition * (2 if moving else 1) if readable else [])
    if allowed:
        # The move answers the board: scope, move hooks, then booking hooks.
        booking = ["appointment.book"]
        if "appointment.book" not in BUNDLES_V1[role]:
            booking.append("appointment.book_own")
        expected += [*agenda, "appointment.move", "appointment.move_own", *booking]
    return allowed, expected


def _exercise(graph: RbacGraph, symbol: str) -> None:
    for role in (*UserClinicRole.Role.values, "none", "inactive_owner"):
        actor = (
            graph.physician
            if role == "physician"
            else permission_actor(graph, "owner" if role == "inactive_owner" else role)[
                0
            ]
        )
        if role == "inactive_owner":
            User.objects.filter(pk=actor).update(is_active=False)
        seeded = _seed(symbol, graph, actor)
        allowed, expected = _expected(symbol, role)
        before = seeded.state()
        with checked_permissions() as asked, permission_context(graph, actor):
            if allowed or symbol.endswith("._holds"):
                assert seeded.call() is allowed, (symbol, role)
            else:
                with pytest.raises(seeded.denial):
                    seeded.call()
        if role in UserClinicRole.Role.values:
            assert asked == expected, (symbol, role, asked)
        else:
            assert set(asked) <= {
                "appointment.book",
                "appointment.book_own",
                "appointment.move",
                "appointment.move_own",
            }
        after = seeded.state()
        if not allowed or symbol.endswith("._holds"):
            assert after == before, (symbol, role)
        else:
            assert after != before, (symbol, role)


CASES = {
    PREFIX + target: Case(
        partial(_seed, PREFIX + target),
        database_role="clinic_app",
        permission="appointment.book"
        if target.endswith(".view_day_grid")
        else "appointment.move",
        exercise=_exercise,
        permission_role="receptionist",
    )
    for target in TARGETS
}
