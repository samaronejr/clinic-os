"""Scoped arrival, queue, room and display-device services."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import psycopg
from django.db import DatabaseError, connection, transaction
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic, UserClinicRole
from apps.realtime.transport import publish_on_commit
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_lifecycle import arrive, authorize_transition
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.models import (
    Appointment,
    ClinicianDelay,
    DisplayDevice,
    QueueEntry,
    Resource,
    RoomStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

MAX_ROOM_UNITS: Final = 64
MAX_DELAY_MINUTES: Final = 720


class ReceptionAccessDeniedError(Exception):
    """Hide unknown, foreign and unauthorized reception selectors."""

    def __init__(self) -> None:
        """Expose no identifiers or input values."""
        super().__init__("reception access denied")


class ReceptionInputError(ValueError):
    """Reject malformed administrative input without reflecting it."""

    def __init__(self) -> None:
        """Keep the error independent of the rejected value."""
        super().__init__("invalid reception input")


class ReceptionConflictError(Exception):
    """A stale row or a room that became unavailable."""

    message = _("The queue or room changed meanwhile. Reload before trying again.")

    def __init__(self) -> None:
        """Expose one stable conflict code."""
        super().__init__("reception_conflict")


@dataclass(frozen=True, slots=True)
class ArrivalReadiness:
    """Administrative metadata only; consent and payment never block arrival."""

    forms_pending: int
    consents_pending: int
    payment_pending: bool | None
    identity_document_recorded: bool


@dataclass(frozen=True, slots=True)
class IssuedDisplayDevice:
    """Show the secret once; the database retains only its SHA-256 digest."""

    device_id: UUID
    token: str


def reception_clinic(*, clinic_id: UUID) -> Clinic:
    """Authorize queue/room work independently of appointment mutations."""
    try:
        require_permission("reception.queue", clinic_id=clinic_id)
        return Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise ReceptionAccessDeniedError from error


def reception_admin_clinic(*, clinic_id: UUID) -> Clinic:
    """Keep delay broadcasts and device administration reception-only."""
    clinic = reception_clinic(clinic_id=clinic_id)
    try:
        require_permission("appointment.move", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise ReceptionAccessDeniedError from error
    return clinic


@contextmanager
def _write(clinic_id: UUID) -> Iterator[None]:
    """Serialize with appointment writes before locking any reception row."""
    try:
        with transaction.atomic():
            acquire_advisory_locks((clinic_lock_key(clinic_id),))
            yield
    except AppointmentAccessDeniedError as error:
        raise ReceptionAccessDeniedError from error
    except DatabaseError as error:
        cause = error.__cause__
        if isinstance(cause, psycopg.Error):
            if cause.sqlstate == "42501":
                raise ReceptionAccessDeniedError from error
            if cause.diag.constraint_name in {
                "scheduling_queue_room_occupied",
                "scheduling_queue_room_unavailable",
                "scheduling_queue_transition",
            }:
                raise ReceptionConflictError from error
        raise


def _changed(event: str, clinic_id: UUID, record_id: UUID) -> None:
    record_phase1_event(event, clinic_id=clinic_id, affected_record_id=record_id)
    publish_on_commit(topic=f"clinic:{clinic_id}:queue", kind="queue", version=1)


def _unit_revision(unit: int, revision: int) -> None:
    if (
        type(unit) is not int
        or not 1 <= unit <= MAX_ROOM_UNITS
        or type(revision) is not int
        or not 0 <= revision <= 2**31 - 1
    ):
        raise ReceptionInputError


def arrival_readiness(*, clinic_id: UUID, appointment_id: UUID) -> ArrivalReadiness:
    """Read counts and flags without exposing clinical answers or financial terms."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.reception_readiness(%s,%s)",
            [clinic_id, appointment_id],
        )
        row = cursor.fetchone()
    if row is None:
        raise ReceptionAccessDeniedError
    return ArrivalReadiness(
        forms_pending=row[0],
        consents_pending=row[1],
        payment_pending=row[2],
        identity_document_recorded=row[3],
    )


def mark_arrived(
    *,
    clinic_id: UUID,
    appointment_id: UUID,
    location_id: UUID,
    expected_revision: int,
    command_id: UUID,
) -> QueueEntry:
    """Keep existing arrival authority and atomically enqueue one location ticket."""
    with _write(clinic_id):
        try:
            appointment = Appointment.objects.get(
                pk=appointment_id, clinic_id=clinic_id
            )
            authorize_transition("arrive", appointment, "scheduled")
            Resource.objects.get(
                pk=location_id, clinic_id=clinic_id, kind="location", active=True
            )
        except (Appointment.DoesNotExist, Resource.DoesNotExist) as error:
            raise ReceptionAccessDeniedError from error
        if (
            appointment.status == "arrived"
            and appointment.revision == expected_revision
        ):
            # The agenda may already have registered arrival without a location.
            # Recheck the same arrival terms above, then add only the queue ticket.
            current = appointment
        else:
            current = arrive(
                clinic_id=clinic_id,
                appointment_id=appointment_id,
                expected_revision=expected_revision,
                command_id=command_id,
            )
        entry = QueueEntry.objects.filter(appointment_id=current.pk).first()
        if entry is not None:
            if entry.location_id != location_id:
                raise ReceptionConflictError
            return entry
        entry = QueueEntry.objects.create(
            organization_id=current.organization_id,
            clinic_id=clinic_id,
            appointment_id=current.pk,
            location_id=location_id,
        )
        entry.refresh_from_db()
        _changed("scheduling.queue.arrived", clinic_id, entry.pk)
        return entry


