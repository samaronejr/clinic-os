"""Appointment lifecycle v2 (todo 22): every transition x every actor, both layers.

The state machine and its actor column are pinned here from the plan's SM table
(appointment row) and RP agenda row, never read from the guard code. The
Python services and the database lifecycle trigger are each exercised on their
own: the service matrix runs every action from every stored state for every
catalog role plus the booked physician, another physician, the booked
patient's session, another patient's session, an inactive and a foreign-clinic
actor and the machine (W) context; the SQL matrix issues raw UPDATEs as
``clinic_app`` for every (source, target) status pair. Refusals are compared
for identity and checked for side effects. Time-dependent rules (hold expiry at
equality, the no-show deadline) run under pinned DB clocks.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

import pytest
from apps.ehr.services import ClinicalConflictError, open_encounter
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.identity.scope_provisioning import narrow_role
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling import appointment_lifecycle, services
from apps.scheduling.appointment_lifecycle import authorize_transition
from apps.scheduling.models import Appointment, AppointmentTransition
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentIdempotencyConflictError,
    AppointmentLifecycleError,
    AppointmentLocalRange,
)
from apps.scheduling.tasks import expire_holds
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scheduling.lifecycle_races import race_expiry_and_booking, race_two_holds
from scheduling.lifecycle_world import (
    STATES,
    Actor,
    LifecycleWorld,
    acting,
    build_world,
    pinned_clock,
)
from scheduling.test_resource_permission_identity import holders, siblings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

ROLES = tuple(UserClinicRole.Role.values)
PROFESSIONAL_ROLES = frozenset({"physician", "nurse", "allied_professional"})
NARROWING_AUTHORITY = "staff.organization"

# --- Pinned specification (plan SM appointment row + RP agenda row) ---------
LEGAL: dict[str, tuple[frozenset[str], str]] = {
    "hold": (frozenset({"requested"}), "held"),
    "book": (frozenset({"requested", "held"}), "scheduled"),
    "arrive": (frozenset({"scheduled"}), "arrived"),
    "start": (frozenset({"arrived"}), "in_progress"),
    "complete": (frozenset({"in_progress"}), "completed"),
    "cancel": (frozenset({"requested", "held", "scheduled", "arrived"}), "cancelled"),
    "mark_no_show": (frozenset({"scheduled"}), "no_show"),
}
# (permission, own-schedule only)
BOOK = (("appointment.book", False), ("appointment.book_own", True))
MOVE = (("appointment.move", False), ("appointment.move_own", True))
CARE = (("appointment.move_own", True),)
STAFF: dict[str, tuple[tuple[str, bool], ...]] = {
    "hold": BOOK,
    "book": BOOK,
    "arrive": MOVE,
    "start": CARE,
    "complete": CARE,
    "cancel": MOVE,
    "mark_no_show": MOVE,
}
PATIENT: dict[str, frozenset[str]] = {
    "book": frozenset({"held"}),
    "cancel": frozenset({"requested", "held", "scheduled"}),
}
# Database edges and their actor classes; "legacy" keeps pre-v2 authority.
EDGES: dict[tuple[str, str], str] = {
    ("requested", "held"): "book",
    ("requested", "scheduled"): "book",
    ("held", "scheduled"): "book",
    ("requested", "cancelled"): "move",
    ("held", "cancelled"): "move",
    ("scheduled", "arrived"): "move",
    ("scheduled", "no_show"): "move",
    ("arrived", "cancelled"): "move",
    ("arrived", "in_progress"): "care",
    ("in_progress", "completed"): "care",
    ("held", "expired"): "machine",
    ("scheduled", "cancelled"): "legacy",
}
PATIENT_EDGES = frozenset(
    {("held", "scheduled"), ("held", "cancelled"), ("requested", "cancelled")}
)
CLASS_TERMS = {"book": BOOK, "move": MOVE, "care": CARE}


@pytest.fixture
def world(rbac_graph: RbacGraph, superuser_database_url: str) -> LifecycleWorld:
    return build_world(rbac_graph, superuser_database_url, _reference())


def _reference() -> datetime:
    return datetime(2035, 6, 1, tzinfo=UTC)


def _staff_allowed(terms: Sequence[tuple[str, bool]], actor: Actor) -> bool:
    if actor.kind != "staff" or actor.role is None:
        return False
    return any(
        permission in BUNDLES_V1[actor.role] and (actor.own or not own_only)
        for permission, own_only in terms
    )


def expected_service(action: str, state: str, actor: Actor) -> str:
    sources, _ = LEGAL[action]
    if actor.kind == "patient":
        allowed = actor.own_patient and state in PATIENT.get(action, frozenset())
    else:
        allowed = _staff_allowed(STAFF[action], actor)
    if not allowed:
        return "denied"
    return "ok" if state in sources else "illegal_transition"


def asked(terms: Sequence[tuple[str, bool]], actor: Actor) -> list[str]:
    """Permission names the Python guard sends to has_permission, in order."""
    if actor.kind == "patient":
        return []
    names = []
    for permission, _ in terms:
        names.append(permission)
        if (
            actor.kind == "staff"
            and actor.role is not None
            and permission in BUNDLES_V1[actor.role]
        ):
            break
    return names


@contextmanager
def checked_permissions() -> Iterator[list[str]]:
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


class Snapshot(NamedTuple):
    transitions: int
    audit: int
    patient_receipts: int
    status: str
    revision: int
    updated_at: object


@contextmanager
def checked_statements() -> Iterator[list[str]]:
    sent: list[str] = []

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        sent.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        yield sent


def _sql_expire(row: Appointment, *, stale: bool = False) -> str:
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.scheduling_expire_hold(%s, %s, %s, %s)",
                [row.clinic_id, row.pk, row.revision + (99 if stale else 0), uuid4()],
            )
    except DatabaseError as error:
        if getattr(error.__cause__, "sqlstate", None) == "42501":
            return "denied"
        raise
    return "ok"


def _owner_snapshot(world: LifecycleWorld, row: Appointment) -> Snapshot:
    """Read side effects as the owner inside the same (to be rolled back) txn."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_tenant', true)")
        prior = cursor.fetchone()
        cursor.execute("RESET ROLE")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(world.graph.organization_a)],
        )
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.scheduling_appointmenttransition),"
            " (SELECT count(*) FROM clinic_app.audit_event_tenant),"
            " (SELECT count(*) FROM clinic_app.scheduling_patientbookingevent),"
            " a.status, a.revision, a.updated_at"
            " FROM clinic_app.scheduling_appointment a WHERE a.id=%s",
            [row.pk],
        )
        snapshot = cursor.fetchone()
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [(prior or ("",))[0] or ""],
        )
        cursor.execute("SET LOCAL ROLE clinic_app")
    assert snapshot is not None
    return Snapshot(*snapshot)


