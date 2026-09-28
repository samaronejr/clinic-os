"""Shared todo 22 world: one appointment per lifecycle state plus every actor.

Holds are created at the fixture reference ``T0``; the scheduled row's start is
the no-show deadline. ``pinned_clock`` re-freezes every audited scheduling SQL
clock (the census-controlled readers) to an exact instant inside the test.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.models import User, UserClinicRole
from apps.intake.patient_access import patient_session_context
from apps.intake.services import create_patient
from apps.scheduling.models import Appointment
from apps.scheduling.patient_booking import book_patient_slot, patient_slots
from apps.scheduling.services import (
    AppointmentLocalRange,
    arrive,
    cancel,
    complete,
    create_appointment,
    create_hold,
    expire,
    mark_no_show,
    start,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from psycopg import sql

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from renewal.test_self_booking import _session
from scheduling.appointment_service_support import (
    AppointmentSetup,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from rbac_fixtures import RbacGraph

DAY = "2035-06-02"
STATES = (
    "requested",
    "held",
    "scheduled",
    "arrived",
    "in_progress",
    "completed",
    "expired",
    "no_show",
    "cancelled",
)
SLOTS = {
    "held": "08:30",
    "scheduled": "09:00",
    "arrived": "09:30",
    "in_progress": "10:00",
    "completed": "10:30",
    "expired": "11:00",
    "no_show": "11:30",
    "cancelled": "08:00",
}


@dataclass(frozen=True)
class Actor:
    """One acting identity; exactly one of user/session, or neither (machine)."""

    kind: str
    role: str | None = None
    user_id: UUID | None = None
    session_id: UUID | None = None
    own: bool = False
    own_patient: bool = False


@dataclass
class LifecycleWorld:
    graph: RbacGraph
    setup: AppointmentSetup
    reference: datetime
    rows: dict[str, Appointment]
    actors: dict[str, Actor] = field(default_factory=dict)
    patient_session: UUID | None = None


def _range(slot: str) -> AppointmentLocalRange:
    hour, minute = map(int, slot.split(":"))
    end = f"{hour + (minute + 30) // 60:02d}:{(minute + 30) % 60:02d}"
    return AppointmentLocalRange(f"{DAY}T{slot}", f"{DAY}T{end}")


def _revision(row: Appointment) -> int:
    row.refresh_from_db()
    return row.revision


def _step(function: object, row: Appointment, *, clinic_id: UUID) -> Appointment:
    assert callable(function)
    result = function(
        clinic_id=clinic_id,
        appointment_id=row.pk,
        expected_revision=_revision(row),
        command_id=uuid4(),
    )
    assert isinstance(result, Appointment)
    return result


@contextmanager
def pinned_clock(database_url: str, instant: datetime) -> Iterator[None]:
    """Pin the lifecycle v2 clock reader (hold TTL/expiry, no-show) exactly.

    Only ``clinic_app.scheduling_clock()`` changes; the legacy guards keep the
    fixture's frozen reference. The prior definition is restored byte-exactly.
    """
    signature = "clinic_app.scheduling_clock()"
    with psycopg.connect(database_url, autocommit=True) as owner:
        row = owner.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", [signature]
        ).fetchone()
        assert row is not None
        original = str(row[0])
        head, marker, _ = original.partition("AS $function$\n")
        assert marker
        literal = sql.Literal(instant).as_string(owner)
        owner.execute(
            (head + marker + f" SELECT {literal}::timestamptz\n$function$\n").encode()
        )
        try:
            yield
        finally:
            owner.execute(original.encode())
            restored = owner.execute(
                "SELECT pg_get_functiondef(%s::regprocedure)", [signature]
            ).fetchone()
            assert restored == (original,)


def _other_patient_session(setup: AppointmentSetup) -> UUID:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        other = create_patient(
            clinic_id=setup.clinic_id,
            full_name="Sintetico Outro Paciente",
            birth_date=date(1990, 3, 4),
            idempotency_key=uuid4(),
        )
    return _session(
        AppointmentSetup(
            setup.organization_id,
            setup.clinic_id,
            setup.actor_id,
            setup.practitioner_id,
            other.enrollment.pk,
            other.patient.pk,
        )
    )


def _special_actors(graph: RbacGraph) -> dict[str, Actor]:
    inactive, _ = permission_actor(graph, UserClinicRole.Role.RECEPTIONIST)
    User.objects.filter(pk=inactive).update(is_active=False)
    foreign = User.objects.create(username=f"sintetico-foreign-{uuid4().hex}")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=foreign,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    other_physician, _ = permission_actor(graph, UserClinicRole.Role.PHYSICIAN)
    return {
        "inactive_receptionist": Actor("inactive", "receptionist", inactive),
        "foreign_clinic_receptionist": Actor("foreign", "receptionist", foreign.pk),
        "other_physician": Actor("staff", "physician", other_physician),
        "own_physician": Actor("staff", "physician", graph.physician, own=True),
        "machine": Actor("machine"),
    }


def build_world(
    graph: RbacGraph, database_url: str, reference: datetime
) -> LifecycleWorld:
    """Seed one row per state through the real services and DB clock."""
    setup = seed_appointment_setup(graph)
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
    session_id = _session(setup)
    rows: dict[str, Appointment] = {}
    with runtime_role(), patient_session_context(session_id):
        token = patient_slots(date(2035, 6, 2))[0].token
        rows["requested"] = book_patient_slot(token=token, idempotency_key=uuid4())
    assert rows["requested"].status == "requested"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        for state in ("held", "expired"):
            rows[state] = create_hold(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                practitioner_id=setup.practitioner_id,
                local_range=_range(SLOTS[state]),
                idempotency_key=uuid4(),
            )
        for state in (
            "scheduled",
            "arrived",
            "in_progress",
            "completed",
            "no_show",
            "cancelled",
        ):
            rows[state] = create_appointment(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                practitioner_id=setup.practitioner_id,
                local_range=_range(SLOTS[state]),
                idempotency_key=uuid4(),
            )
        for state in ("arrived", "in_progress", "completed"):
            rows[state] = _step(arrive, rows[state], clinic_id=setup.clinic_id)
        rows["cancelled"] = _step(cancel, rows["cancelled"], clinic_id=setup.clinic_id)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        for state in ("in_progress", "completed"):
            rows[state] = _step(start, rows[state], clinic_id=setup.clinic_id)
        rows["completed"] = _step(
            complete, rows["completed"], clinic_id=setup.clinic_id
        )
    expired = rows["expired"]
    assert expired.hold_expires_at == reference + timedelta(minutes=10)
    with owner_context(graph.organization_a):
        revision = _revision(expired)
    with pinned_clock(database_url, expired.hold_expires_at), runtime_role():
        expire(
            clinic_id=setup.clinic_id,
            appointment_id=expired.pk,
            expected_revision=revision,
            command_id=uuid4(),
        )
    with (
        pinned_clock(database_url, rows["no_show"].start_at),
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        rows["no_show"] = _step(
            mark_no_show, rows["no_show"], clinic_id=setup.clinic_id
        )
    with owner_context(graph.organization_a):
        for state, row in rows.items():
            row.refresh_from_db()
            assert row.status == state, (state, row.status)
    world = LifecycleWorld(graph, setup, reference, rows, patient_session=session_id)
    add_actors(world, session_id)
    return world


def add_actors(world: LifecycleWorld, session_id: UUID) -> None:
    """Every catalog role plus own/other physician and patient, inactive, foreign, W."""
    graph = world.graph
    for role in UserClinicRole.Role.values:
        world.actors[role] = Actor("staff", role, permission_actor(graph, role)[0])
    world.actors.update(_special_actors(graph))
    world.actors["own_patient"] = Actor(
        "patient", session_id=session_id, own_patient=True
    )
    world.actors["other_patient"] = Actor(
        "patient", session_id=_other_patient_session(world.setup)
    )


@contextmanager
def acting(world: LifecycleWorld, actor: Actor) -> Iterator[None]:
    """One rolled-back transaction as clinic_app with exactly this actor's GUCs."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            if actor.kind == "patient":
                cursor.execute(
                    "SELECT set_config('app.current_patient_session', %s, true)",
                    [str(actor.session_id)],
                )
            else:
                cursor.execute(
                    "SELECT set_config('app.current_tenant', %s, true)",
                    [str(world.graph.organization_a)],
                )
                if actor.user_id is not None:
                    cursor.execute(
                        "SELECT set_config('app.current_user_id', %s, true)",
                        [str(actor.user_id)],
                    )
        yield
        transaction.set_rollback(True)
