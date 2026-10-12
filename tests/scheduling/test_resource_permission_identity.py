"""Each scheduling guard decides by its exact permission, never by a sibling.

BUNDLES_V1 gives several permissions identical role sets: appointment.book and
appointment.move; configuration.organization with staff.organization,
finance.policy and automation.organization; appointment.book_own with
appointment.move_own and the other physician-only permissions. A role matrix
cannot tell a guard that checks the wrong one. Here a clinic-local narrowing
(narrow_role) removes exactly one permission inside a rolled-back transaction:
removing a checked permission refuses unless another checked term still
grants, and removing every same-role-set sibling changes nothing.

Guard specifications are pinned from the RP matrix of plan todo 21, never read
from guard bodies, so a swapped permission cannot rewrite its own expectation.
Own-scope terms are exercised with the actor as the booked practitioner and as
a different physician. The professional projection is compared as an exact
set. Python guards are also spied: the permission names reaching
has_permission must be exactly the pinned ones, in order.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.identity.scope_provisioning import narrow_role
from apps.scheduling import resource_services
from apps.scheduling.models import Appointment, AvailabilityBlock, Resource
from apps.scheduling.resource_booking import (
    authorized_service_clinic,
    authorized_transition_clinic,
    service_practitioners,
)
from apps.scheduling.services import AppointmentAccessDeniedError
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from uuid import UUID

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

ROLES = tuple(UserClinicRole.Role.values)
# RP clinical professional roles: the only roles a service booking may name.
PROFESSIONAL_ROLES = frozenset({"physician", "nurse", "allied_professional"})
NARROWING_AUTHORITY = "staff.organization"


@dataclass(frozen=True)
class Term:
    permission: str
    # Granted only when the actor is the booked practitioner (own scope).
    own: bool = False


# Plan todo 21 Authz: scheduler/reception/manager configure; booking per RP.
CONFIGURE = (Term("appointment.book"), Term("configuration.organization"))
BOOK = (Term("appointment.book"), Term("appointment.book_own", own=True))
MOVE = (Term("appointment.move"), Term("appointment.move_own", own=True))
PROJECTION = (
    Term("appointment.book"),
    Term("appointment.move"),
    Term("configuration.organization"),
    Term("appointment.book_own", own=True),
    Term("appointment.move_own", own=True),
)
GUARDS: dict[str, tuple[Term, ...]] = {
    "definition_trigger": CONFIGURE,
    "generated_block_trigger": CONFIGURE,
    "capacity_update_trigger": MOVE,
    "capacity_insert_trigger": BOOK,
    "projection": PROJECTION,
    "configuration_clinic": CONFIGURE,
    "authorized_service_clinic": BOOK,
    "authorized_transition_clinic": MOVE,
    "subject": PROJECTION,
}
SPIED = {
    "configuration_clinic",
    "authorized_service_clinic",
    "authorized_transition_clinic",
    "subject",
}


def holders(permission: str) -> frozenset[str]:
    return frozenset(
        role for role, bundle in BUNDLES_V1.items() if permission in bundle
    )


def siblings(permission: str, role: str) -> frozenset[str]:
    """Permissions of the role's bundle that no role matrix can tell apart."""
    return frozenset(
        other
        for other in BUNDLES_V1[role]
        if other != permission and holders(other) == holders(permission)
    )


def grants(
    terms: Sequence[Term], role: str, narrowed: frozenset[str], own: bool
) -> bool:
    return any(
        term.permission in BUNDLES_V1[role]
        and term.permission not in narrowed
        and (own or not term.own)
        for term in terms
    )


def asked(terms: Sequence[Term], role: str, narrowed: frozenset[str]) -> list[str]:
    """Permission names a guard asks, in order, until one is held."""
    names = []
    for term in terms:
        names.append(term.permission)
        if term.permission in BUNDLES_V1[role] and term.permission not in narrowed:
            break
    return names


