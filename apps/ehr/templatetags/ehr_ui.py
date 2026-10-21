"""Translate stored clinical vocabulary without placing authority in the UI."""

from uuid import UUID

from django import template
from django.utils.translation import gettext_lazy as _

from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.longitudinal_values import CODES
from apps.ehr.services import ClinicalAccessDeniedError
from apps.ehr.workspace_forms import ROUTE_CHOICES

register = template.Library()


@register.filter
def provenance_style(value: str) -> str:
    """Adapt domain vocabulary to the shared provenance badge."""
    return {
        "reported_by_patient": "reported",
        "clinician_confirmed": "confirmed",
        "observed": "observed",
        "imported": "imported",
        "ai_drafted": "ai_drafted",
    }.get(value, "reported")


@register.filter
def record_kind(value: str) -> str:
    """Render kinds as pt-BR labels, never database enums."""
    return str(
        {
            "observation": _("Observation"),
            "medication": _("Medication"),
            "note": _("Clinical note"),
            "encounter": _("Encounter"),
            "addendum": _("Addendum"),
        }.get(value, _("Clinical record"))
    )


@register.filter
def observation_label(value: str) -> str:
    """Look up the label from the installed immutable local vocabulary."""
    code = CODES.get(value)
    return str(code.label) if code is not None else str(_("Observation"))


@register.filter
def route_label(value: str) -> str:
    """Translate the closed medication route vocabulary."""
    return str(dict(ROUTE_CHOICES).get(value, _("Other")))


@register.filter
def clinical_decimal(value: str) -> str:
    """Display retained decimal strings with a comma, without float conversion."""
    return {
        "yes": str(_("Yes")),
        "no": str(_("No")),
        "unknown": str(_("Unknown")),
    }.get(value, value.replace(".", ","))


@register.simple_tag
def patient_workspace_allowed(clinic_id: UUID, enrollment_id: UUID) -> bool:
    """Hide the clinical entry when current scope would refuse it."""
    try:
        patient_scope(clinic_id=clinic_id, enrollment_id=enrollment_id, action="read")
    except ClinicalAccessDeniedError:
        return False
    return True
