"""Every stored role is refused wherever its bundle lacks the scheduling permission.

The census labels these guards "delegated" to has_permission; this module does
not rely on that label. Roles come from UserClinicRole.Role and permitted sets
from BUNDLES_V1, so a new role or a changed bundle changes the matrix. Each new
todo 21 boundary runs as clinic_app: configuration services, the generation
command, service booking and transitions, the professional projection, the
Settings route, and the PostgreSQL triggers behind them.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import date, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.scheduling.models import Appointment, Resource
from apps.scheduling.resource_booking import (
    authorized_service_clinic,
    authorized_transition_clinic,
    service_practitioners,
)
from apps.scheduling.resource_services import (
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
    configuration_clinic,
    create_closure,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
    retire_definition,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentLocalRange,
    ServiceBooking,
    cancel_appointment,
    create_availability,
    create_service_appointment,
    reschedule_appointment,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction
from django.test import Client

from auth.stepup_test_support import create_role_actor
from identity.permission_support import permission_actor, permission_context
from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    get_totp_device,
    login,
    token_for,
)
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.test.client import _MonkeyPatchedWSGIResponse

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

ROLES = tuple(UserClinicRole.Role.values)
CONFIGURE = ("appointment.book", "configuration.organization")
BOOK, BOOK_OWN = "appointment.book", "appointment.book_own"
MOVE, MOVE_OWN = "appointment.move", "appointment.move_own"
PROJECTION = ("appointment.book", "appointment.move", "configuration.organization")
# RP clinical professional roles: the only roles a service booking may name.
PROFESSIONAL_ROLES = frozenset({"physician", "nurse", "allied_professional"})


def permitted(*permissions: str) -> frozenset[str]:
    return frozenset(
        role
        for role, bundle in BUNDLES_V1.items()
        if any(permission in bundle for permission in permissions)
    )


def _allowed(call: Callable[[], object]) -> bool:
    try:
        call()
    except AppointmentAccessDeniedError:
        return False
    return True


def _sql_allowed(call: Callable[[], object]) -> bool:
    state = "none"
    try:
        with transaction.atomic():
            call()
            transaction.set_rollback(True)
    except DatabaseError as error:
        state = str(getattr(error.__cause__, "sqlstate", None))
    # Only the permission refusal counts; any other failure is a defect.
    assert state in {"none", "42501"}, state
    return state == "none"


def _definition_row(clinic: UUID, organization: UUID) -> Resource:
    return Resource.objects.create(
        organization_id=organization,
        clinic_id=clinic,
        name="Sintetico matrix SQL",
        kind="room",
    )


def test_role_catalog_is_the_bundle_catalog_and_splits_every_guard() -> None:
    assert set(BUNDLES_V1) == set(ROLES)
    for permissions in (CONFIGURE, (BOOK,), (BOOK_OWN,), (MOVE,), (MOVE_OWN,)):
        # Both sides non-empty: the matrix proves grants and refusals.
        assert permitted(*permissions), permissions
        assert set(ROLES) - permitted(*permissions), permissions
    # Own-scope holders must be bookable professionals for the own path.
    assert permitted(BOOK_OWN) | permitted(MOVE_OWN) <= PROFESSIONAL_ROLES


def _template(setup: AppointmentSetup, resource: UUID) -> UUID:
    return create_template(
        clinic_id=setup.clinic_id,
        content=TemplateInput(
            resource_id=resource,
            weekdays=(date(2035, 6, 9).weekday(),),
            start_local=time(8),
            end_local=time(12),
            valid_from=date(2035, 6, 9),
            valid_to=date(2035, 6, 9),
        ),
    ).pk


@pytest.mark.parametrize("role", ROLES)
def test_configuration_boundaries_follow_the_role_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, _, _ = _catalog(setup)
        template = _template(setup, room.pk)
        spare = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="Sintetico matrix spare", kind="location"),
        )
    actor, _ = permission_actor(rbac_graph, role)
    expected = role in permitted(*CONFIGURE)
    clinic = setup.clinic_id
    calls: dict[str, Callable[[], object]] = {
        # The guard itself first: PostgreSQL would mask a removed Python check.
        "guard": lambda: configuration_clinic(clinic),
        "resource": lambda: create_resource(
            clinic_id=clinic,
            content=ResourceInput(name="Sintetico matrix", kind="location"),
        ),
        "service": lambda: create_service_type(
            clinic_id=clinic,
            content=ServiceInput(name="Sintetico matrix", duration_min=30),
        ),
        "template": lambda: _template(setup, room.pk),
        "closure": lambda: create_closure(
            clinic_id=clinic,
            content=ClosureInput(
                start_local="2035-12-24T08:00",
                end_local="2035-12-24T12:00",
                reason="holiday",
            ),
        ),
        "generate": lambda: generate_availability(
            clinic_id=clinic,
            template_id=template,
            start_date=date(2035, 6, 9),
            end_date=date(2035, 6, 9),
        ),
        "retire": lambda: retire_definition(
            clinic_id=clinic, kind="resource", record_id=spare.pk
        ),
    }
    with runtime_role(), tenant_context(actor, setup.organization_id):
        for name, call in calls.items():
            assert _allowed(call) is expected, (role, name)
        assert (
            _sql_allowed(lambda: _definition_row(clinic, setup.organization_id))
            is expected
        ), (role, "definition trigger")
        projection = role in permitted(*PROJECTION) or (
            role in permitted(BOOK_OWN, MOVE_OWN) and role in PROFESSIONAL_ROLES
        )
        assert bool(service_practitioners(clinic)) is projection, (role, "projection")


@pytest.mark.parametrize("role", ROLES)
def test_generation_command_follows_the_role_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="Sintetico matrix job", kind="room"),
        )
        template = _template(setup, room.pk)
    actor, _ = permission_actor(rbac_graph, role)
    arguments = {
        "--user-id": actor,
        "--organization-id": setup.organization_id,
        "--clinic-id": setup.clinic_id,
        "--template-id": template,
        "--start-date": "2035-06-09",
        "--end-date": "2035-06-09",
    }
    output = io.StringIO()
    refusal = ""
    with runtime_role():
        try:
            call_command(
                "generate_scheduling_availability",
                *[str(value) for pair in arguments.items() for value in pair],
                stdout=output,
            )
        except CommandError as error:
            refusal = str(error)
    allowed = not refusal
    assert (output.getvalue(), refusal) == (
        ("generated_blocks=1\n", "")
        if allowed
        else ("", "Scheduling generation refused.")
    )
    assert allowed is (role in permitted(*CONFIGURE)), role


def _own_booking(setup: AppointmentSetup, actor: UUID, role: str) -> UUID:
    """Book the actor's own slot through a service naming the actor's role."""
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=actor,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
        room, equipment, _ = _catalog(setup)
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico own service",
                duration_min=30,
                required_professional_roles=(role,),
                required_resource_kinds=("room", "equipment"),
            ),
        )
    with runtime_role(), tenant_context(actor, setup.organization_id):
        # 11:00 keeps the shared patient clear of the 09:00 booking.
        return book(
            replace(setup, practitioner_id=actor),
            (room, equipment, service),
            start="11:00",
        ).pk


@pytest.mark.parametrize("role", ROLES)
def test_booking_and_transitions_follow_the_role_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        booked = book(setup, catalog, start="09:00")
    actor, _ = permission_actor(rbac_graph, role)
    clinic = setup.clinic_id
    service_id = catalog[2].pk
    with runtime_role(), tenant_context(actor, setup.organization_id):
        # The guards themselves: PostgreSQL would mask a removed Python check.
        guards = {
            "book other": lambda: authorized_service_clinic(
                clinic, setup.practitioner_id
            ),
            "book own": lambda: authorized_service_clinic(clinic, actor),
            "move other": lambda: authorized_transition_clinic(booked),
            "move own": lambda: authorized_transition_clinic(
                Appointment(
                    clinic_id=clinic, service_type_id=service_id, practitioner_id=actor
                )
            ),
        }
        expected = {
            "book other": role in permitted(BOOK),
            "book own": role in permitted(BOOK, BOOK_OWN),
            "move other": role in permitted(MOVE),
            "move own": role in permitted(MOVE, MOVE_OWN),
        }
        assert {name: _allowed(call) for name, call in guards.items()} == expected, role
        # Another clinician's slot: only clinic-wide permissions apply.
        assert _allowed(lambda: book(setup, catalog, start="10:00")) is (
            role in permitted(BOOK)
        ), (role, "book")
        moves = role in permitted(MOVE)
        assert (
            _allowed(
                lambda: reschedule_appointment(
                    appointment_id=booked.pk,
                    local_range=AppointmentLocalRange(
                        "2035-06-02T11:00", "2035-06-02T11:30"
                    ),
                )
            )
            is moves
        ), (role, "move")
        assert (
            _allowed(
                lambda: cancel_appointment(
                    appointment_id=booked.pk, reason="clinic_request"
                )
            )
            is moves
        ), (role, "cancel")
    if role in permitted(BOOK_OWN):
        own = _own_booking(setup, actor, role)
        with runtime_role(), tenant_context(actor, setup.organization_id):
            assert _allowed(
                lambda: reschedule_appointment(
                    appointment_id=own,
                    local_range=AppointmentLocalRange(
                        "2035-06-02T11:30", "2035-06-02T12:00"
                    ),
                )
            ) is (role in permitted(MOVE, MOVE_OWN)), (role, "own move")


@pytest.mark.parametrize("role", sorted(PROFESSIONAL_ROLES))
def test_own_service_rows_follow_the_role_bundle_in_postgresql(
    rbac_graph: RbacGraph, role: str
) -> None:
    """The own-scope trigger terms, where the actor is the booked professional."""
    setup = seed_appointment_setup(rbac_graph)
    actor, _ = permission_actor(rbac_graph, role)
    day = date(2035, 6, 2)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, _ = _catalog(setup)
        schedule = create_template(
            clinic_id=setup.clinic_id,
            content=TemplateInput(
                practitioner_id=actor,
                weekdays=(day.weekday(),),
                start_local=time(8),
                end_local=time(12),
                valid_from=day,
                valid_to=day,
            ),
        )
        generate_availability(
            clinic_id=setup.clinic_id,
            template_id=schedule.pk,
            start_date=day,
            end_date=day,
        )
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico own rows",
                duration_min=30,
                required_professional_roles=(role,),
                required_resource_kinds=("room", "equipment"),
            ),
        )
        booked = create_service_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=actor,
            booking=ServiceBooking(
                AppointmentLocalRange("2035-06-02T09:00", "2035-06-02T09:30"),
                service.pk,
                (room.pk, equipment.pk),
            ),
            idempotency_key=uuid4(),
        )
    shift = timedelta(minutes=90)
    copy = {
        field.attname: getattr(booked, field.attname)
        for field in Appointment._meta.concrete_fields
    } | {
        "id": uuid4(),
        "idempotency_key": uuid4(),
        "start_at": booked.start_at + shift,
        "end_at": booked.end_at + shift,
    }
    with runtime_role(), tenant_context(actor, setup.organization_id):
        assert _sql_allowed(
            lambda: Appointment.objects.filter(pk=booked.pk).update(
                updated_at=booked.updated_at
            )
        ) is (role in permitted(MOVE, MOVE_OWN)), (role, "own update")
        assert _sql_allowed(lambda: Appointment.objects.create(**copy)) is (
            role in permitted(BOOK, BOOK_OWN)
        ), (role, "own insert")


def _verified_client(user: User) -> Client:
    create_totp_device(user.pk, confirmed=True)
    client = Client()
    with runtime_role(), fixed_otp_time():
        login(client, user.username, password=RBAC_RAW_CREDENTIAL)
        device = get_totp_device(user.pk, confirmed=True)
        client.post(
            "/auth/verify/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
                "next": "/auth/protected/",
            },
        )
    return client


def _settings(client: Client, clinic: UUID) -> _MonkeyPatchedWSGIResponse:
    with runtime_role():
        return client.get(f"/scheduling/clinics/{clinic}/settings/")


@pytest.mark.parametrize("role", ROLES)
def test_settings_route_follows_the_role_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    user = create_role_actor(rbac_graph, UserClinicRole.Role(role))
    client = _verified_client(user)
    unknown = _settings(client, uuid4())
    assert unknown.status_code == 403
    response = _settings(client, rbac_graph.clinic_a)
    if role in permitted(*CONFIGURE):
        assert response.status_code == 200, role
    else:
        # Refusal is byte-identical to the unknown-clinic refusal, no cookie.
        assert (response.status_code, response.content) == (
            unknown.status_code,
            unknown.content,
        ), role
        assert not response.cookies, role


def test_inactive_actor_is_refused_by_every_boundary(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        booked = book(setup, catalog, start="09:00")
    configure = sorted(permitted(*CONFIGURE) & permitted(BOOK, MOVE))[0]
    actor, _ = permission_actor(rbac_graph, configure)
    clinic = setup.clinic_id
    boundaries: dict[str, Callable[[], object]] = {
        "configure": lambda: create_resource(
            clinic_id=clinic,
            content=ResourceInput(name="Sintetico inactive", kind="location"),
        ),
        "book": lambda: book(setup, catalog, start="10:00"),
        "move": lambda: reschedule_appointment(
            appointment_id=booked.pk,
            local_range=AppointmentLocalRange("2035-06-02T11:00", "2035-06-02T11:30"),
        ),
    }

    def decisions() -> dict[str, bool]:
        # Both GUCs bound: only the actor's is_active differs between passes,
        # so a refusal cannot come from a missing tenant (R8-B1).
        with permission_context(rbac_graph, actor):
            result = {name: _in_savepoint(call) for name, call in boundaries.items()}
            result["definition trigger"] = _sql_allowed(
                lambda: _definition_row(clinic, setup.organization_id)
            )
            result["projection"] = bool(service_practitioners(clinic))
        return result

    active = decisions()
    assert set(active.values()) == {True}, active
    User.objects.filter(pk=actor).update(is_active=False)
    try:
        assert set(decisions().values()) == {False}
        with (
            runtime_role(),
            pytest.raises(TenantAccessDeniedError),
            tenant_context(actor, setup.organization_id),
        ):
            pass
    finally:
        User.objects.filter(pk=actor).update(is_active=True)


def _in_savepoint(call: Callable[[], object]) -> bool:
    with transaction.atomic():
        allowed = _allowed(call)
        transaction.set_rollback(True)
    return allowed


def test_effective_interval_is_actor_invariant(rbac_graph: RbacGraph) -> None:
    """Executed backing for the row_integrity label: no actor input changes it."""
    query = (
        "SELECT clinic_app.scheduling_effective_interval("
        "'2035-06-02T09:00Z'::timestamptz,'2035-06-02T09:30Z'::timestamptz,10,5)::text"
    )
    actors = [permission_actor(rbac_graph, role)[0] for role in ROLES]
    # No actor, every stored role, then the last actor deactivated.
    states: list[tuple[UUID | None, bool]] = [
        (None, True),
        *((actor, True) for actor in actors),
        (actors[-1], False),
    ]
    results = set()
    for actor, active in states:
        if not active:
            User.objects.filter(pk=actors[-1]).update(is_active=False)
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true), "
                "set_config('app.current_user_id', %s, true)",
                [
                    "" if actor is None else str(rbac_graph.organization_a),
                    "" if actor is None else str(actor),
                ],
            )
            cursor.execute(query)
            results.add(cursor.fetchone())
    assert results == {('["2035-06-02 08:50:00+00","2035-06-02 09:35:00+00")',)}
