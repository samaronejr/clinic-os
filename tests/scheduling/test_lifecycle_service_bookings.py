"""Service bookings keep todo 21's DB guard on every status change (review B1).

At 135ef11 any UPDATE of a service booking (``service_type_id`` set) required
``appointment.move``, own ``appointment.move_own`` for the booked practitioner,
or the booked patient's session. Lifecycle v2 may only add edge rules on top of
that: here every stored state of a service booking takes a raw ``clinic_app``
UPDATE to every other status, for every actor in the role catalog plus the
booked/other physician, both patient sessions, an inactive and a foreign-clinic
actor and a tenant-only (no user) context, at a clock before and after the
bookings. No outcome may be more permissive than the 135ef11 rule, except the
machine's held -> expired edge.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    arrive,
    cancel,
    complete,
    expire,
    mark_no_show,
    start,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, transaction

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from renewal.test_self_booking import _session
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.lifecycle_world import (
    Actor,
    LifecycleWorld,
    _revision,
    _step,
    acting,
    add_actors,
    pinned_clock,
)
from scheduling.test_appointment_states import _sql_update, expected_sql
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

REFERENCE = datetime(2035, 6, 1, tzinfo=UTC)
SERVICE_STATES = (
    "held",
    "scheduled",
    "arrived",
    "in_progress",
    "completed",
    "expired",
    "no_show",
    "cancelled",
)
TARGETS = (*SERVICE_STATES, "requested")
# The reviewer's 135ef11 probe of a raw service-booking cancel (gate-review.md,
# "B1 reproduction"), copied as the baseline this lane must not loosen.
BASELINE_135EF11_CANCEL = {
    "owner": "denied",
    "other_physician": "denied",
    "receptionist": "ok",
    "clinic_admin": "ok",
    "nurse": "denied",
    "allied_professional": "denied",
    "scheduler": "ok",
    "clinic_manager": "ok",
    "finance": "denied",
    "org_admin": "denied",
    "own_physician": "ok",
    "machine": "denied",
}


def baseline_allowed(actor: Actor) -> bool:
    """Todo 21's service-booking UPDATE rule, derived from BUNDLES_V1."""
    if actor.kind == "patient":
        return actor.own_patient
    if actor.kind != "staff" or actor.role is None:
        return False
    bundle = BUNDLES_V1[actor.role]
    return "appointment.move" in bundle or (
        actor.own and "appointment.move_own" in bundle
    )


def expected_service_sql(source: str, target: str, actor: Actor, *, late: bool) -> str:
    lifecycle = expected_sql(source, target, actor, late=late)
    if lifecycle != "ok" or (source, target) == ("held", "expired"):
        return lifecycle
    return "ok" if baseline_allowed(actor) else "denied"


def _held_copy(template: Appointment, start_at: datetime) -> Appointment:
    row = Appointment.objects.create(
        **{
            field.attname: getattr(template, field.attname)
            for field in Appointment._meta.concrete_fields
        }
        | {
            "id": uuid4(),
            "idempotency_key": uuid4(),
            "start_at": start_at,
            "end_at": start_at + (template.end_at - template.start_at),
            "status": "held",
            "cancellation_reason": None,
            "cancelled_at": None,
        }
    )
    row.refresh_from_db()  # the trigger assigns revision and the hold deadline
    return row


