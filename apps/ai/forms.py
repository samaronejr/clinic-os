"""Native organization controls; users cannot submit model or region choices."""

from __future__ import annotations

from typing import Final

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.ai.contracts import MAX_MICROS, Purpose
from apps.ai.policy import OrganizationPolicy

PURPOSE_LABELS: Final = {
    Purpose.SCRIBE_ASR: _("Scribe transcription"),
    Purpose.SCRIBE_DRAFT: _("Scribe drafting"),
    Purpose.BRIEF: _("Pre-visit brief"),
    Purpose.EXTRACTION: _("Document extraction"),
    Purpose.FRONT_DESK: _("Front desk drafts"),
    Purpose.ANALYST: _("Operational analysis"),
    Purpose.DECISION_SUPPORT: _("Clinical review aid"),
}


class PolicyForm(forms.Form):
    """Keep opt-in and exact integer spending caps in one versioned command."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    enabled = forms.BooleanField(
        label=_("Enable synthetic AI for this organization"), required=False
    )
    purposes = forms.MultipleChoiceField(
        label=_("Enabled AI capabilities"),
        choices=[(p.value, PURPOSE_LABELS[p]) for p in Purpose],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    monthly_budget_micros = forms.IntegerField(
        label=_("Monthly AI limit (micro-BRL)"), min_value=0, max_value=MAX_MICROS
    )
    for purpose, label in PURPOSE_LABELS.items():
        locals()["limit_" + purpose.value] = forms.IntegerField(
            label=label,
            help_text=_(
                "Monthly capability limit in micro-BRL; 1 BRL = 1000000 micro-BRL."
            ),
            min_value=0,
            max_value=MAX_MICROS,
            required=False,
            initial=0,
        )
    del purpose, label

    def clean_purposes(self) -> list[str]:
        """Refuse repeated capability selections instead of silently deduplicating."""
        purposes: list[str] = self.cleaned_data["purposes"]
        if len(purposes) != len(set(purposes)):
            message = _("Choose each AI capability only once.")
            raise forms.ValidationError(message)
        return purposes

    def policy(self) -> OrganizationPolicy:
        """Build policy after Django parsed the closed native form fields."""
        return OrganizationPolicy(
            enabled=self.cleaned_data["enabled"],
            purposes=tuple(self.cleaned_data["purposes"]),
            monthly_budget_micros=self.cleaned_data["monthly_budget_micros"],
            purpose_budgets=tuple(
                (p.value, self.cleaned_data["limit_" + p.value] or 0) for p in Purpose
            ),
        )