def _fresh(world: LifecycleWorld, row: Appointment) -> None:
    with owner_context(world.graph.organization_a):
        row.refresh_from_db()


def _call(action: str, world: LifecycleWorld, row: Appointment) -> object:
    function = getattr(services, action)
    return function(
        clinic_id=world.setup.clinic_id,
        appointment_id=row.pk,
        expected_revision=row.revision,
        command_id=uuid4(),
    )


def _outcome(call: Callable[[], object]) -> tuple[str, object]:
    try:
        with transaction.atomic():
            result = call()
    except AppointmentAccessDeniedError as error:
        return "denied", str(error)
    except AppointmentLifecycleError as error:
        return error.code, str(error)
    return "ok", result


def _run_service_matrix(
    world: LifecycleWorld, actions: Sequence[str], at: datetime | None
) -> int:
    database_url = _database_url()
    checked = 0
    context = pinned_clock(database_url, at) if at is not None else _nothing()
    with context:
        for action in actions:
            for state in STATES:
                row = world.rows[state]
                _fresh(world, row)
                for name, actor in world.actors.items():
                    expected = expected_service(action, state, actor)
                    with acting(world, actor), checked_permissions() as names:
                        before = _owner_snapshot(world, row)
                        outcome, detail = _outcome(partial(_call, action, world, row))
                        after = _owner_snapshot(world, row)
                    assert outcome == expected, (action, state, name, outcome)
                    if outcome == "ok":
                        assert isinstance(detail, Appointment)
                        assert detail.status == LEGAL[action][1]
                        assert detail.revision == row.revision + 1
                        assert after.transitions == before.transitions + 1
                        assert (after.status, after.revision) == (
                            LEGAL[action][1],
                            row.revision + 1,
                        )
                    else:
                        # Refusals write nothing: no receipt, audit, patient
                        # receipt, row change or partial update.
                        assert after == before, (action, state, name)
                    if actor.kind in {"staff", "inactive", "foreign"}:
                        assert names == asked(STAFF[action], actor)[: len(names)]
                        if outcome in {"ok", "illegal_transition"}:
                            assert names == asked(STAFF[action], actor)
                    else:
                        assert names == [], (action, state, name, names)
                    checked += 1
    return checked