def build_service_world(graph: RbacGraph, database_url: str) -> LifecycleWorld:
    """One service booking (room + equipment, 10/10 buffers) per stored state."""
    setup = seed_appointment_setup(graph)
    rows: dict[str, Appointment] = {}
    clinic = setup.clinic_id
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        # Terminal states reuse the 08:10 slot once they release capacity.
        rows["completed"] = _step(
            arrive, book(setup, catalog, start="08:10"), clinic_id=clinic
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        rows["completed"] = _step(start, rows["completed"], clinic_id=clinic)
        rows["completed"] = _step(complete, rows["completed"], clinic_id=clinic)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        rows["cancelled"] = _step(
            cancel, book(setup, catalog, start="08:10"), clinic_id=clinic
        )
        rows["no_show"] = book(setup, catalog, start="08:10")
    with (
        pinned_clock(database_url, rows["no_show"].start_at),
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        rows["no_show"] = _step(mark_no_show, rows["no_show"], clinic_id=clinic)
    template = rows["completed"]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        rows["expired"] = _held_copy(template, template.start_at)
    assert rows["expired"].hold_expires_at == REFERENCE + timedelta(minutes=10)
    with owner_context(graph.organization_a):
        revision = _revision(rows["expired"])
    with pinned_clock(database_url, REFERENCE + timedelta(minutes=10)), runtime_role():
        expire(
            clinic_id=clinic,
            appointment_id=rows["expired"].pk,
            expected_revision=revision,
            command_id=uuid4(),
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        rows["held"] = _held_copy(template, template.start_at)
        rows["scheduled"] = book(setup, catalog, start="09:00")
        rows["arrived"] = _step(
            arrive, book(setup, catalog, start="09:50"), clinic_id=clinic
        )
        rows["in_progress"] = _step(
            arrive, book(setup, catalog, start="10:40"), clinic_id=clinic
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        rows["in_progress"] = _step(start, rows["in_progress"], clinic_id=clinic)
    with owner_context(graph.organization_a):
        for state, row in rows.items():
            row.refresh_from_db()
            assert (row.status, row.service_type_id is not None) == (state, True)
    administrator, _ = permission_actor(graph, UserClinicRole.Role.CLINIC_ADMIN)
    with runtime_role(), tenant_context(administrator, graph.organization_a):
        publish_configuration(
            clinic_id=graph.clinic_a,
            expected_version=0,
            content=ConfigurationContent(
                display_name="Sintetico Clinica",
                self_booking_requires_approval=True,
            ),
        )
    world = LifecycleWorld(graph, setup, REFERENCE, rows)
    add_actors(world, _session(setup))
    return world


@pytest.fixture
def world(rbac_graph: RbacGraph, superuser_database_url: str) -> LifecycleWorld:
    return build_service_world(rbac_graph, superuser_database_url)


def test_every_service_booking_status_change_keeps_the_todo_21_guard(
    world: LifecycleWorld, superuser_database_url: str
) -> None:
    outcomes = _run_matrix(world, superuser_database_url)
    for (source, target, name, is_late), observed in outcomes.items():
        actor = world.actors[name]
        assert observed == expected_service_sql(source, target, actor, late=is_late), (
            source,
            target,
            name,
            is_late,
            observed,
        )
        if observed == "ok":
            # Never more permissive than 135ef11, except the W expiry edge.
            assert baseline_allowed(actor) or (
                (source, target) == ("held", "expired") and actor.kind == "machine"
            ), (source, target, name)
    assert len(outcomes) == 2 * len(SERVICE_STATES) * (len(TARGETS) - 1) * len(
        world.actors
    )
    cancel_row = {
        name: outcomes[("scheduled", "cancelled", name, False)]
        for name in BASELINE_135EF11_CANCEL
    }
    assert cancel_row == BASELINE_135EF11_CANCEL


def _run_matrix(
    world: LifecycleWorld, database_url: str
) -> dict[tuple[str, str, str, bool], str]:
    """Pin the clock once per phase (not per probe) to keep this cheap."""
    late = world.rows["in_progress"].end_at + timedelta(hours=1)
    outcomes: dict[tuple[str, str, str, bool], str] = {}
    for clock, is_late in ((None, False), (late, True)):
        with pinned_clock(database_url, clock) if clock else nullcontext():
            for source in SERVICE_STATES:
                row = world.rows[source]
                for target in TARGETS:
                    if target == source:
                        continue
                    for name, actor in world.actors.items():
                        with acting(world, actor):
                            observed = _sql_update(world, row, target)
                        outcomes[(source, target, name, is_late)] = observed
    return outcomes


def _insert_service_row(template: Appointment, status: str) -> str:
    try:
        with transaction.atomic():
            Appointment.objects.create(
                **{
                    field.attname: getattr(template, field.attname)
                    for field in Appointment._meta.concrete_fields
                }
                | {
                    "id": uuid4(),
                    "idempotency_key": uuid4(),
                    "start_at": template.start_at + timedelta(hours=2),
                    "end_at": template.end_at + timedelta(hours=2),
                    "status": status,
                }
            )
    except DatabaseError as error:
        cause = error.__cause__
        return "denied" if getattr(cause, "sqlstate", None) == "42501" else "refused"
    return "ok"


def test_a_requested_service_booking_cannot_be_created(world: LifecycleWorld) -> None:
    """No actor can create a requested (or patient) service booking.

    The approval policy is on, so a patient's plain request is admitted while
    the same row naming a service is refused: requested service rows never
    exist, so their edges have no subject.
    """
    template = world.rows["scheduled"]
    plain = replace_service(template)
    with acting(world, world.actors["own_patient"]):
        assert _insert_service_row(template, "requested") == "denied"
        assert _insert_service_row(template, "held") == "denied"
        assert _insert_service_row(plain, "requested") == "ok"
    with acting(world, world.actors["receptionist"]):
        assert _insert_service_row(template, "requested") == "denied"


def replace_service(template: Appointment) -> Appointment:
    """The same slot without service requirements (a plain patient request)."""
    plain = Appointment(
        **{
            field.attname: getattr(template, field.attname)
            for field in Appointment._meta.concrete_fields
        }
    )
    plain.service_type_id = None
    plain.resource_ids = []
    plain.buffer_before = 0
    plain.buffer_after = 0
    return plain
