"""Todo 21's actor readers refuse an inactive actor on every OR branch.

``scheduling_capacity_guard()`` and ``scheduling_service_practitioners(uuid)``
read ``app.current_user_id`` only for own-scope equality, and they mention
``identity_user.is_active``. That clause constrains the professional they
list or book, not the actor, so fix-a9's census derives them as own-clause
readers without a probe that would prove the actor's refusal.

Their actor refusal is ``has_permission`` (own is_active clause, executed in
``test_sql_inactive_actor``) on every staff OR branch. Each branch is derived
here from the live definition and executed true/false/true, with only
``identity_user.is_active`` flipped. Every other branch is narrowed away in the
same rolled-back transaction, so the flip is decided by that branch alone. The
refusal must be the function's own: the guard's 42501 raised by
``scheduling_capacity_guard``, or an empty projection where an active actor
sees an exact set.

The capacity guard's remaining branch, ``patient_booking_scope()``, reads only
``app.current_patient_session``: it is a patient principal, not an actor. It is
executed true/false/true on session validity (its own RLS scope refuses an
invalid session) and shown never to admit a staff actor.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.identity.scope_provisioning import narrow_role
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from identity.authority_sql import references
from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from renewal.test_self_booking import _session
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from rbac_fixtures import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)

CAPACITY = "clinic_app.scheduling_capacity_guard()"
PROJECTION = "clinic_app.scheduling_service_practitioners(uuid)"
# fix-a9's own-clause readers whose is_active clause is about the professional
# they list or book; each is certified per branch below instead of by PROBES.
BRANCH_CERTIFIED = frozenset({CAPACITY, PROJECTION})
PATIENT_BRANCH = "patient_booking_scope"
OWN_TERMS = frozenset({"appointment.book_own", "appointment.move_own"})
PROFESSIONAL_ROLES = frozenset({"physician", "nurse", "allied_professional"})
NARROWING_AUTHORITY = "staff.organization"
PERMISSION = re.compile(r"has_permission\('([a-z_.]+)'")
GUARD_RAISE = re.compile(
    r"PL/pgSQL function (?:clinic_app\.)?(\w+)\(\) line \d+ at RAISE"
)
CAPACITY_REFUSAL = ("42501", "scheduling access denied", CAPACITY)


def _definition(signature: str) -> str:
    with connection.cursor() as cursor:
        # The body, exactly as fix-a9's census parses it.
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure", [signature]
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


def derived_branches(signature: str) -> frozenset[str]:
    """Every OR branch the live body grants through: permissions, principals."""
    source = _definition(signature)
    found = set(PERMISSION.findall(source))
    parsed = references(source)
    # A set-returning call in FROM parses as a name rather than a call.
    if PATIENT_BRANCH in {ref[-1] for ref in (*parsed.calls, *parsed.names)}:
        found.add(PATIENT_BRANCH)
    return frozenset(found)


def holders(permission: str) -> frozenset[str]:
    return frozenset(
        role for role, bundle in BUNDLES_V1.items() if permission in bundle
    )


@dataclass(frozen=True)
class Seed:
    graph: RbacGraph
    setup: AppointmentSetup
    booked: Appointment
    professionals: frozenset[UUID]
    narrower: UUID


def _seed(graph: RbacGraph) -> Seed:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        booked = book(setup, _catalog(setup), start="09:00")
    narrower, _ = permission_actor(graph, sorted(holders(NARROWING_AUTHORITY))[0])
    with owner_context(graph.organization_a):
        # Owner-read, derived: every active professional of the clinic.
        professionals = frozenset(
            UserClinicRole.objects.filter(
                clinic_id=graph.clinic_a,
                role__in=PROFESSIONAL_ROLES,
                user__is_active=True,
            ).values_list("user_id", flat=True)
        )
    assert graph.physician in professionals
    return Seed(graph, setup, booked, professionals, narrower)


def _set_active(actor: UUID, *, active: bool) -> None:
    # Raw SQL as the test connection: no model hook or Python actor check.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET is_active = %s WHERE id = %s",
            [active, actor],
        )
        assert cursor.rowcount == 1


@contextmanager
def _as_actor(
    seed: Seed, actor: UUID | None, role: str, narrowed: frozenset[str]
) -> Iterator[None]:
    """One rolled-back transaction: narrow the other branches, then clinic_app."""
    graph = seed.graph
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(graph.organization_a)],
            )
        if narrowed:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_owner")
                cursor.execute(
                    "SELECT statement_timestamp() - interval '1 second' "
                    "FROM set_config('app.current_user_id', %s, true)",
                    [str(seed.narrower)],
                )
                row = cursor.fetchone()
            assert row is not None
            for permission in sorted(narrowed):
                narrow_role(
                    clinic_id=graph.clinic_a,
                    role=role,
                    permission=permission,
                    valid_from=row[0],
                )
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                ["" if actor is None else str(actor)],
            )
        yield
        transaction.set_rollback(True)


type Outcome = str | tuple[str | None, str | None, str | None]


def _guarded(statement: Callable[[], object]) -> Outcome:
    """ "passed", "hidden" when RLS matched no row, or the refusal's sqlstate,
    message and raising guard."""
    try:
        with transaction.atomic():
            if statement() == 0:
                return "hidden"
    except DatabaseError as error:
        cause = error.__cause__
        if not isinstance(cause, psycopg.Error):
            raise
        raised = GUARD_RAISE.search(cause.diag.context or "")
        return (
            cause.sqlstate,
            cause.diag.message_primary,
            f"clinic_app.{raised.group(1)}()" if raised else None,
        )
    return "passed"


def _update(booked: Appointment) -> Outcome:
    return _guarded(
        lambda: Appointment.objects.filter(pk=booked.pk).update(
            updated_at=booked.updated_at
        )
    )


def _insert(booked: Appointment) -> Outcome:
    shift = timedelta(minutes=90)
    copy = Appointment(
        **{
            field.attname: getattr(booked, field.attname)
            for field in Appointment._meta.concrete_fields
        }
        | {
            "id": uuid4(),
            "idempotency_key": uuid4(),
            "start_at": booked.start_at + shift,
            "end_at": booked.end_at + shift,
        }
    )

    def save() -> None:
        copy.save(force_insert=True)

    return _guarded(save)


def _projection(seed: Seed) -> frozenset[UUID]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT user_id FROM clinic_app.scheduling_service_practitioners(%s)",
            [seed.graph.clinic_a],
        )
        return frozenset(row[0] for row in cursor.fetchall())


@dataclass(frozen=True)
class Branch:
    """The one branch left granting: its role, and whether the actor is the
    booked practitioner (own scope)."""

    role: str
    own: bool


def _branch(permission: str) -> Branch:
    if permission in OWN_TERMS:
        role = sorted(holders(permission) & PROFESSIONAL_ROLES)[0]
        return Branch(role, own=True)
    return Branch(sorted(holders(permission))[0], own=False)


def _others(role: str, permission: str, branches: frozenset[str]) -> frozenset[str]:
    """Narrow every other derived branch the role holds."""
    return frozenset(
        other
        for other in branches
        if other not in {permission, PATIENT_BRANCH} and other in BUNDLES_V1[role]
    )


def test_every_derived_branch_is_executed() -> None:
    # The executed set must equal the derived set: a new branch needs a probe.
    assert derived_branches(CAPACITY) == frozenset(CAPACITY_BRANCHES)
    assert derived_branches(PROJECTION) == frozenset(PROJECTION_BRANCHES)
    for permission in {*CAPACITY_BRANCHES, *PROJECTION_BRANCHES} - {PATIENT_BRANCH}:
        branch = _branch(permission)
        assert permission in BUNDLES_V1[branch.role]
        if branch.own:
            # The seeded booking's practitioner is the clinic physician.
            assert branch.role == UserClinicRole.Role.PHYSICIAN


# Capacity trigger branches and the statement that reaches each.
CAPACITY_BRANCHES: dict[str, Callable[[Appointment], Outcome]] = {
    "appointment.move": _update,
    "appointment.move_own": _update,
    "appointment.book": _insert,
    "appointment.book_own": _insert,
    PATIENT_BRANCH: _update,
}
PROJECTION_BRANCHES = frozenset(
    {
        "appointment.book",
        "appointment.move",
        "configuration.organization",
        "appointment.book_own",
        "appointment.move_own",
    }
)


def _actor(seed: Seed, branch: Branch) -> UUID:
    if branch.own:
        return seed.graph.physician
    actor, _ = permission_actor(seed.graph, branch.role)
    return actor


def test_capacity_guard_refuses_inactive_actor_on_every_staff_branch(
    rbac_graph: RbacGraph,
) -> None:
    seed = _seed(rbac_graph)
    branches = frozenset(CAPACITY_BRANCHES)
    rows: dict[str, tuple[Outcome, ...]] = {}
    for permission, statement in sorted(CAPACITY_BRANCHES.items()):
        if permission == PATIENT_BRANCH:
            continue
        branch = _branch(permission)
        actor = _actor(seed, branch)
        phases = []
        for active in (True, False, True):
            _set_active(actor, active=active)
            with _as_actor(
                seed, actor, branch.role, _others(branch.role, permission, branches)
            ):
                phases.append(statement(seed.booked))
        rows[permission] = tuple(phases)
    assert rows == dict.fromkeys(
        sorted(branches - {PATIENT_BRANCH}), ("passed", CAPACITY_REFUSAL, "passed")
    )


def test_projection_refuses_inactive_actor_on_every_branch(
    rbac_graph: RbacGraph,
) -> None:
    seed = _seed(rbac_graph)
    rows: dict[str, tuple[frozenset[UUID], ...]] = {}
    expected: dict[str, tuple[frozenset[UUID], ...]] = {}
    for permission in sorted(PROJECTION_BRANCHES):
        branch = _branch(permission)
        actor = _actor(seed, branch)
        # Own scope sees exactly the actor; a clinic-wide branch every professional.
        visible = frozenset({actor}) if branch.own else seed.professionals
        phases = []
        for active in (True, False, True):
            _set_active(actor, active=active)
            with _as_actor(
                seed,
                actor,
                branch.role,
                _others(branch.role, permission, PROJECTION_BRANCHES),
            ):
                phases.append(_projection(seed))
        rows[permission] = tuple(phases)
        expected[permission] = (visible, frozenset(), visible)
        assert visible, permission
    assert rows == expected


def _set_session_valid(database_url: str, session_id: UUID, *, valid: bool) -> None:
    # As the test superuser: the session table is tenant-scoped under RLS.
    with psycopg.connect(database_url, autocommit=True) as raw:
        updated = raw.execute(
            "UPDATE clinic_app.intake_patientsession SET idle_expires_at = "
            "CASE WHEN %s THEN statement_timestamp() + interval '1 hour' "
            "ELSE statement_timestamp() - interval '1 second' END WHERE id = %s",
            [valid, session_id],
        ).rowcount
    assert updated == 1


def test_capacity_patient_branch_is_a_patient_principal(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    """The branch reads only the patient session; staff activity cannot reach it."""
    seed = _seed(rbac_graph)
    assert (
        "app.current_user_id"
        not in references(_definition("clinic_app.patient_booking_scope()")).settings
    )
    session = _session(seed.setup)
    phases = []
    for valid in (True, False, True):
        _set_session_valid(superuser_database_url, session, valid=valid)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(
                "SELECT set_config('app.current_patient_session', %s, true)",
                [str(session)],
            )
            phases.append(_update(seed.booked))
            transaction.set_rollback(True)
    # The patient principal's own scope refuses an invalid session: its RLS
    # policy (patient_booking_scope) no longer admits the row.
    assert phases == ["passed", "hidden", "passed"]
    # Without a patient session the branch never admits a staff actor, active or
    # inactive: a staff role with no move permission is refused by the guard.
    outsider = next(
        role
        for role in UserClinicRole.Role.values
        if not {"appointment.move", "appointment.move_own"} & BUNDLES_V1[role]
    )
    actor, _ = permission_actor(rbac_graph, outsider)
    staff = []
    for active in (True, False, True):
        _set_active(actor, active=active)
        with _as_actor(seed, actor, outsider, frozenset()):
            staff.append(_update(seed.booked))
    assert staff == [CAPACITY_REFUSAL] * 3


def test_branch_certified_readers_are_not_active_actor_clauses() -> None:
    """Their is_active clause filters listed/booked professionals, not the actor."""
    for signature in BRANCH_CERTIFIED:
        source = _definition(signature)
        assert "identity_user" in source, signature
        assert "is_active" in source, signature
        # The actor setting appears only in own-scope equalities.
        for line in source.splitlines():
            if "app.current_user_id" in line:
                assert re.search(r"(u\.id|NEW\.practitioner_id)\s*=\s*NULLIF", line), (
                    signature,
                    line,
                )