@contextmanager
def _nothing() -> Iterator[None]:
    yield


_DATABASE_URL: list[str] = []


def _database_url() -> str:
    return _DATABASE_URL[0]


@pytest.fixture(autouse=True)
def _remember_database(superuser_database_url: str) -> None:
    _DATABASE_URL[:] = [superuser_database_url]


def test_pinned_state_machine_is_complete_and_distinguishes_actors() -> None:
    assert set(BUNDLES_V1) == set(ROLES)
    assert set(STATES) == set(Appointment.Status.values)
    for action, (sources, target) in LEGAL.items():
        assert (sources | {target}) <= set(STATES)
        assert all((source, target) in EDGES for source in sources), action
        assert PATIENT.get(action, frozenset()) <= sources
        for permission, _ in STAFF[action]:
            assert holders(permission), (action, permission)
        # Every action both grants and refuses some catalog role.
        grantees = {
            role
            for role in ROLES
            if _staff_allowed(STAFF[action], Actor("staff", role, own=True))
        }
        assert grantees
        assert grantees != set(ROLES)
    assert set(EDGES.values()) == {"book", "move", "care", "machine", "legacy"}


def test_every_transition_by_every_actor_through_the_services(
    world: LifecycleWorld,
) -> None:
    before_start = world.rows["scheduled"].start_at
    checked = _run_service_matrix(
        world, [a for a in LEGAL if a != "mark_no_show"], None
    )
    # The no-show deadline is the booked start: legal at exact equality.
    checked += _run_service_matrix(world, ["mark_no_show"], before_start)
    assert checked == len(LEGAL) * len(STATES) * len(world.actors)
    # One tick before the start the booked row cannot be marked absent yet.
    tick = before_start - timedelta(microseconds=1)
    with (
        pinned_clock(_database_url(), tick),
        acting(world, world.actors["receptionist"]),
    ):
        outcome, _ = _outcome(
            lambda: _call("mark_no_show", world, world.rows["scheduled"])
        )
    assert outcome == "illegal_transition"


def test_python_guard_decides_each_action_by_its_pinned_terms(
    world: LifecycleWorld,
) -> None:
    """authorize_transition alone: a mutant ignoring its result cannot pass."""
    for action in LEGAL:
        for state in STATES:
            row = world.rows[state]
            _fresh(world, row)
            for name, actor in world.actors.items():
                if actor.kind == "patient" and name == "other_patient":
                    continue  # RLS hides the row before any guard runs.
                expected = expected_service(action, state, actor)
                with acting(world, actor):
                    try:
                        kind = authorize_transition(action, row, state)
                    except AppointmentAccessDeniedError:
                        kind = "denied"
                assert (kind == "denied") == (expected == "denied"), (
                    action,
                    state,
                    name,
                )


def _sql_update(world: LifecycleWorld, row: Appointment, target: str) -> str:
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.scheduling_appointment SET status=%s, "
                "cancellation_reason=CASE WHEN %s='cancelled' THEN CASE WHEN "
                "NULLIF(current_setting('app.current_patient_session',true),'') "
                "IS NULL THEN 'clinic_request' ELSE 'patient_request' END END, "
                "cancelled_at=CASE WHEN %s='cancelled' THEN statement_timestamp() END "
                "WHERE id=%s",
                [target, target, target, row.pk],
            )
            if cursor.rowcount == 0:
                return "invisible"
    except DatabaseError as error:
        cause = error.__cause__
        state = getattr(cause, "sqlstate", None)
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
        if state == "42501":
            return "denied"
        return f"{state}:{constraint}"
    return "ok"


