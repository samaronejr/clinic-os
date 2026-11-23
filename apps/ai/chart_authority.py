"""Visit-bound chart AI; neither chart access nor a newer acceptance is consent."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from apps.ai.brief_contracts import VisitPermission
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.consent.services import current_ai_acceptance
from apps.ehr.chart_sources import chart_enrollment
from apps.ehr.models import Encounter
from apps.ehr.services import record_denial
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.brief_contracts import ChartDisplay, ChartRequest


def visit_permission(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    encounter_id: UUID | None,
    expected_encounter: UUID | None = None,
) -> VisitPermission:
    """Never search for a favorable disclosure: bind only the actual visit.

    An enrollment-only workspace has no visit selector. Resolve its sole open
    encounter, or fail closed (pre-visit, historical-only or ambiguous). A
    selected encounter always uses its own immutable attestation.
    """
    if encounter_id is None:
        subjects = PatientClinicEnrollment.objects.filter(
            pk=enrollment_id, clinic_id=clinic_id
        ).values("patient_id")
        visits = list(
            Encounter.objects.filter(
                patient_id__in=subjects, clinic_id=clinic_id, state="open"
            ).values_list("pk", flat=True)[:2]
        )
        if len(visits) != 1:
            record_denial(clinic_id, enrollment_id, "ai_permission_unavailable")
            raise GatewayRefusedError(reason="authority_revoked")
        encounter_id = visits[0]
    if expected_encounter is not None and encounter_id != expected_encounter:
        raise GatewayAccessDeniedError
    if (
        chart_enrollment(clinic_id=clinic_id, encounter_id=encounter_id)
        != enrollment_id
    ):
        raise GatewayAccessDeniedError
    receipt = current_ai_acceptance(
        clinic_id=clinic_id, encounter_id=encounter_id, enrollment_id=enrollment_id
    )
    if receipt is None:
        record_denial(clinic_id, encounter_id, "ai_permission_unavailable")
        raise GatewayRefusedError(reason="authority_revoked")
    return VisitPermission(encounter_id, receipt.pk)


def recheck_chart_permission(
    *,
    clinic_id: UUID,
    request: ChartRequest,
    enrollment_id: UUID | None,
    display: ChartDisplay | None = None,
) -> None:
    """Legacy/unbound requests cannot run or reveal retained material."""
    binding = request.permission
    if (
        binding is None
        or request.enrollment != enrollment_id
        or (
            display is not None
            and not hmac.compare_digest(request.session, display.session)
        )
    ):
        raise GatewayAccessDeniedError
    if (
        visit_permission(
            clinic_id=clinic_id,
            enrollment_id=request.enrollment,
            encounter_id=binding.encounter if display is None else display.encounter,
            expected_encounter=binding.encounter,
        )
        != binding
    ):
        record_denial(clinic_id, binding.encounter, "ai_permission_unavailable")
        raise GatewayRefusedError(reason="authority_revoked")
