"""Current owner-recorded gates and patient refusal, with no synthetic bypass flag."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.decision_contracts import parse_decision_request, resolve_catalog
from apps.ai.policy import enabled_policy
from apps.ai.reference_catalog import load_catalog
from apps.consent.services import ai_disclosure_status
from apps.ehr.chart_sources import chart_enrollment
from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import UserClinicRole
from apps.providers.services import current_version

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.decision_contracts import DecisionRequest
    from apps.ai.reference_catalog import CatalogEntry

GATE_CAPABILITIES = (
    ("decision_support_safety", "EG-3"),
    ("decision_support_regulatory", "EG-4"),
    ("decision_support_references", "EG-13"),
)


def authorized_catalog(*, clinic_id: UUID) -> tuple[CatalogEntry, ...]:
    """Refuse researched/sandbox rows and missing authorization evidence."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    enabled_policy(Purpose.DECISION_SUPPORT)
    for key, gate in GATE_CAPABILITIES:
        version = current_version(key, clinic_id=clinic_id)
        if (
            version is None
            or version.state not in {"production_authorized", "activated"}
            or version.capability.key != key
            or version.capability.record_ref != gate
            or version.capability.clinic_id not in {None, clinic_id}
            or version.approval is None
            or version.approval.capability_id != version.capability_id
            or not version.approval.evidence_uri
        ):
            raise GatewayRefusedError(reason="configuration_unavailable")
    return load_catalog()


def authorize_decision(
    *, clinic_id: UUID, content: str, enrollment_id: UUID | None
) -> DecisionRequest:
    """Recheck authorization at artifact, gateway, dispatch and display boundaries."""
    request = parse_decision_request(content)
    enrolled = chart_enrollment(clinic_id=clinic_id, encounter_id=request.encounter)
    if enrolled != request.chart.enrollment or enrolled != enrollment_id:
        raise GatewayAccessDeniedError
    disclosure = ai_disclosure_status(
        clinic_id=clinic_id, encounter_id=request.encounter
    )
    if disclosure is None or not disclosure.informed or disclosure.refused:
        raise GatewayRefusedError(reason="disabled")
    resolve_catalog(request, authorized_catalog(clinic_id=clinic_id))
    return request