# (source, target, late clock) -> outcome for an authorized actor.
TIME_RULES: dict[tuple[str, str, bool], str] = {
    ("held", "scheduled", True): "23514:scheduling_appointment_hold_expired",
    ("held", "expired", False): "23514:scheduling_appointment_transition_check",
    ("scheduled", "no_show", False): "23514:scheduling_appointment_no_show_deadline",
}


def _sql_allowed(edge: str, pair: tuple[str, str], actor: Actor) -> bool:
    if actor.kind == "patient" and pair in PATIENT_EDGES:
        return True
    if edge == "legacy":
        # Pre-v2 authority for booked -> cancelled is unchanged (todo 6 parity):
        # the legacy service and the patient guard decide; RLS scopes the row.
        return True
    if edge == "machine":
        return actor.kind == "machine"
    return _staff_allowed(CLASS_TERMS[edge], actor)


def expected_sql(source: str, target: str, actor: Actor, *, late: bool) -> str:
    if actor.kind == "patient" and not actor.own_patient:
        return "invisible"
    edge = EDGES.get((source, target))
    if source == "cancelled":
        return "23514:scheduling_appointment_terminal_check"
    if edge is None:
        return "23514:scheduling_appointment_transition_check"
    if not _sql_allowed(edge, (source, target), actor):
        return "denied"
    return TIME_RULES.get((source, target, late), "ok")


def test_database_trigger_decides_every_status_pair_by_actor(
    world: LifecycleWorld,
) -> None:
    """Raw UPDATEs through real RLS as clinic_app; the Python layer is absent."""
    late = world.rows["no_show"].start_at + timedelta(hours=1)
    checked = 0
    for clock, is_late in ((None, False), (late, True)):
        context = pinned_clock(_database_url(), clock) if clock else _nothing()
        with context:
            for source in STATES:
                row = world.rows[source]
                for target in STATES:
                    if target == source:
                        continue
                    for name, actor in world.actors.items():
                        with acting(world, actor):
                            observed = _sql_update(world, row, target)
                        expected = expected_sql(source, target, actor, late=is_late)
                        assert observed == expected, (source, target, name, clock)
                        checked += 1
    assert checked == 2 * len(STATES) * (len(STATES) - 1) * len(world.actors)


def test_physician_on_another_practitioners_appointment_gets_42501(
    world: LifecycleWorld,
) -> None:
    other = world.actors["other_physician"]
    own = world.actors["own_physician"]
    for source, target in (("scheduled", "arrived"), ("arrived", "in_progress")):
        row = world.rows[source]
        with acting(world, other):
            assert _sql_update(world, row, target) == "denied"
        with acting(world, own):
            assert _sql_update(world, row, target) == "ok"


