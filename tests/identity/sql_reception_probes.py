"""Real reception trigger INSERT members for the sealed SQL role census."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.scheduling.models import ClinicianDelay, DisplayDevice, QueueEntry, RoomStatus

from identity.sql_denial_contracts import DmlMember, SqlVerdict, probe_dml

if TYPE_CHECKING:
    from identity.sql_guard_probes import SqlWorld

QUEUE_MEMBER = DmlMember(
    "clinic_app.reception_guard",
    "clinic_app.scheduling_queueentry",
    "INSERT",
    "scheduling_queueentry",
)
ROOM_MEMBER = DmlMember(
    "clinic_app.reception_guard",
    "clinic_app.scheduling_roomstatus",
    "INSERT",
    "scheduling_roomstatus",
)
DELAY_MEMBER = DmlMember(
    "clinic_app.reception_guard",
    "clinic_app.scheduling_cliniciandelay",
    "INSERT",
    "scheduling_cliniciandelay",
)
DISPLAY_MEMBER = DmlMember(
    "clinic_app.reception_guard",
    "clinic_app.scheduling_displaydevice",
    "INSERT",
    "scheduling_displaydevice",
)


def insert_queue(w: SqlWorld, valid: bool) -> SqlVerdict:
    return probe_dml(
        QUEUE_MEMBER,
        QueueEntry(
            organization_id=w.actor.graph.organization_a,
            clinic_id=w.actor.clinic_for(valid),
            appointment_id=w.actor.appointment.pk,
            location_id=w.reception.setup.location.pk,
            position=2,
            ticket_code="A2",
        ),
    )


def insert_room(w: SqlWorld, valid: bool) -> SqlVerdict:
    return probe_dml(
        ROOM_MEMBER,
        RoomStatus(
            organization_id=w.actor.graph.organization_a,
            clinic_id=w.actor.clinic_for(valid),
            room_id=w.reception.setup.room.pk,
            unit=1,
            state="available",
        ),
    )


def insert_delay(w: SqlWorld, valid: bool) -> SqlVerdict:
    return probe_dml(
        DELAY_MEMBER,
        ClinicianDelay(
            organization_id=w.actor.graph.organization_a,
            clinic_id=w.actor.clinic_for(valid),
            practitioner_id=w.reception.setup.booking.practitioner_id,
            minutes=5,
        ),
    )


def insert_display(w: SqlWorld, valid: bool) -> SqlVerdict:
    return probe_dml(
        DISPLAY_MEMBER,
        DisplayDevice(
            organization_id=w.actor.graph.organization_a,
            clinic_id=w.actor.clinic_for(valid),
            token_hash="a" * 64,
        ),
    )