def call_to_room(
    *,
    clinic_id: UUID,
    queue_entry_id: UUID,
    room_id: UUID,
    room_unit: int,
    expected_revision: int,
) -> QueueEntry:
    """Call an arrived ticket once; the database owns room-capacity arbitration."""
    _unit_revision(room_unit, expected_revision)
    clinic = reception_clinic(clinic_id=clinic_id)
    with _write(clinic_id):
        try:
            entry = QueueEntry.objects.select_for_update().get(
                pk=queue_entry_id, clinic_id=clinic_id
            )
        except QueueEntry.DoesNotExist as error:
            raise ReceptionAccessDeniedError from error
        # Resolve every selector before exposing a revision or state conflict.
        if not Resource.objects.filter(
            pk=room_id, clinic_id=clinic_id, kind="room", active=True
        ).exists():
            raise ReceptionAccessDeniedError
        if (
            entry.revision != expected_revision
            or entry.state != QueueEntry.State.WAITING
        ):
            raise ReceptionConflictError
        if not RoomStatus.objects.filter(room_id=room_id, unit=room_unit).exists():
            status = RoomStatus.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                room_id=room_id,
                unit=room_unit,
                state=RoomStatus.State.AVAILABLE,
            )
            _changed("scheduling.room.updated", clinic_id, status.pk)
        entry.state = QueueEntry.State.CALLED
        entry.room_id = room_id
        entry.room_unit = room_unit
        entry.save(update_fields=("state", "room", "room_unit"))
        entry.refresh_from_db()
        _changed("scheduling.queue.called", clinic_id, entry.pk)
        return entry


def update_room_status(
    *,
    clinic_id: UUID,
    room_id: UUID,
    unit: int,
    state: str,
    expected_revision: int,
) -> RoomStatus:
    """Record a closed room condition without releasing an occupied room."""
    _unit_revision(unit, expected_revision)
    if state not in RoomStatus.State.values:
        raise ReceptionInputError
    clinic = reception_clinic(clinic_id=clinic_id)
    with _write(clinic_id):
        if not Resource.objects.filter(
            pk=room_id, clinic_id=clinic_id, kind="room", active=True
        ).exists():
            raise ReceptionAccessDeniedError
        status = RoomStatus.objects.filter(room_id=room_id, unit=unit).first()
        if status is None:
            if expected_revision != 0:
                raise ReceptionConflictError
            status = RoomStatus.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                room_id=room_id,
                unit=unit,
                state=state,
            )
        else:
            if status.revision != expected_revision:
                raise ReceptionConflictError
            if status.state == state:
                return status
            status.state = state
            status.save(update_fields=("state",))
        status.refresh_from_db()
        _changed("scheduling.room.updated", clinic_id, status.pk)
        return status


def broadcast_clinician_delay(
    *,
    clinic_id: UUID,
    practitioner_id: UUID,
    minutes: int,
    expected_revision: int,
) -> ClinicianDelay:
    """Broadcast a bounded delay estimate, with optimistic revision checking."""
    _unit_revision(1, expected_revision)
    if type(minutes) is not int or not 0 <= minutes <= MAX_DELAY_MINUTES:
        raise ReceptionInputError
    clinic = reception_admin_clinic(clinic_id=clinic_id)
    with _write(clinic_id):
        if not UserClinicRole.objects.filter(
            clinic_id=clinic_id,
            user_id=practitioner_id,
            revoked_at__isnull=True,
            role__in=("physician", "nurse", "allied_professional"),
        ).exists():
            raise ReceptionAccessDeniedError
        delay = ClinicianDelay.objects.filter(
            clinic_id=clinic_id, practitioner_id=practitioner_id
        ).first()
        if delay is None:
            if expected_revision != 0:
                raise ReceptionConflictError
            delay = ClinicianDelay.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                practitioner_id=practitioner_id,
                minutes=minutes,
            )
        else:
            if delay.revision != expected_revision:
                raise ReceptionConflictError
            if delay.minutes == minutes:
                return delay
            delay.minutes = minutes
            delay.save(update_fields=("minutes",))
        delay.refresh_from_db()
        _changed("scheduling.delay.updated", clinic_id, delay.pk)
        return delay


def issue_display_device(*, clinic_id: UUID) -> IssuedDisplayDevice:
    """Mint a read-only device token with no staff-session impersonation."""
    clinic = reception_admin_clinic(clinic_id=clinic_id)
    with _write(clinic_id):
        token = secrets.token_urlsafe(32)
        device = DisplayDevice.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
        )
        _changed("scheduling.display.issued", clinic_id, device.pk)
        return IssuedDisplayDevice(device.pk, token)


def revoke_display_device(*, clinic_id: UUID, device_id: UUID) -> None:
    """Revoke on the next display read, including existing device cookies."""
    reception_admin_clinic(clinic_id=clinic_id)
    with _write(clinic_id):
        try:
            device = DisplayDevice.objects.get(pk=device_id, clinic_id=clinic_id)
        except DisplayDevice.DoesNotExist as error:
            raise ReceptionAccessDeniedError from error
        if device.active:
            device.active = False
            device.save(update_fields=("active",))
            _changed("scheduling.display.revoked", clinic_id, device.pk)