@contextmanager
def _narrowed(
    world: LifecycleWorld, role: str, removed: frozenset[str], actor: UUID
) -> Iterator[None]:
    graph = world.graph
    narrower = next(
        world.actors[r].user_id
        for r in sorted(holders(NARROWING_AUTHORITY))
        if r != role
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(graph.organization_a)],
            )
            if removed:
                cursor.execute("SET LOCAL ROLE clinic_owner")
                cursor.execute(
                    "SELECT statement_timestamp() - interval '1 second' "
                    "FROM set_config('app.current_user_id', %s, true)",
                    [str(narrower)],
                )
                row = cursor.fetchone()
                assert row is not None
        for permission in sorted(removed):
            narrow_role(
                clinic_id=graph.clinic_a,
                role=role,
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


SAMPLE_EDGE = {
    "hold": ("requested", "held"),
    "book": ("held", "scheduled"),
    "arrive": ("scheduled", "arrived"),
    "start": ("arrived", "in_progress"),
    "complete": ("in_progress", "completed"),
    "cancel": ("scheduled", "cancelled"),
    "mark_no_show": ("scheduled", "no_show"),
}


def test_guards_decide_by_the_exact_permission_not_a_sibling(
    world: LifecycleWorld,
) -> None:
    """Remove exactly one pinned permission (or all its same-role-set siblings).

    Removing the checked permission refuses unless another pinned term still
    grants; removing only siblings changes nothing. Python and SQL both.
    """
    for action, terms in STAFF.items():
        source, target = SAMPLE_EDGE[action]
        for permission, own_only in terms:
            candidates = holders(permission)
            if own_only:
                candidates &= PROFESSIONAL_ROLES
            role = sorted(candidates)[0]
            own = own_only or role == "physician"
            actor_id = world.graph.physician if own else world.actors[role].user_id
            assert actor_id is not None
            actor = Actor("staff", role, actor_id, own=own)
            for removed in (
                frozenset(),
                frozenset({permission}),
                siblings(permission, role),
            ):
                narrowed_actor = _still_grants(terms, actor, removed)
                row = world.rows[source]
                with _narrowed(world, role, removed, actor_id):
                    python = _outcome(
                        partial(authorize_transition, action, row, row.status)
                    )[0]
                assert (python == "ok") == narrowed_actor, (action, permission, removed)
                if (source, target) != ("scheduled", "cancelled"):
                    # The no-show deadline is the booked start (DB time).
                    clock = (
                        pinned_clock(_database_url(), row.start_at)
                        if action == "mark_no_show"
                        else _nothing()
                    )
                    with clock, _narrowed(world, role, removed, actor_id):
                        sql = _sql_update(world, row, target)
                    assert (sql == "ok") == narrowed_actor, (
                        action,
                        permission,
                        removed,
                        sql,
                    )


def _still_grants(
    terms: Sequence[tuple[str, bool]], actor: Actor, removed: frozenset[str]
) -> bool:
    assert actor.role is not None
    return any(
        permission in BUNDLES_V1[actor.role]
        and permission not in removed
        and (actor.own or not own_only)
        for permission, own_only in terms
    )


def test_hold_expiry_at_exact_equality_frees_the_slot(world: LifecycleWorld) -> None:
    held = world.rows["held"]
    deadline = held.hold_expires_at
    assert deadline == world.reference + timedelta(minutes=10)
    receptionist = world.actors["receptionist"]
    # One microsecond before the deadline the hold still books.
    tick = deadline - timedelta(microseconds=1)
    with pinned_clock(_database_url(), tick), acting(world, receptionist):
        assert _outcome(lambda: _call("book", world, held))[0] == "ok"
    # At exact equality it is expired: booking it is refused ...
    with pinned_clock(_database_url(), deadline), acting(world, receptionist):
        outcome, _ = _outcome(lambda: _call("book", world, held))
        assert outcome == "hold_expired"
        # ... and the same slot is free for another booking right away.
        other = _other_enrollment(world)
        booked = services.create_appointment(
            clinic_id=world.setup.clinic_id,
            enrollment_id=other,
            practitioner_id=world.setup.practitioner_id,
            local_range=AppointmentLocalRange("2035-06-02T08:30", "2035-06-02T09:00"),
            idempotency_key=uuid4(),
        )
        assert booked.status == "scheduled"
        held.refresh_from_db()
        assert held.status == "expired"
        receipt = AppointmentTransition.objects.get(
            appointment=held, to_status="expired"
        )
        assert receipt.actor_kind == "machine"


def _other_enrollment(world: LifecycleWorld) -> UUID:
    found = (
        PatientClinicEnrollment.objects.filter(clinic_id=world.setup.clinic_id)
        .exclude(pk=world.setup.enrollment_id)
        .order_by("pk")
        .values_list("pk", flat=True)
        .first()
    )
    assert found is not None
    return found


def test_refusals_are_identical_and_leave_no_trace(world: LifecycleWorld) -> None:
    nurse = world.actors["nurse"]
    row = world.rows["scheduled"]
    messages = set()
    with acting(world, nurse):
        before = _owner_snapshot(world, row)
        for appointment_id in (row.pk, uuid4()):
            try:
                services.arrive(
                    clinic_id=world.setup.clinic_id,
                    appointment_id=appointment_id,
                    expected_revision=row.revision,
                    command_id=uuid4(),
                )
            except AppointmentAccessDeniedError as error:
                messages.add((type(error), str(error)))
        # A foreign-clinic selector is refused the same way.
        try:
            services.arrive(
                clinic_id=world.graph.clinic_b,
                appointment_id=row.pk,
                expected_revision=row.revision,
                command_id=uuid4(),
            )
        except AppointmentAccessDeniedError as error:
            messages.add((type(error), str(error)))
        assert _owner_snapshot(world, row) == before
    assert len(messages) == 1


def test_revision_conflict_replay_and_command_reuse(world: LifecycleWorld) -> None:
    row = world.rows["scheduled"]
    receptionist = world.actors["receptionist"]
    command = uuid4()
    with acting(world, receptionist):
        stale = _outcome(
            lambda: services.arrive(
                clinic_id=world.setup.clinic_id,
                appointment_id=row.pk,
                expected_revision=row.revision + 1,
                command_id=command,
            )
        )
        assert stale[0] == "revision_conflict"
        first = services.arrive(
            clinic_id=world.setup.clinic_id,
            appointment_id=row.pk,
            expected_revision=row.revision,
            command_id=command,
        )
        replay = services.arrive(
            clinic_id=world.setup.clinic_id,
            appointment_id=row.pk,
            expected_revision=row.revision,
            command_id=command,
        )
        assert (replay.pk, replay.revision) == (first.pk, first.revision)
        assert AppointmentTransition.objects.filter(command_id=command).count() == 1
        with pytest.raises(AppointmentIdempotencyConflictError):
            services.mark_no_show(
                clinic_id=world.setup.clinic_id,
                appointment_id=world.rows["arrived"].pk,
                expected_revision=world.rows["arrived"].revision,
                command_id=command,
            )


def test_expiry_is_machine_only_and_the_job_expires_due_holds(
    world: LifecycleWorld,
) -> None:
    held = world.rows["held"]
    humans = [name for name in world.actors if name != "machine"]
    assert set(ROLES) <= set(humans)
    with pinned_clock(_database_url(), held.hold_expires_at or world.reference):
        for name in humans:
            with acting(world, world.actors[name]), checked_statements() as sent:
                # The Python layer refuses before any expiry statement is sent ...
                with pytest.raises(AppointmentAccessDeniedError):
                    services.expire(
                        clinic_id=world.setup.clinic_id,
                        appointment_id=held.pk,
                        expected_revision=held.revision,
                        command_id=uuid4(),
                    )
                with pytest.raises(AppointmentAccessDeniedError):
                    services.expire_due_holds()
                assert not any("scheduling_expire_hold" in text for text in sent)
                assert not any("scheduling_due_holds" in text for text in sent)
                # ... and the W resolvers refuse humans on their own.
                assert _sql_expire(held) == "denied", name
                # Refused before any row work: not a revision conflict.
                assert _sql_expire(held, stale=True) == "denied", name
                with connection.cursor() as cursor:
                    cursor.execute("SELECT * FROM clinic_app.scheduling_due_holds(100)")
                    assert cursor.fetchall() == [], name
    with runtime_role():
        # Not yet due: the machine can only do what the clock mandates.
        assert services.expire_due_holds() == ()
        with pytest.raises(AppointmentLifecycleError) as early:
            services.expire(
                clinic_id=world.setup.clinic_id,
                appointment_id=held.pk,
                expected_revision=held.revision,
                command_id=uuid4(),
            )
        assert early.value.code == "illegal_transition"
    assert held.hold_expires_at is not None
    with pinned_clock(_database_url(), held.hold_expires_at), runtime_role():
        assert expire_holds() == 1
        assert services.expire_due_holds() == ()
    _fresh(world, held)
    assert held.status == "expired"


def test_patient_session_cannot_mark_no_show_at_either_layer(
    world: LifecycleWorld,
) -> None:
    patient = world.actors["own_patient"]
    row = world.rows["scheduled"]
    with pinned_clock(_database_url(), row.start_at):
        with acting(world, patient):
            assert _outcome(lambda: _call("mark_no_show", world, row))[0] == "denied"
        with acting(world, patient):
            assert _sql_update(world, row, "no_show") == "denied"


def test_encounters_open_only_after_arrival(world: LifecycleWorld) -> None:
    graph = world.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(ClinicalConflictError, match="precondition_failed"):
            open_encounter(
                clinic_id=graph.clinic_a, appointment_id=world.rows["scheduled"].pk
            )
        for state in ("arrived", "in_progress"):
            encounter = open_encounter(
                clinic_id=graph.clinic_a, appointment_id=world.rows[state].pk
            )
            assert encounter.appointment_id == world.rows[state].pk
    # The database binding guard enforces the same predicate without Python.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        for state, admitted in (("scheduled", False), ("completed", False)):
            row = world.rows[state]
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO clinic_app.ehr_encounter (id,organization_id,"
                        "clinic_id,appointment_id,patient_id,physician_id,state,"
                        "revision,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,"
                        "'open',1,now(),now())",
                        [
                            uuid4(),
                            graph.organization_a,
                            graph.clinic_a,
                            row.pk,
                            row.patient_id,
                            graph.physician,
                        ],
                    )
                observed = True
            except DatabaseError:
                observed = False
            assert observed is admitted, state


def test_requested_only_under_the_approval_policy(world: LifecycleWorld) -> None:
    patient = world.actors["own_patient"]
    receptionist = world.actors["receptionist"]
    requested = world.rows["requested"]
    # Policy on (world default): a patient insert must be a request.
    with acting(world, patient):
        assert _insert_as(world, requested, "scheduled") == (
            "23514:scheduling_appointment_request_policy_check"
        )
        assert _insert_as(world, requested, "requested") == "ok"
    # Staff never create requests.
    with acting(world, receptionist):
        assert _insert_as(world, requested, "requested") == "denied"


def _insert_as(world: LifecycleWorld, template: Appointment, status: str) -> str:
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO clinic_app.scheduling_appointment (id,organization_id,"
                "clinic_id,patient_id,practitioner_id,start_at,end_at,"
                "idempotency_key,create_fingerprint,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
                [
                    uuid4(),
                    template.organization_id,
                    template.clinic_id,
                    template.patient_id,
                    template.practitioner_id,
                    template.start_at + timedelta(hours=3),
                    template.end_at + timedelta(hours=3),
                    uuid4(),
                    b"r" * 32,
                    status,
                ],
            )
    except DatabaseError as error:
        cause = error.__cause__
        if getattr(cause, "sqlstate", None) == "42501":
            return "denied"
        return f"{cause.sqlstate}:{cause.diag.constraint_name}"  # type: ignore[union-attr]
    return "ok"


def test_hold_slot_race_yields_exactly_one_occupant(world: LifecycleWorld) -> None:
    """Two holds for the same free slot race on the widened exclusion."""
    winners, losers = race_two_holds(world)
    assert (winners, losers) == (1, 1)


def test_expiry_and_rebooking_race_writes_one_expiry(world: LifecycleWorld) -> None:
    expired_receipts, booked = race_expiry_and_booking(world, _database_url())
    assert (expired_receipts, booked) == (1, 1)


def test_lifecycle_services_read_no_python_clock() -> None:
    # Hold deadlines, expiry and the no-show deadline are DB time only.
    assert not hasattr(appointment_lifecycle, "timezone")
    assert not hasattr(appointment_lifecycle, "datetime")


def test_hold_is_due_exactly_at_its_deadline(rbac_graph: RbacGraph) -> None:
    """The single equality rule, without any world: due iff deadline <= DB time."""
    instant = datetime(2035, 6, 1, 0, 10, tzinfo=UTC)
    tick = timedelta(microseconds=1)
    with pinned_clock(_database_url(), instant), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.scheduling_hold_due(%s), "
            "clinic_app.scheduling_hold_due(%s), clinic_app.scheduling_hold_due(%s)",
            [instant - tick, instant, instant + tick],
        )
        assert cursor.fetchone() == (True, True, False)