@dataclass(frozen=True)
class Case:
    role: str
    own: bool
    narrowed: frozenset[str]


def cases(terms: Sequence[Term]) -> list[Case]:
    held = {role for term in terms for role in holders(term.permission)}
    outsider = next(role for role in ROLES if role not in held)
    found = [Case(outsider, own=False, narrowed=frozenset())]
    for term in terms:
        candidates = holders(term.permission)
        if term.own:
            candidates &= PROFESSIONAL_ROLES
        role = sorted(candidates)[0]
        every_term = frozenset(t.permission for t in terms) & BUNDLES_V1[role]
        for own in (True, False) if term.own else (False,):
            for narrowed in (
                frozenset(),
                frozenset({term.permission}),
                siblings(term.permission, role),
                every_term,
            ):
                case = Case(role, own, narrowed)
                if case not in found:
                    found.append(case)
    return found


def test_the_guard_specifications_can_tell_siblings_apart() -> None:
    assert set(BUNDLES_V1) == set(ROLES)
    for name, terms in GUARDS.items():
        for term in terms:
            assert holders(term.permission), (name, term)
            role = sorted(
                holders(term.permission)
                & (PROFESSIONAL_ROLES if term.own else frozenset(ROLES))
            )[0]
            # Siblings exist, so the swap classes are actually exercised.
            assert siblings(term.permission, role), (name, term)
            assert NARROWING_AUTHORITY in BUNDLES_V1["owner"]
        found = cases(terms)
        # Every guard has granted and refused cases.
        outcomes = {grants(terms, c.role, c.narrowed, c.own) for c in found}
        assert outcomes == {True, False}, name


@dataclass(frozen=True)
class World:
    graph: RbacGraph
    setup: AppointmentSetup
    clinic: Clinic
    room: Resource
    booked: Appointment
    actors: dict[str, UUID]
    professionals: frozenset[UUID]


def _world(graph: RbacGraph) -> World:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        booked = book(setup, catalog)
    assert booked.practitioner_id == graph.physician
    actors = {role: permission_actor(graph, role)[0] for role in ROLES}
    with owner_context(graph.organization_a):
        clinic = Clinic.objects.get(pk=graph.clinic_a)
        # Owner-read: every active professional of the clinic, derived.
        professionals = frozenset(
            UserClinicRole.objects.filter(
                clinic_id=graph.clinic_a,
                role__in=PROFESSIONAL_ROLES,
                user__is_active=True,
            ).values_list("user_id", flat=True)
        )
    assert graph.physician in professionals
    return World(graph, setup, clinic, catalog[0], booked, actors, professionals)


def _actor(world: World, case: Case) -> UUID:
    if case.own:
        # The booked practitioner acting as themself.
        role = UserClinicRole.Role.PHYSICIAN
        assert case.role == role, "the seeded booking names the clinic physician"
        return world.graph.physician
    return world.actors[case.role]


@contextmanager
def _narrowed(world: World, case: Case, actor: UUID) -> Iterator[None]:
    """One rolled-back transaction: clinic subtractions, then act as clinic_app."""
    graph = world.graph
    narrower = next(
        world.actors[role]
        for role in sorted(holders(NARROWING_AUTHORITY))
        if role != case.role
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(graph.organization_a)],
            )
        if case.narrowed:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_owner")
                cursor.execute(
                    "SELECT statement_timestamp() - interval '1 second' "
                    "FROM set_config('app.current_user_id', %s, true)",
                    [str(narrower)],
                )
                row = cursor.fetchone()
            assert row is not None
            for permission in sorted(case.narrowed):
                narrow_role(
                    clinic_id=graph.clinic_a,
                    role=case.role,
                    permission=permission,
                    valid_from=row[0],
                )
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(actor)]
            )
        yield
        transaction.set_rollback(True)


