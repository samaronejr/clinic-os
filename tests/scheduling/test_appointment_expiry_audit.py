"""Gate r1 (SC-4): every W hold expiry appends the registered audit event."""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.audit.events import PHASE1_AUDIT_EVENTS
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.identity.current_context import current_actor_id
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling import services
from apps.scheduling.tasks import expire_holds
from apps.tenancy.db import tenant_context
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scheduling.lifecycle_world import acting, build_world, pinned_clock

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scheduling.models import Appointment

    from rbac_fixtures import RbacGraph
    from scheduling.lifecycle_world import LifecycleWorld

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

EVENT = "scheduling.appointment.expired"
LIFECYCLE = ("scheduling", "0007_appointment_lifecycle_v2")
EXPIRY_AUDIT = ("scheduling", "0008_expiry_audit")
EXPIRE_HOLD = "clinic_app.scheduling_expire_hold(uuid,uuid,integer,uuid)"
TIMEOUT = 10


@pytest.fixture
def expiry_world(rbac_graph: RbacGraph, superuser_database_url: str) -> LifecycleWorld:
    return build_world(
        rbac_graph, superuser_database_url, datetime(2035, 6, 1, tzinfo=UTC)
    )


def _events(appointment: Appointment) -> tuple[AuditEvent, ...]:
    # Owner-only inspection of the tenant chain inside a rolled-back savepoint:
    # runtime callers hold no audit grant of their own.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(appointment.organization_id)],
        )
        events = tuple(
            AuditEvent.objects.filter(
                event_type=EVENT, affected_record_id=str(appointment.pk)
            )
        )
        assert verify_chain(appointment.organization_id).valid
        transaction.set_rollback(True)
    return events


def _assert_event(appointment: Appointment) -> None:
    events = _events(appointment)
    assert len(events) == 1
    event = events[0]
    definition = PHASE1_AUDIT_EVENTS[EVENT]
    assert definition.chain == "tenant"
    assert event.organization_id == appointment.organization_id
    assert event.actor_user_id is None
    assert event.component_id == definition.component_id
    assert event.component_ip is None
    assert event.affected_record_type == definition.affected_record_type
    assert event.payload == {
        "clinic_id": str(appointment.clinic_id),
        "object_verb": definition.object_verb,
    }


def _other_enrollment(world: LifecycleWorld) -> UUID:
    with owner_context(world.graph.organization_a):
        found = (
            PatientClinicEnrollment.objects.filter(clinic_id=world.setup.clinic_id)
            .exclude(pk=world.setup.enrollment_id)
            .order_by("pk")
            .values_list("pk", flat=True)
            .first()
        )
    assert found is not None
    return found


def test_explicit_expiry_audits_once_even_when_command_replayed(
    expiry_world: LifecycleWorld, superuser_database_url: str
) -> None:
    held = expiry_world.rows["held"]
    assert held.hold_expires_at is not None
    command = uuid4()
    with pinned_clock(superuser_database_url, held.hold_expires_at), runtime_role():
        first = services.expire(
            clinic_id=held.clinic_id,
            appointment_id=held.pk,
            expected_revision=held.revision,
            command_id=command,
        )
        replay = services.expire(
            clinic_id=held.clinic_id,
            appointment_id=held.pk,
            expected_revision=held.revision,
            command_id=command,
        )
        assert replay == first
        _assert_event(held)


def test_expiry_job_audits_once_and_has_no_repeat_work(
    expiry_world: LifecycleWorld, superuser_database_url: str
) -> None:
    held = expiry_world.rows["held"]
    assert held.hold_expires_at is not None
    with pinned_clock(superuser_database_url, held.hold_expires_at), runtime_role():
        assert expire_holds() == 1
        assert expire_holds() == 0
        _assert_event(held)


def test_lazy_expiry_audits_and_restores_the_booking_actor(
    expiry_world: LifecycleWorld, superuser_database_url: str
) -> None:
    held = expiry_world.rows["held"]
    assert held.hold_expires_at is not None
    enrollment_id = _other_enrollment(expiry_world)
    receptionist = expiry_world.actors["receptionist"]
    with (
        pinned_clock(superuser_database_url, held.hold_expires_at),
        acting(expiry_world, receptionist),
    ):
        booked = services.create_appointment(
            clinic_id=held.clinic_id,
            enrollment_id=enrollment_id,
            practitioner_id=held.practitioner_id,
            local_range=services.AppointmentLocalRange(
                "2035-06-02T08:30", "2035-06-02T09:00"
            ),
            idempotency_key=uuid4(),
        )
        held.refresh_from_db()
        assert held.status == "expired"
        assert booked.status == "scheduled"
        assert current_actor_id() == receptionist.user_id
        _assert_event(held)


