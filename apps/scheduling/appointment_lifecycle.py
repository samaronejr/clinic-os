"""Appointment lifecycle v2 transition services (todo 22, D-9).

Every transition is keyword-only ``(*, clinic_id, appointment_id,
expected_revision, command_id)``. Authority follows the SM actor column through
``has_permission``: S (scheduling staff) books with ``appointment.book`` or, for
their own schedule, ``appointment.book_own``, and moves with ``appointment.move``
or ``appointment.move_own``; C (the booked clinician) starts and completes with
``appointment.move_own`` on their own appointment; P (the booked patient's
session) may confirm or cancel its own hold/request; W (no human actor) expires
due holds. The database lifecycle trigger re-decides each edge, owns DB time,
revisions and hold deadlines, and writes the transition receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final
from uuid import UUID, uuid5

import psycopg
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils.translation import gettext_lazy as _

from apps.identity.current_context import CurrentActorError, require_permission
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentIdempotencyConflictError,
    SlotConflict,
)
from apps.scheduling.appointment_locking import acquire_appointment_write_gates
from apps.scheduling.appointment_persistence import _resource_constraint
from apps.scheduling.appointment_transition_state import (
    reload_transition_appointment,
    transition_write_target,
)
from apps.scheduling.models import Appointment, AppointmentTransition
from apps.scheduling.patient_authority import (
    patient_booking_scope,
    record_appointment_event,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.utils.functional import Promise

ILLEGAL_TRANSITION: Final = "illegal_transition"
HOLD_EXPIRED: Final = "hold_expired"
REVISION_CONFLICT: Final = "revision_conflict"


class AppointmentLifecycleError(Exception):
    """A stable lifecycle refusal code with a metadata-only message."""

    MESSAGES: ClassVar[dict[str, Promise]] = {
        "illegal_transition": _("This appointment cannot move to that state."),
        "hold_expired": _("The hold on this time expired. Choose the time again."),
        "revision_conflict": _(
            "This appointment changed meanwhile. Reload before trying again."
        ),
    }

    def __init__(self, code: str) -> None:
        """Keep identifiers out of the exception; expose only the code."""
        self.code = code
        self.message = self.MESSAGES[code]
        super().__init__(code)


class AppointmentLifecycleInputError(ValueError):
    """Reject malformed transition input without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying input message."""
        super().__init__("appointment lifecycle input is invalid")


@dataclass(frozen=True, slots=True)
class Term:
    """One permission a transition asks, optionally limited to own schedule."""

    permission: str
    own: bool = False


# Pinned from the SM actor column and the RP agenda row (plan todo 22).
BOOK_TERMS: Final = (Term("appointment.book"), Term("appointment.book_own", own=True))
MOVE_TERMS: Final = (Term("appointment.move"), Term("appointment.move_own", own=True))
CARE_TERMS: Final = (Term("appointment.move_own", own=True),)

# action -> (legal source states, target state, staff terms, patient sources)
TRANSITIONS: Final[
    Mapping[str, tuple[frozenset[str], str, tuple[Term, ...], frozenset[str]]]
] = {
    "hold": (frozenset({"requested"}), "held", BOOK_TERMS, frozenset()),
    "book": (
        frozenset({"requested", "held"}),
        "scheduled",
        BOOK_TERMS,
        frozenset({"held"}),
    ),
    "arrive": (frozenset({"scheduled"}), "arrived", MOVE_TERMS, frozenset()),
    "start": (frozenset({"arrived"}), "in_progress", CARE_TERMS, frozenset()),
    "complete": (frozenset({"in_progress"}), "completed", CARE_TERMS, frozenset()),
    "cancel": (
        frozenset({"requested", "held", "scheduled", "arrived"}),
        "cancelled",
        MOVE_TERMS,
        frozenset({"requested", "held", "scheduled"}),
    ),
    "mark_no_show": (frozenset({"scheduled"}), "no_show", MOVE_TERMS, frozenset()),
}
AUDIT_EVENTS: Final = {
    "hold": "scheduling.appointment.held",
    "book": "scheduling.appointment.booked",
    "arrive": "scheduling.appointment.arrived",
    "start": "scheduling.appointment.started",
    "complete": "scheduling.appointment.completed",
    "cancel": "scheduling.appointment.cancelled",
    "mark_no_show": "scheduling.appointment.no_show",
}
_ILLEGAL: Final = frozenset(
    {
        "scheduling_appointment_transition_check",
        "scheduling_appointment_no_show_deadline",
        "scheduling_appointment_request_policy_check",
    }
)
_OCCUPANCY: Final = frozenset(
    {
        "scheduling_appointment_scheduled_patient_excl",
        "scheduling_appointment_scheduled_practitioner_excl",
    }
)
EXPIRY_NAMESPACE: Final = UUID("5b0f2a6e-6c44-4f39-9a8e-0e2d6f7c1a22")
EXPIRY_BATCH: Final = 100


@dataclass(frozen=True, slots=True)
class HoldExpiry:
    """The machine result: no human-readable appointment data."""

    appointment_id: UUID
    revision: int


def _validate(
    clinic_id: object,
    appointment_id: object,
    expected_revision: object,
    command: object,
) -> None:
    if (
        type(clinic_id) is not UUID
        or type(appointment_id) is not UUID
        or type(command) is not UUID
        or type(expected_revision) is not int
        or not 1 <= expected_revision <= 2**31 - 1
    ):
        raise AppointmentLifecycleInputError


def authorize_transition(action: str, appointment: Appointment, source: str) -> str:
    """Return the actor kind allowed to take ``action`` from ``source``."""
    _, _, terms, patient_sources = TRANSITIONS[action]
    scope = patient_booking_scope()
    if scope is not None:
        if source in patient_sources and (
            scope.organization_id,
            scope.clinic_id,
            scope.patient_id,
        ) == (
            appointment.organization_id,
            appointment.clinic_id,
            appointment.patient_id,
        ):
            return "patient"
        raise AppointmentAccessDeniedError
    for term in terms:
        try:
            actor = require_permission(term.permission, clinic_id=appointment.clinic_id)
        except CurrentActorError:
            continue
        if not term.own or actor == appointment.practitioner_id:
            return "staff"
    raise AppointmentAccessDeniedError


def _constraint(error: DatabaseError) -> tuple[str | None, str | None]:
    cause = error.__cause__
    if not isinstance(cause, psycopg.Error):
        return None, None
    return cause.sqlstate, cause.diag.constraint_name


def _save(current: Appointment, fields: tuple[str, ...]) -> None:
    try:
        with transaction.atomic():
            current.save(update_fields=fields)
    except IntegrityError as error:
        _, constraint = _constraint(error)
        if constraint in _ILLEGAL:
            raise AppointmentLifecycleError(ILLEGAL_TRANSITION) from error
        if constraint == "scheduling_appointment_hold_expired":
            raise AppointmentLifecycleError(HOLD_EXPIRED) from error
        if constraint == "scheduling_transition_command_uniq":
            raise AppointmentIdempotencyConflictError from error
        if constraint in _OCCUPANCY:
            raise SlotConflict from error
        if constraint == "scheduling_appointment_active_availability_check":
            raise AppointmentAvailabilityError from error
        _resource_constraint(error, constraint)
        raise
    except DatabaseError as error:
        if _constraint(error)[0] == "42501":
            raise AppointmentAccessDeniedError from error
        raise


def _transition(
    action: str,
    *,
    clinic_id: UUID,
    appointment_id: UUID,
    expected_revision: int,
    command_id: UUID,
) -> Appointment:
    _validate(clinic_id, appointment_id, expected_revision, command_id)
    sources, target_status, _, _ = TRANSITIONS[action]
    with transaction.atomic():
        try:
            discovered = Appointment.objects.get(pk=appointment_id, clinic_id=clinic_id)
        except Appointment.DoesNotExist as error:
            raise AppointmentAccessDeniedError from error
        target = transition_write_target(discovered)
        acquire_appointment_write_gates(target=target)
        current = reload_transition_appointment(target, appointment_id, for_update=True)
        receipt = AppointmentTransition.objects.filter(
            organization_id=current.organization_id, command_id=command_id
        ).first()
        if receipt is not None:
            authorize_transition(action, current, receipt.from_status)
            if (
                receipt.appointment_id == current.pk
                and receipt.to_status == target_status
            ):
                return current
            raise AppointmentIdempotencyConflictError
        kind = authorize_transition(action, current, current.status)
        if current.revision != expected_revision:
            raise AppointmentLifecycleError(REVISION_CONFLICT)
        if current.status not in sources:
            raise AppointmentLifecycleError(ILLEGAL_TRANSITION)
        current.status = target_status
        current.last_command_id = command_id
        fields: tuple[str, ...] = ("status", "last_command_id", "updated_at")
        if action == "cancel":
            current.cancellation_reason = (
                Appointment.CancellationReason.PATIENT_REQUEST
                if kind == "patient"
                else Appointment.CancellationReason.CLINIC_REQUEST
            )
            fields = (*fields, "cancellation_reason")
        _save(current, fields)
        current.refresh_from_db()
        record_appointment_event(
            AUDIT_EVENTS[action], clinic_id=clinic_id, affected_record_id=current.pk
        )
        return current


def hold(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Approve a patient request into an expiring hold (S)."""
    return _transition(
        "hold",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def book(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Confirm a request (S) or a live hold (S/P); an expired hold is refused."""
    return _transition(
        "book",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def arrive(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Record the patient's arrival for a booked appointment (S)."""
    return _transition(
        "arrive",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def start(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Begin the visit of an arrived patient (C, the booked clinician)."""
    return _transition(
        "start",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def complete(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Finish an in-progress visit (C); finalized notes are never touched."""
    return _transition(
        "complete",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def cancel(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Cancel a request, hold, booking or arrival; the reason follows the actor."""
    return _transition(
        "cancel",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def mark_no_show(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> Appointment:
    """Mark a booked patient absent once the start time has passed (S)."""
    return _transition(
        "mark_no_show",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        expected_revision=expected_revision,
        command_id=command_id,
    )


def _machine_context() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_user_id', true), '') IS NULL "
            "AND NULLIF(current_setting('app.current_patient_session', true), '') "
            "IS NULL"
        )
        row = cursor.fetchone()
    return row is not None and row[0] is True


def expire(
    *, clinic_id: UUID, appointment_id: UUID, expected_revision: int, command_id: UUID
) -> HoldExpiry:
    """Expire one due hold as the W principal (no human actor, DB time only)."""
    _validate(clinic_id, appointment_id, expected_revision, command_id)
    if not _machine_context():
        raise AppointmentAccessDeniedError
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.scheduling_expire_hold(%s, %s, %s, %s)",
                [clinic_id, appointment_id, expected_revision, command_id],
            )
            row = cursor.fetchone()
    except IntegrityError as error:
        _, constraint = _constraint(error)
        if constraint == "scheduling_appointment_revision_check":
            raise AppointmentLifecycleError(REVISION_CONFLICT) from error
        if constraint == "scheduling_transition_command_uniq":
            raise AppointmentIdempotencyConflictError from error
        if constraint in _ILLEGAL:
            raise AppointmentLifecycleError(ILLEGAL_TRANSITION) from error
        raise
    except DatabaseError as error:
        if _constraint(error)[0] == "42501":
            raise AppointmentAccessDeniedError from error
        raise
    if row is None or type(row[0]) is not int:
        raise AppointmentAccessDeniedError
    return HoldExpiry(appointment_id=appointment_id, revision=row[0])


def expire_due_holds(*, batch: int = EXPIRY_BATCH) -> tuple[HoldExpiry, ...]:
    """W job: expire every due hold, each in its own transaction and command."""
    if type(batch) is not int or not 1 <= batch <= EXPIRY_BATCH:
        raise AppointmentLifecycleInputError
    if not _machine_context():
        raise AppointmentAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.scheduling_due_holds(%s)", [batch])
        due = tuple(cursor.fetchall())
    expired = []
    for clinic_id, appointment_id, revision in due:
        try:
            expired.append(
                expire(
                    clinic_id=clinic_id,
                    appointment_id=appointment_id,
                    expected_revision=revision,
                    command_id=uuid5(EXPIRY_NAMESPACE, f"{appointment_id}:{revision}"),
                )
            )
        except (AppointmentLifecycleError, AppointmentIdempotencyConflictError):
            # A concurrent booking/expiry won the row lock; the next run re-reads.
            continue
    return tuple(expired)
