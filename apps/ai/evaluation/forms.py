"""Explicit, required structured review decisions; never a clinical-text input."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.ai.evaluation.protocol import ERROR_LABELS

if TYPE_CHECKING:
    from django.http import QueryDict

LABELS = {
    "significant_omission": _("Clinically significant omission"),
    "hallucination": _("Unsupported clinical assertion"),
    "speaker_attribution": _("Speaker attribution error"),
    "negation": _("Negation error"),
    "chronology": _("Chronology error"),
    "units_decimals": _("Unit or decimal error"),
    "medication_name_dose": _("Medication name or dose error"),
    "allergy_handling": _("Allergy handling error"),
    "unsupported_citation": _("Unsupported citation"),
    "incorrect_action": _("Incorrect action"),
    "policy_violation": _("Policy violation"),
    "unauthorized_effect": _("Unauthorized side effect"),
    "duplicate_effect": _("Duplicate side effect"),
    "missed_escalation": _("Missed escalation"),
    "critical_error": _("Critical error"),
    "correction_ms": _("Correction time in milliseconds"),
    "clinical_error": _("Cases with clinical error"),
    "citation_support": _("Citation support"),
    "action_correctness": _("Action correctness"),
    "specialty": _("Specialty"),
    "device": _("Device"),
    "accent": _("Simulated accent"),
    "passed": _("Passed"),
    "failed": _("Failed"),
}


class RatingForm(forms.Form):
    """All rubric choices require an explicit answer, including no-error ratings."""

    run_id = forms.UUIDField(widget=forms.HiddenInput)
    case_id = forms.CharField(max_length=64, widget=forms.HiddenInput)
    correction_ms = forms.IntegerField(
        label=_("Correction time in milliseconds"), min_value=0, max_value=3_600_000
    )

    def __init__(
        self,
        data: QueryDict | None = None,
        *,
        initial: dict[str, object] | None = None,
    ) -> None:
        """Keep field order stable and radio groups explicitly named."""
        super().__init__(data=data, initial=initial)
        for name in ERROR_LABELS:
            self.fields[name] = forms.TypedChoiceField(
                label=LABELS[name],
                choices=((0, _("No error")), (1, _("Error observed"))),
                coerce=int,
                widget=forms.RadioSelect,
            )
        self.order_fields(["run_id", "case_id", *ERROR_LABELS, "correction_ms"])
