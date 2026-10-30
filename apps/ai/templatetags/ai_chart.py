"""The drawer never retrieves a chart until an authorized POST requests it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.utils.translation import gettext as _

from apps.ai.brief import Brief
from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ai.policy import enabled_policy
from apps.core.chart_sources import SECTIONS
from apps.ehr.chart_sources import chart_enrollment
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID

register = template.Library()


@register.inclusion_tag("ai/brief_panel.html", takes_context=True)
def chart_panel(
    context: template.Context,
    clinic_id: UUID,
    enrollment_id: UUID | None = None,
    encounter_id: UUID | None = None,
    surface: str = "workspace",
) -> dict[str, object]:
    """Expose no counts, citations or controls to a refused actor."""
    result: dict[str, object] = {
        "request": context["request"],
        "clinic_id": clinic_id,
        "surface": surface,
        "can_request": False,
        "brief": Brief("unavailable"),
    }
    if not enrollment_id and not encounter_id:
        return result
    try:
        enrolled = chart_enrollment(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            encounter_id=encounter_id,
        )
        enabled_policy(Purpose.BRIEF)
    except (CurrentActorError, ClinicalAccessDeniedError, GatewayRefusedError):
        return result
    brief = context.get("chart_brief")
    if not isinstance(brief, Brief) or context.get("chart_enrollment_id") != enrolled:
        brief = Brief("unavailable")
    labels = dict(
        zip(
            SECTIONS,
            (
                _("Chronology"),
                _("Active problems"),
                _("Allergies"),
                _("Medications"),
                _("Observations"),
                _("Recent results"),
                _("Outstanding tasks"),
                _("Prior decisions"),
            ),
            strict=True,
        )
    )
    coverage = dict(brief.chart.coverage)
    result.update(
        can_request=True,
        enrollment_id=enrolled,
        brief=brief,
        clinic_timezone=Clinic.objects.get(pk=clinic_id).timezone,
        sections=[
            {
                "key": section,
                "label": labels[section],
                "sources": [
                    source
                    for source in brief.chart.sources
                    if source.section == section
                ],
                "coverage": coverage.get(section, "unavailable"),
            }
            for section in SECTIONS
            if section in coverage
        ],
    )
    return result


@register.filter
def citation_kind(kind: str) -> str:
    """Translate closed source kinds instead of displaying stored enums."""
    return {
        "encounter": _("Encounter"),
        "problem": _("Problem"),
        "problem_assessment": _("Problem assessment"),
        "allergy": _("Allergy"),
        "allergy_assessment": _("Allergy assessment"),
        "medication": _("Medication"),
        "observation": _("Observation"),
        "result": _("Result"),
        "task": _("Task"),
        "note": _("Clinical note"),
    }.get(kind, _("Source"))