@contextmanager
def _checked_permissions() -> Iterator[list[str]]:
    """Record every permission the application asks has_permission about."""
    names: list[str] = []

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if "clinic_app.has_permission(" in sql and params:
            names.append(str(params[0]))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        yield names


def _decided(call: Callable[[], object]) -> bool:
    """Run in a savepoint; only the permission refusals count as refusals."""
    state = "none"
    try:
        with transaction.atomic():
            call()
    except (AppointmentAccessDeniedError, CurrentActorError):
        state = "refused"
    except DatabaseError as error:
        state = str(getattr(error.__cause__, "sqlstate", None))
    assert state in {"none", "refused", "42501"}, state
    return state == "none"


def _copy(booked: Appointment, minutes: int) -> Appointment:
    shift = timedelta(minutes=minutes)
    return Appointment(
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


def _probe(world: World, name: str, actor: UUID, case: Case) -> object:
    clinic_id = world.graph.clinic_a
    practitioner = actor if case.own else world.graph.physician
    booked = world.booked
    probes: dict[str, Callable[[], object]] = {
        "definition_trigger": lambda: _decided(
            lambda: Resource.objects.create(
                organization_id=world.graph.organization_a,
                clinic_id=clinic_id,
                name="Sintetico identity",
                kind="room",
            )
        ),
        "generated_block_trigger": lambda: _decided(
            lambda: AvailabilityBlock.objects.create(
                organization_id=world.graph.organization_a,
                clinic_id=clinic_id,
                resource_id=world.room.pk,
                start_at=booked.start_at + timedelta(days=30),
                end_at=booked.end_at + timedelta(days=30),
                idempotency_key=uuid4(),
                create_fingerprint=b"i" * 32,
            )
        ),
        "capacity_update_trigger": lambda: _decided(
            lambda: Appointment.objects.filter(pk=booked.pk).update(
                updated_at=booked.updated_at
            )
        ),
        "capacity_insert_trigger": lambda: _decided(
            lambda: _copy(booked, 90).save(force_insert=True)
        ),
        "projection": lambda: frozenset(
            pk for pk, _ in service_practitioners(clinic_id)
        ),
        "configuration_clinic": lambda: _decided(
            lambda: resource_services.configuration_clinic(clinic_id)
        ),
        "authorized_service_clinic": lambda: _decided(
            lambda: authorized_service_clinic(clinic_id, practitioner)
        ),
        "authorized_transition_clinic": lambda: _decided(
            lambda: authorized_transition_clinic(booked)
        ),
        "subject": lambda: _decided(
            lambda: resource_services._subject(world.clinic, practitioner, None)
        ),
    }
    return probes[name]()


def _expected(world: World, name: str, actor: UUID, case: Case) -> object:
    terms = GUARDS[name]
    if name in {"projection", "subject"}:
        wide = tuple(term for term in terms if not term.own)
        own = tuple(term for term in terms if term.own)
        if grants(wide, case.role, case.narrowed, own=False):
            visible = world.professionals
        elif grants(own, case.role, case.narrowed, own=True) and actor in (
            world.professionals
        ):
            # Own scope: exactly the actor, never another professional.
            visible = frozenset({actor})
        else:
            visible = frozenset()
        if name == "projection":
            return visible
        return (actor if case.own else world.graph.physician) in visible
    return grants(terms, case.role, case.narrowed, case.own)


@pytest.mark.parametrize("name", sorted(GUARDS))
def test_guard_decides_by_its_exact_permission(
    rbac_graph: RbacGraph, name: str
) -> None:
    world = _world(rbac_graph)
    terms = GUARDS[name]
    for case in cases(terms):
        actor = _actor(world, case)
        with _narrowed(world, case, actor), _checked_permissions() as names:
            decision = _probe(world, name, actor, case)
        assert decision == _expected(world, name, actor, case), (name, case)
        if name in SPIED:
            expected_names = (
                [] if name == "subject" else asked(terms, case.role, case.narrowed)
            )
            assert names == expected_names, (name, case, names)