def test_expiry_and_audit_roll_back_together(
    expiry_world: LifecycleWorld, superuser_database_url: str
) -> None:
    held = expiry_world.rows["held"]
    assert held.hold_expires_at is not None
    with pinned_clock(superuser_database_url, held.hold_expires_at), runtime_role():
        with transaction.atomic():
            services.expire(
                clinic_id=held.clinic_id,
                appointment_id=held.pk,
                expected_revision=held.revision,
                command_id=uuid4(),
            )
            _assert_event(held)
            transaction.set_rollback(True)
        assert _events(held) == ()
    with owner_context(expiry_world.graph.organization_a):
        held.refresh_from_db()
    assert held.status == "held"


def _await_lock_wait(backend_pid: int, finished: Event) -> None:
    """Bounded wait for the exact pg_locks state; never a timer sleep."""
    deadline = monotonic() + TIMEOUT
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                "AND locktype = 'advisory' AND NOT granted)",
                [backend_pid],
            )
            if cursor.fetchone() == (True,):
                return
            if finished.is_set():
                pytest.fail("the expiry finished without waiting behind any gate")
        cursor.execute(
            "SELECT locktype, mode, relation::regclass::text FROM pg_locks "
            "WHERE pid = %s AND NOT granted",
            [backend_pid],
        )
        waits = cursor.fetchall()
    pytest.fail(f"the expiry never waited behind an advisory lock: {waits}")


def _machine_expiry(
    held: Appointment, backend_pids: Queue[int], finished: Event
) -> object:
    close_old_connections()
    try:
        with runtime_role():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                row = cursor.fetchone()
            assert row is not None
            backend_pids.put(int(row[0]))
            return services.expire(
                clinic_id=held.clinic_id,
                appointment_id=held.pk,
                expected_revision=held.revision,
                command_id=uuid4(),
            )
    except Exception as error:  # noqa: BLE001 - the outcome is asserted below
        return error
    finally:
        finished.set()
        connections.close_all()


def test_expiry_append_queues_on_the_ledger_writers_audit_lock(
    expiry_world: LifecycleWorld, superuser_database_url: str
) -> None:
    """The trigger's key is the one every ledger writer spells with COLLATE "C"."""
    held = expiry_world.rows["held"]
    assert held.hold_expires_at is not None
    organization = str(held.organization_id)
    backend_pids: Queue[int] = Queue()
    finished = Event()
    with (
        pinned_clock(superuser_database_url, held.hold_expires_at),
        ThreadPoolExecutor(max_workers=1) as pool,
        psycopg.connect(superuser_database_url) as holder,
    ):
        holder.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended("
            "('clinic-audit:' || %s::pg_catalog.text) COLLATE pg_catalog.\"C\", 0))",
            [organization],
        )
        outcome = pool.submit(_machine_expiry, held, backend_pids, finished)
        backend_pid = backend_pids.get(timeout=TIMEOUT)
        _await_lock_wait(backend_pid, finished)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT (classid::pg_catalog.int8 << 32) | objid::pg_catalog.int8 "
                "= pg_catalog.hashtextextended(('clinic-audit:' || "
                '%s::pg_catalog.text) COLLATE pg_catalog."C", 0) '
                "FROM pg_catalog.pg_locks WHERE pid = %s "
                "AND locktype = 'advisory' AND NOT granted",
                [organization, backend_pid],
            )
            assert cursor.fetchall() == [(True,)]
        holder.rollback()
        expired = outcome.result(timeout=TIMEOUT)
    assert not isinstance(expired, Exception), expired
    _assert_event(held)


