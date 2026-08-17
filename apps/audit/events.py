"""Fixed Phase 1A metadata-only audit event vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from django.utils import timezone

from apps.audit.canonical import AuditEventInput

if TYPE_CHECKING:
    from collections.abc import Mapping

type AuditChain = Literal["tenant", "system"]


@dataclass(frozen=True, slots=True)
class Phase1AuditEventDefinition:
    """Immutable event fields shared by every append of one event type."""

    chain: AuditChain
    component_id: str
    affected_record_type: str
    object_verb: str


@dataclass(frozen=True, slots=True)
class Phase1AuditAppend:
    """Complete metadata-only arguments for one tenant or system append."""

    chain: AuditChain
    event: AuditEventInput
    payload: Mapping[str, str]


class Phase1AuditEventRejectedError(ValueError):
    """Reject values outside the fixed Phase 1A event contract."""

    def __init__(self) -> None:
        """Expose one stable message without reflecting caller data."""
        super().__init__("Phase 1A audit event input is invalid")


PHASE1_AUDIT_EVENTS: Final[Mapping[str, Phase1AuditEventDefinition]] = MappingProxyType(
    {
        "ops.clinic.bootstrapped": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "identity.organization",
            "bootstrapped",
        ),
        "identity.staff.provisioned": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.user_clinic_role",
            "provisioned",
        ),
        "identity.staff_role.revoked": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.user_clinic_role",
            "revoked",
        ),
        "identity.clinic_timezone.changed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.clinic",
            "timezone_changed",
        ),
        "intake.patient.created": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient",
            "created",
        ),
        "intake.patient.searched": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "identity.clinic",
            "searched",
        ),
        "scheduling.availability.created": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.availability",
            "created",
        ),
        "scheduling.availability.retired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.availability",
            "retired",
        ),
        "scheduling.availability.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "identity.clinic",
            "viewed",
        ),
        "scheduling.booking.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_clinic_enrollment",
            "viewed",
        ),
        "scheduling.appointment.created": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "created",
        ),
        "scheduling.appointment.rescheduled": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "rescheduled",
        ),
        "scheduling.appointment.cancelled": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "cancelled",
        ),
        "scheduling.appointment.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "viewed",
        ),
        "scheduling.agenda.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "identity.clinic",
            "viewed",
        ),
    }
)


def build_phase1_audit_event(
    event_type: str,
    *,
    clinic_id: UUID,
    affected_record_id: UUID,
) -> Phase1AuditAppend:
    """Build exact fields for one fixed event without domain or patient data."""
    definition = PHASE1_AUDIT_EVENTS.get(event_type)
    if (
        definition is None
        or type(clinic_id) is not UUID
        or type(affected_record_id) is not UUID
    ):
        raise Phase1AuditEventRejectedError
    return Phase1AuditAppend(
        chain=definition.chain,
        event=AuditEventInput(
            event_type=event_type,
            component_id=definition.component_id,
            component_ip=None,
            affected_record_type=definition.affected_record_type,
            affected_record_id=str(affected_record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload={
            "clinic_id": str(clinic_id),
            "object_verb": definition.object_verb,
        },
    )
