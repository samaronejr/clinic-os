"""Two independent choices: an exact visit attestation and current patient consent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.consent.models import ConsentPurpose
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_consent,
    record_refusal,
)
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


def accept_ai(graph: RbacGraph, enrollment: UUID) -> tuple[UUID, UUID]:
    """Publish as staff; accept as the patient, never as the encounter physician."""
    return choose_ai(
        ConsentActors(
            graph.organization_a, graph.clinic_a, graph.clinic_admin, graph.shared_user
        ),
        enrollment,
    )


@dataclass(frozen=True)
class ConsentActors:
    organization: UUID
    clinic: UUID
    publisher: UUID
    inviter: UUID


def choose_ai(
    scope: ConsentActors, enrollment: UUID, *, accept: bool = True
) -> tuple[UUID, UUID]:
    """Use distinct staff and patient contexts, also in a served-browser fixture."""
    purpose = ConsentPurpose.AI_ASSISTANCE
    with runtime_role(), tenant_context(scope.publisher, scope.organization):
        text = publish_text(
            clinic_id=scope.clinic,
            purpose=purpose,
            text="Consentimento sintético específico para assistência por IA.",
        )
    with runtime_role(), tenant_context(scope.inviter, scope.organization):
        invitation = issue_invitation(clinic_id=scope.clinic, enrollment_id=enrollment)
    with runtime_role():
        patient = redeem_invitation(scope.clinic, invitation.secret)
    assert patient is not None
    with runtime_role(), patient_session_context(patient):
        _, offer = prepare_acceptance(text_id=text.pk)
        if not accept:
            return patient, record_refusal(offer=offer, purpose=purpose).pk
        return patient, record_consent(offer=offer, purpose=purpose, accepted=True).pk
