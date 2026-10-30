"""An encounter include that never opts the clinician in or retrieves patient facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

from apps.ai.decision_support import DecisionPanel, open_decision_support
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError

if TYPE_CHECKING:
    from uuid import UUID

register = template.Library()


@register.inclusion_tag("ai/decision_fragment.html", takes_context=True)
def decision_panel(
    context: template.Context, clinic_id: UUID, encounter_id: UUID
) -> dict[str, object]:
    """Show neither patient facts nor hidden counts for an unavailable capability."""
    try:
        panel = open_decision_support(clinic_id=clinic_id, encounter_id=encounter_id)
    except (CurrentActorError, ClinicalAccessDeniedError):
        panel = DecisionPanel()
    return {
        "request": context["request"],
        "clinic_id": clinic_id,
        "encounter_id": encounter_id,
        "cds": panel,
    }
