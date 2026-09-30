"""Every scheduling write decides the scope of its own response first.

The tenant middleware commits any response below 500. A write view that
refuses the page it re-renders or redirects to only after the write keeps the
write and still answers with a refusal (worker-contract amendment 2026-09-28;
todo 23 gate B1). Todo 21 lets RP permission holders book and move service
bookings, and some of them (scheduler, clinic_manager, a physician on their own
booking) cannot see the transition page or every agenda row. So each write here
must be refused before anything is written when either the write or its
response is refused.

Roles come from UserClinicRole.Role and permissions from BUNDLES_V1, and the
response scopes come from the registry's MANAGER_ROLES and the agenda's
physician-own scope. Both legacy and service bookings are covered. Every
refusal is byte-identical to the unknown-record refusal, sets no cookie, and
leaves every booking row and the audit ledger unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.current_context import MANAGER_ROLES
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.tenancy.db import tenant_context
from django.urls import reverse

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from scheduling.test_resource_role_matrix import _verified_client
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog
from scheduling.write_scope_routes import write_routes

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from conftest import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

ROLES = tuple(UserClinicRole.Role.values)
PHYSICIAN = UserClinicRole.Role.PHYSICIAN
# Response scopes: the transition page is manager-only; the agenda a booking
# redirects to shows managers every row and a physician their own.
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


def _snapshot(
    database_url: str,
) -> tuple[dict[tuple[str, UUID], tuple[object, ...]], int]:
    """Every booking row and the audit ledger size, read as the test superuser.

    A separate connection sees exactly what the request committed.
    """
    with psycopg.connect(database_url) as raw:
        rows = raw.execute(
            "SELECT id, start_at, end_at, status, service_type_id, resource_ids, "
            "buffer_before, buffer_after FROM clinic_app.scheduling_appointment"
        ).fetchall()
        resources = raw.execute(
            "SELECT id, appointment_id, resource_id, unit, start_at, end_at, occupied "
            "FROM clinic_app.scheduling_appointmentresource"
        ).fetchall()
        audit = raw.execute("SELECT count(*) FROM clinic_app.audit_event").fetchone()
    assert audit is not None
    return {
        **{("appointment", row[0]): tuple(row[1:]) for row in rows},
        **{("resource", row[0]): tuple(row[1:]) for row in resources},
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
    for route in write_routes():
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
                allowed = write_allowed and role in response_scope
                name = (route.name, mode, "service" if is_service else "legacy", htmx)
                # Warm step-up and compare the unknown request's own refusal.
                unknown = _post(client, unknown_url, data, htmx=htmx)
                session_before = dict(client.session)
                before_rows, before_audit = _snapshot(superuser_database_url)
                response = _post(client, url, data, htmx=htmx)
                after_rows, after_audit = _snapshot(superuser_database_url)
                if allowed:
                    expected_status = 200 if mode == "prepare" else 204 if htmx else 303
                    assert response.status_code == expected_status, (
                        role,
                        name,
                        response.status_code,
                    )
                    assert (after_rows == before_rows) == (mode == "prepare"), (
                        role,
                        name,
                    )
                    assert after_audit > before_audit, (role, name)
                    if mode != "prepare":
                        target = response["HX-Redirect" if htmx else "Location"]
                        with runtime_role():
                            assert client.get(target).status_code == 200, (role, name)
                else:
                    assert _refused_like(response, unknown), (
                        role,
                        name,
                        response.status_code,
                    )
                    assert dict(client.session) == session_before, (role, name)
                    assert after_rows == before_rows, (role, name)
                    assert after_audit == before_audit, (role, name)
    # The physician's own service move is refused only by the response scope.
    if role == PHYSICIAN:
        assert moves_service
        assert PHYSICIAN not in TRANSITION_SCOPE
