from __future__ import annotations

from dataclasses import dataclass
from queue import Queue
from threading import Thread
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCancellationConflictError,
    AppointmentCancellationInputError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentLocalRange,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    SlotConflict,
    cancel_appointment,
    reschedule_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
)

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
)

if TYPE_CHECKING:
    from threading import Barrier
    from uuid import UUID

type AppointmentOutcome = Appointment | Exception


@dataclass(frozen=True, slots=True)
class _CreateCall:
    setup: AppointmentSetup
    idempotency_key: UUID
    backend_pids: Queue[int]
    outcomes: Queue[AppointmentOutcome]
    start_barrier: Barrier | None


def _create_worker(call: _CreateCall) -> None:
    close_old_connections()
    try:
        with (
            runtime_role(),
            tenant_context(call.setup.actor_id, call.setup.organization_id),
        ):
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
                create_synthetic_appointment(
                    call.setup,
                    idempotency_key=call.idempotency_key,
                )
            )
    except (
        AppointmentAccessDeniedError,
        AppointmentAvailabilityError,
        AppointmentCreateInputError,
        AppointmentIdempotencyConflictError,
        AppointmentPractitionerError,
        DatabaseError,
        SlotConflict,
    ) as error:
        call.outcomes.put(error)
    finally:
        connections.close_all()


def start_appointment_creator(
    setup: AppointmentSetup,
    idempotency_key: UUID,
    *,
    start_barrier: Barrier | None = None,
) -> tuple[Thread, Queue[int], Queue[AppointmentOutcome]]:
    backend_pids: Queue[int] = Queue()
    outcomes: Queue[AppointmentOutcome] = Queue()
    thread = Thread(
        target=_create_worker,
        args=(
            _CreateCall(
                setup=setup,
                idempotency_key=idempotency_key,
                backend_pids=backend_pids,
                outcomes=outcomes,
                start_barrier=start_barrier,
            ),
        ),
    )
    thread.start()
    return thread, backend_pids, outcomes


@dataclass(frozen=True, slots=True)
class _TransitionCall:
    actor_id: UUID
    organization_id: UUID
    appointment_id: UUID
    local_range: AppointmentLocalRange | None
    reason: str | None
    backend_pids: Queue[int]
    outcomes: Queue[AppointmentOutcome]
    start_barrier: Barrier


def _transition_worker(call: _TransitionCall) -> None:
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
            call.start_barrier.wait(timeout=5)
            if call.local_range is not None:
                outcome = reschedule_appointment(
                    appointment_id=call.appointment_id,
                    local_range=call.local_range,
                )
            elif call.reason is not None:
                outcome = cancel_appointment(
                    appointment_id=call.appointment_id,
                    reason=call.reason,
                )
            else:
                raise AssertionError
            call.outcomes.put(outcome)
    except (
        AppointmentAccessDeniedError,
        AppointmentAvailabilityError,
        AppointmentCancellationConflictError,
        AppointmentCancellationInputError,
        AppointmentCreateInputError,
        AppointmentIdempotencyConflictError,
        AppointmentPractitionerError,
        AppointmentRescheduleInputError,
        AppointmentTerminalError,
        DatabaseError,
        SlotConflict,
    ) as error:
        call.outcomes.put(error)
    finally:
        connections.close_all()


def _start_transition(call: _TransitionCall) -> Thread:
    thread = Thread(target=_transition_worker, args=(call,))
    thread.start()
    return thread


def start_appointment_rescheduler(
    *,
    actor_id: UUID,
    organization_id: UUID,
    appointment_id: UUID,
    local_range: AppointmentLocalRange,
    start_barrier: Barrier,
) -> tuple[Thread, Queue[int], Queue[AppointmentOutcome]]:
    backend_pids: Queue[int] = Queue()
    outcomes: Queue[AppointmentOutcome] = Queue()
    thread = _start_transition(
        _TransitionCall(
            actor_id,
            organization_id,
            appointment_id,
            local_range,
            None,
            backend_pids,
            outcomes,
            start_barrier,
        )
    )
    return thread, backend_pids, outcomes


def start_appointment_canceller(
    *,
    actor_id: UUID,
    organization_id: UUID,
    appointment_id: UUID,
    reason: str,
    start_barrier: Barrier,
) -> tuple[Thread, Queue[int], Queue[AppointmentOutcome]]:
    backend_pids: Queue[int] = Queue()
    outcomes: Queue[AppointmentOutcome] = Queue()
    thread = _start_transition(
        _TransitionCall(
            actor_id,
            organization_id,
            appointment_id,
            None,
            reason,
            backend_pids,
            outcomes,
            start_barrier,
        )
    )
    return thread, backend_pids, outcomes


def wait_for_advisory_waiter(
    backend_pid: int,
    outcomes: Queue[AppointmentOutcome],
) -> None:
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
            if not outcomes.empty():
                outcome = outcomes.get_nowait()
                outcomes.put(outcome)
                pytest.fail(
                    "appointment creator exited before the advisory gate: "
                    f"{type(outcome).__name__}"
                )
    pytest.fail("appointment creator did not reach the advisory gate")


def wait_for_row_waiter(
    backend_pid: int,
    outcomes: Queue[AppointmentOutcome],
) -> None:
    deadline = monotonic() + 5
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid = %s AND locktype <> 'advisory' AND NOT granted)",
                [backend_pid],
            )
            if cursor.fetchone() == (True,):
                return
            if not outcomes.empty():
                outcome = outcomes.get_nowait()
                outcomes.put(outcome)
                pytest.fail(
                    "appointment creator exited before the UUID row lock: "
                    f"{type(outcome).__name__}"
                )
    pytest.fail("appointment creator did not reach the UUID row lock")
