"""Gate r1 (SC-8): agenda lifecycle controls equal the services' own decisions.

Each case renders the real agenda for one actor and compares its arrival,
start and completion buttons with what the real transition services accept
from that actor on the same rows (each attempt rolled back).
"""

from __future__ import annotations

from html.parser import HTMLParser
from itertools import combinations
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.identity.scope_provisioning import narrow_role
from apps.scheduling import services
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.test import Client

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from otp_test_support import create_totp_device
from patient_service_support import runtime_role
from scheduling.appointment_http_support import agenda_at_url
from scheduling.appointment_service_support import (
    arrive_appointment,
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.scheduling.models import Appointment

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

DAY: Final = "2035-06-02"
MANAGEMENT: Final = (
    "owner",
    "clinic_admin",
    "receptionist",
    "scheduler",
    "clinic_manager",
)
ROLE_UNIONS: Final = tuple(
    group
    for size in range(len(MANAGEMENT) + 1)
    for group in combinations(MANAGEMENT, size)
)
SERVICES: Final[dict[str, Callable[..., Appointment]]] = {
    "arrive": services.arrive,
    "start": services.start,
    "complete": services.complete,
}
OWN_STEPS: Final = {
    "scheduled": {"arrive"},
    "arrived": {"start"},
    "in_progress": {"complete"},
}


class LifecycleButtons(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.status: str | None = None
        self.actions: dict[str, set[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self.status = attributes.get("data-appointment-status")
        value = attributes.get("value")
        if (
            tag == "button"
            and self.status is not None
            and attributes.get("name") == "action"
            and value in SERVICES
        ):
            self.actions.setdefault(self.status, set()).add(str(value))


@pytest.fixture
def lifecycle_rows(rbac_graph: RbacGraph) -> dict[str, Appointment]:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        rows = {
            status: create_synthetic_appointment(
                setup, start_local=f"{DAY}T{start}", end_local=f"{DAY}T{end}"
            )
            for status, start, end in (
                ("scheduled", "09:00", "09:30"),
                ("arrived", "09:30", "10:00"),
                ("in_progress", "10:00", "10:30"),
            )
        }
        rows["arrived"] = arrive_appointment(rows["arrived"])
        rows["in_progress"] = arrive_appointment(rows["in_progress"])
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        rows["in_progress"] = services.start(
            clinic_id=setup.clinic_id,
            appointment_id=rows["in_progress"].pk,
            expected_revision=rows["in_progress"].revision,
            command_id=uuid4(),
        )
    assert {status: row.status for status, row in rows.items()} == {
        status: status for status in rows
    }
    return rows


def _add_roles(graph: RbacGraph, user_id: UUID, roles: tuple[str, ...]) -> None:
    with owner_context(graph.organization_a):
        for role in roles:
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=user_id,
                role=role,
            )


def _rendered(graph: RbacGraph, actor_id: UUID) -> dict[str, set[str]]:
    device = create_totp_device(actor_id, confirmed=True)
    user = User.objects.get(pk=actor_id)
    client = Client()
    with runtime_role():
        client.force_login(user)
        session = client.session
        session["active_org_id"] = str(graph.organization_a)
        session["otp_device_id"] = device.persistent_id
        session.save()
        response = client.get(agenda_at_url(graph.clinic_a, "day", DAY))
    assert response.status_code == 200
    buttons = LifecycleButtons()
    buttons.feed(response.content.decode())
    return buttons.actions


def _permitted(
    graph: RbacGraph, actor_id: UUID, rows: dict[str, Appointment]
) -> dict[str, set[str]]:
    permitted: dict[str, set[str]] = {}
    for status, row in rows.items():
        for action, transition in SERVICES.items():
            with permission_context(graph, actor_id):
                try:
                    with transaction.atomic():
                        transition(
                            clinic_id=row.clinic_id,
                            appointment_id=row.pk,
                            expected_revision=row.revision,
                            command_id=uuid4(),
                        )
                        transaction.set_rollback(True)
                except (
                    services.AppointmentAccessDeniedError,
                    services.AppointmentLifecycleError,
                ):
                    continue
            permitted.setdefault(status, set()).add(action)
    return permitted


@pytest.mark.parametrize(
    "roles", ROLE_UNIONS, ids=lambda roles: "+".join(("physician", *roles))
)
def test_booked_clinician_keeps_own_steps_in_every_role_union(
    rbac_graph: RbacGraph,
    lifecycle_rows: dict[str, Appointment],
    roles: tuple[str, ...],
) -> None:
    _add_roles(rbac_graph, rbac_graph.physician, roles)
    rendered = _rendered(rbac_graph, rbac_graph.physician)
    assert rendered == _permitted(rbac_graph, rbac_graph.physician, lifecycle_rows)
    assert rendered == OWN_STEPS


@pytest.mark.parametrize("role", MANAGEMENT)
def test_scheduling_staff_see_exactly_their_move_authority(
    rbac_graph: RbacGraph, lifecycle_rows: dict[str, Appointment], role: str
) -> None:
    actor_id, _ = permission_actor(rbac_graph, role)
    rendered = _rendered(rbac_graph, actor_id)
    assert rendered == _permitted(rbac_graph, actor_id, lifecycle_rows)
    moves = "appointment.move" in BUNDLES_V1[role]
    assert rendered == ({"scheduled": {"arrive"}} if moves else {})


@pytest.mark.parametrize("roles", [(), ("receptionist",)])
def test_narrowed_own_move_hides_care_but_keeps_reception_arrival(
    rbac_graph: RbacGraph,
    lifecycle_rows: dict[str, Appointment],
    roles: tuple[str, ...],
) -> None:
    _add_roles(rbac_graph, rbac_graph.physician, roles)
    narrower, _ = permission_actor(rbac_graph, "org_admin")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(rbac_graph.organization_a), str(narrower)],
        )
        cursor.execute("SELECT statement_timestamp() - interval '1 second'")
        row = cursor.fetchone()
        assert row is not None
        narrow_role(
            clinic_id=rbac_graph.clinic_a,
            role="physician",
            permission="appointment.move_own",
            valid_from=row[0],
        )
    rendered = _rendered(rbac_graph, rbac_graph.physician)
    assert rendered == _permitted(rbac_graph, rbac_graph.physician, lifecycle_rows)
    assert rendered == ({"scheduled": {"arrive"}} if roles else {})