def test_machine_expiry_waits_behind_the_appointment_gates(
    expiry_world: LifecycleWorld, superuser_database_url: str
) -> None:
    """A writer holding the gates and the audit lock lazily expires the hold.

    The machine expiry must queue behind the clinic gate before it locks the
    row; locking the row first and then waiting for the audit lock deadlocks
    against this writer's lazy expiry of the same row.
    """
    held = expiry_world.rows["held"]
    assert held.hold_expires_at is not None
    enrollment_id = _other_enrollment(expiry_world)
    setup = expiry_world.setup
    backend_pids: Queue[int] = Queue()
    finished = Event()
    with (
        pinned_clock(superuser_database_url, held.hold_expires_at),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            # Gates for this clinic, practitioner and patient, then the audit lock.
            services.create_appointment(
                clinic_id=setup.clinic_id,
                enrollment_id=enrollment_id,
                practitioner_id=setup.practitioner_id,
                local_range=services.AppointmentLocalRange(
                    "2035-06-02T11:00", "2035-06-02T11:30"
                ),
                idempotency_key=uuid4(),
            )
            outcome = pool.submit(_machine_expiry, held, backend_pids, finished)
            _await_lock_wait(backend_pids.get(timeout=TIMEOUT), finished)
            # The same transaction now books over the due hold: lazy expiry.
            booked = services.create_appointment(
                clinic_id=setup.clinic_id,
                enrollment_id=enrollment_id,
                practitioner_id=setup.practitioner_id,
                local_range=services.AppointmentLocalRange(
                    "2035-06-02T08:30", "2035-06-02T09:00"
                ),
                idempotency_key=uuid4(),
            )
            assert booked.status == "scheduled"
        refused = outcome.result(timeout=TIMEOUT)
    assert isinstance(refused, services.AppointmentLifecycleError), refused
    assert refused.code == "revision_conflict"
    _assert_event(held)


def _audit_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "organization_id": "33333333-3333-4333-8333-333333333333",
        "actor_user_id": None,
        "event_type": EVENT,
        "component_id": "clinic-os-worker",
        "component_ip": None,
        "affected_record_type": "scheduling.appointment",
        "affected_record_id": str(uuid4()),
        "payload": (
            '{"clinic_id": "11111111-1111-4111-8111-111111111111", '
            '"object_verb": "expired"}'
        ),
        "prev_hash": secrets.token_bytes(32),
        "curr_hash": secrets.token_bytes(32),
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    ("changes", "admitted"),
    [
        ({}, True),
        ({"event_type": "scheduling.appointment.cancelled"}, False),
        ({"component_id": "clinic-os-web"}, False),
        ({"component_ip": "192.0.2.10"}, False),
        ({"affected_record_type": "intake.patient"}, False),
        (
            {
                "payload": (
                    '{"clinic_id": "11111111-1111-4111-8111-111111111111", '
                    '"object_verb": "cancelled"}'
                )
            },
            False,
        ),
        (
            {
                "payload": (
                    '{"clinic_id": "11111111-1111-4111-8111-111111111111", '
                    '"object_verb": "expired", "reason_code": "x"}'
                )
            },
            False,
        ),
    ],
)
def test_null_actor_scope_admits_only_the_machine_expiry_shape(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    changes: dict[str, object],
    *,
    admitted: bool,
) -> None:
    del rbac_graph
    insert = (
        "INSERT INTO clinic_app.audit_event (organization_id, actor_user_id,"
        " event_type, component_id, component_ip, affected_record_type,"
        " affected_record_id, occurred_at_utc, payload, prev_hash, curr_hash)"
        " VALUES (%(organization_id)s, %(actor_user_id)s, %(event_type)s,"
        " %(component_id)s, %(component_ip)s, %(affected_record_type)s,"
        " %(affected_record_id)s, clock_timestamp(), %(payload)s::jsonb,"
        " %(prev_hash)s, %(curr_hash)s)"
    )
    row = _audit_row(**changes)
    with psycopg.connect(superuser_database_url) as owner:
        if admitted:
            assert owner.execute(insert, row).rowcount == 1
        else:
            with pytest.raises(psycopg.errors.CheckViolation) as refused:
                owner.execute(insert, row)
            assert refused.value.diag.constraint_name == "audit_event_actor_scope_ck"
        owner.rollback()


def _catalog() -> tuple[str, bool, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", [EXPIRE_HOLD])
        function = cursor.fetchone()
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = "
            "'scheduling_appointment_expiry_audit' AND NOT tgisinternal), "
            "pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "WHERE c.conname = 'audit_event_actor_scope_ck'"
        )
        trigger = cursor.fetchone()
    assert function is not None
    assert trigger is not None
    return str(function[0]), bool(trigger[0]), str(trigger[1])


def test_empty_reverse_restores_and_reapply_reinstalls_exactly(
    rbac_graph: RbacGraph,
) -> None:
    del rbac_graph
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proowner::regrole::text, prosecdef, proconfig, "
            "has_function_privilege('clinic_app', oid, 'EXECUTE'), "
            "has_function_privilege('clinic_resolver', oid, 'EXECUTE') "
            "FROM pg_proc WHERE oid = "
            "'clinic_app.audit_appointment_expired()'::regprocedure"
        )
        assert cursor.fetchone() == (
            "clinic_owner",
            True,
            ["search_path=pg_catalog, pg_temp"],
            False,
            False,
        )
    installed = _catalog()
    assert installed[1]
    assert "clinic-lock-v1:patient:" in installed[0]
    assert "scheduling.appointment.expired" in installed[2]
    MigrationExecutor(connection).migrate([LIFECYCLE])
    reversed_function, trigger, scope = _catalog()
    assert not trigger
    assert "clinic-lock-v1" not in reversed_function
    assert "scheduling.appointment.expired" not in scope
    MigrationExecutor(connection).migrate([EXPIRY_AUDIT])
    assert _catalog() == installed


def test_populated_reverse_refuses_and_keeps_the_ledger(
    expiry_world: LifecycleWorld,
) -> None:
    # Building the world expired one hold through the real W service.
    expired = expiry_world.rows["expired"]
    _assert_event(expired)
    with pytest.raises(IntegrityError) as refused:
        MigrationExecutor(connection).migrate([LIFECYCLE])
    cause = refused.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.diag.constraint_name == "scheduling_expiry_audit_rollback"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations WHERE app='scheduling' "
            "AND name='0008_expiry_audit'"
        )
        assert cursor.fetchone() == (1,)
    _assert_event(expired)
