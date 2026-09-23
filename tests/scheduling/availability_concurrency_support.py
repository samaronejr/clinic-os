from __future__ import annotations

from dataclasses import dataclass
from queue import Queue
from threading import Barrier, Thread
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import AvailabilityBlock
from apps.scheduling.services import (
    AvailabilityAccessDeniedError,
    AvailabilityIdempotencyConflictError,
    AvailabilityOverlapError,
    AvailabilityPractitionerError,
    create_availability,
    retire_availability,
)
from apps.tenancy.db import tenant_context
from django.db import close_old_connections, connection, connections, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

type CreateOutcome = AvailabilityBlock | Exception


@dataclass(frozen=True, slots=True)
class _CreateCall:
    actor_id: UUID
    organization_id: UUID
    clinic_id: UUID
    practitioner_id: UUID
    idempotency_key: UUID
    backend_pids: Queue[int]
    outcomes: Queue[CreateOutcome]
    start_barrier: Barrier | None


def acquire_role_revocation_gates(clinic_id: UUID, user_id: UUID) -> None:
    acquire_advisory_locks((clinic_lock_key(clinic_id), *user_lock_keys((user_id,))))


def wait_for_advisory_waiter(backend_pid: int) -> None:
    deadline = monotonic() + 5
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                [backend_pid],
            )
            if cursor.fetchone() == (True,):
                return
    pytest.fail("availability creator did not reach the advisory gate")


def _create_worker(call: _CreateCall) -> None:
    close_old_connections()
    try:
        with runtime_role(), tenant_context(call.actor_id, call.organization_id):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                row = cursor.fetchone()
                cursor.execute("SHOW transaction_isolation")
                assert cursor.fetchone() == ("read committed",)
            assert row is not None
            call.backend_pids.put(int(row[0]))
            if call.start_barrier is not None:
                call.start_barrier.wait(timeout=5)
            call.outcomes.put(
                create_availability(
                    clinic_id=call.clinic_id,
                    practitioner_id=call.practitioner_id,
                    start_local="2035-04-01T09:00",
                    end_local="2035-04-01T10:00",
                    idempotency_key=call.idempotency_key,
                )
            )
    except (
        AvailabilityAccessDeniedError,
        AvailabilityIdempotencyConflictError,
        AvailabilityPractitionerError,
    ) as error:
        call.outcomes.put(error)
    finally:
        connections.close_all()


def start_creator(
    rbac_graph: RbacGraph,
    *,
    actor_id: UUID,
    idempotency_key: UUID | None = None,
    clinic_id: UUID | None = None,
    start_barrier: Barrier | None = None,
) -> tuple[Thread, Queue[int], Queue[CreateOutcome]]:
    backend_pids: Queue[int] = Queue()
    outcomes: Queue[CreateOutcome] = Queue()
    thread = Thread(
        target=_create_worker,
        args=(
            _CreateCall(
                actor_id=actor_id,
                organization_id=rbac_graph.organization_a,
                clinic_id=clinic_id or rbac_graph.clinic_a,
                practitioner_id=rbac_graph.physician,
                idempotency_key=idempotency_key or uuid4(),
                backend_pids=backend_pids,
                outcomes=outcomes,
                start_barrier=start_barrier,
            ),
        ),
    )
    thread.start()
    return thread, backend_pids, outcomes


def assert_no_availability_residue(rbac_graph: RbacGraph) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        assert AvailabilityBlock.objects.count() == 0
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        assert cursor.fetchone() == (0,)


def grant_cross_clinic_creator_roles(rbac_graph: RbacGraph) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        for user_id, role in (
            (rbac_graph.shared_user, UserClinicRole.Role.RECEPTIONIST),
            (rbac_graph.physician, UserClinicRole.Role.PHYSICIAN),
        ):
            UserClinicRole.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_b,
                user_id=user_id,
                role=role,
            )


def run_retire_create_race(
    rbac_graph: RbacGraph,
    availability_id: UUID,
) -> tuple[CreateOutcome, CreateOutcome]:
    barrier = Barrier(2)
    outcomes: Queue[CreateOutcome] = Queue()

    def create_worker() -> None:
        close_old_connections()
        try:
            with (
                runtime_role(),
                tenant_context(
                    rbac_graph.shared_user,
                    rbac_graph.organization_a,
                ),
            ):
                barrier.wait(timeout=5)
                outcomes.put(
                    create_availability(
                        clinic_id=rbac_graph.clinic_a,
                        practitioner_id=rbac_graph.physician,
                        start_local="2035-04-02T09:30",
                        end_local="2035-04-02T10:30",
                        idempotency_key=uuid4(),
                    )
                )
        except AvailabilityOverlapError as error:
            outcomes.put(error)
        finally:
            connections.close_all()

    def retire_worker() -> None:
        close_old_connections()
        try:
            with (
                runtime_role(),
                tenant_context(
                    rbac_graph.shared_user,
                    rbac_graph.organization_a,
                ),
            ):
                barrier.wait(timeout=5)
                outcomes.put(
                    retire_availability(
                        clinic_id=rbac_graph.clinic_a,
                        availability_id=availability_id,
                    )
                )
        finally:
            connections.close_all()

    threads = [Thread(target=create_worker), Thread(target=retire_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    return outcomes.get_nowait(), outcomes.get_nowait()
