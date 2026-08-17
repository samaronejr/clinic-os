from __future__ import annotations

import importlib
from typing import Final
from uuid import UUID

from django.utils import timezone

type EventContract = tuple[str, str, str, str]

CLINIC_ID: Final = UUID("11111111-1111-4111-8111-111111111111")
AFFECTED_ID: Final = UUID("22222222-2222-4222-8222-222222222222")
FIXED_MATRIX: Final[dict[str, EventContract]] = {
    "ops.clinic.bootstrapped": (
        "system",
        "clinic-os-ops",
        "identity.organization",
        "bootstrapped",
    ),
    "identity.staff.provisioned": (
        "tenant",
        "clinic-os-ops",
        "identity.user_clinic_role",
        "provisioned",
    ),
    "identity.staff_role.revoked": (
        "tenant",
        "clinic-os-ops",
        "identity.user_clinic_role",
        "revoked",
    ),
    "identity.clinic_timezone.changed": (
        "tenant",
        "clinic-os-ops",
        "identity.clinic",
        "timezone_changed",
    ),
    "intake.patient.created": (
        "tenant",
        "clinic-os-web",
        "intake.patient",
        "created",
    ),
    "intake.patient.searched": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "searched",
    ),
    "scheduling.availability.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.availability",
        "created",
    ),
    "scheduling.availability.retired": (
        "tenant",
        "clinic-os-web",
        "scheduling.availability",
        "retired",
    ),
    "scheduling.availability.viewed": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "viewed",
    ),
    "scheduling.booking.viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "scheduling.appointment.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "created",
    ),
    "scheduling.appointment.rescheduled": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "rescheduled",
    ),
    "scheduling.appointment.cancelled": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "cancelled",
    ),
    "scheduling.appointment.viewed": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "viewed",
    ),
    "scheduling.agenda.viewed": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "viewed",
    ),
}


def test_fixed_matrix_builds_only_clinic_metadata() -> None:
    audit_events = importlib.import_module("apps.audit.events")
    implemented = {
        event_type: (
            definition.chain,
            definition.component_id,
            definition.affected_record_type,
            definition.object_verb,
        )
        for event_type, definition in audit_events.PHASE1_AUDIT_EVENTS.items()
    }

    assert implemented == FIXED_MATRIX
    for event_type, expected in FIXED_MATRIX.items():
        earliest = timezone.now()
        append = audit_events.build_phase1_audit_event(
            event_type,
            clinic_id=CLINIC_ID,
            affected_record_id=AFFECTED_ID,
        )
        latest = timezone.now()

        assert append.chain == expected[0]
        assert append.event.event_type == event_type
        assert append.event.component_id == expected[1]
        assert append.event.component_ip is None
        assert append.event.affected_record_type == expected[2]
        assert append.event.affected_record_id == str(AFFECTED_ID)
        assert earliest <= append.event.occurred_at_utc <= latest
        assert append.payload == {
            "clinic_id": str(CLINIC_ID),
            "object_verb": expected[3],
        }
