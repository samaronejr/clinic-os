"""Show accepted AI origin under the clinical version's own read policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.utils.translation import gettext as _

from apps.ehr.forms import SECTION_LABELS
from apps.scribe.models import ReviewDecision

if TYPE_CHECKING:
    from uuid import UUID

register = template.Library()


@register.inclusion_tag("scribe/provenance.html")
def note_ai_history(clinic_id: UUID, encounter_id: UUID) -> dict[str, object]:
    """Finalized-note readers see origin without access to raw AI artifacts."""
    records = (
        ReviewDecision.objects.filter(
            version__document__encounter_id=encounter_id,
            version__document__encounter__clinic_id=clinic_id,
            action__in=("accepted", "modified"),
        )
        .defer("applied_text")
        .order_by("created_at", "pk")[:100]
    )
    return {
        "ai_use_records": [
            {
                "section": SECTION_LABELS[record.section],
                "action": _("Accepted")
                if record.action == "accepted"
                else _("Modified"),
                "configuration": record.capability_snapshot["configuration_id"],
                "configuration_version": record.capability_snapshot["version"],
                "revision": record.result_revision,
                "created_at": record.created_at,
            }
            for record in records
        ]
    }
