"""Native forms expose bounded controls, never executable JSON or arbitrary tools."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.ai.agent_policies import PolicyTerms


class AgentPolicyForm(forms.Form):
    """Publish a successor version with explicitly disclosed autonomy and limits."""

    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    enabled = forms.BooleanField(label=_("Enable this synthetic agent"), required=False)
    autonomy_ceiling = forms.ChoiceField(
        label=_("Maximum autonomy"),
        choices=(
            ("A0", _("Explain only")),
            ("A1", _("Prepare a review task")),
            ("A2", _("Bounded administrative execution")),
            ("A3", _("Specific approval for every effect")),
        ),
    )
    tools = forms.MultipleChoiceField(
        label=_("Allowed domain commands"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=(
            ("workflows.review_task", _("Create an operational review checklist")),
            ("comms.synthetic_reminder", _("Simulate one logistics reminder")),
        ),
    )
    principal_id = forms.UUIDField(
        label=_("Owner-provisioned machine principal"),
        required=False,
        help_text=_(
            "Required for bounded or specifically approved execution. "
            "Never a staff user."
        ),
    )
    budget_actions = forms.IntegerField(
        label=_("Daily action limit"),
        min_value=1,
        max_value=1000,
        initial=20,
    )
    budget_micros = forms.IntegerField(
        label=_("Daily simulated cost limit (micro-BRL)"),
        min_value=0,
        max_value=10**15,
        initial=20,
    )
    timeout_seconds = forms.IntegerField(
        label=_("Execution timeout (seconds)"),
        min_value=1,
        max_value=60,
        initial=30,
    )
    compensation = forms.ChoiceField(
        label=_("After a known failure"),
        choices=(
            ("manual", _("Require manual follow-up")),
            ("synthetic_cancel", _("Simulate the approved cancellation")),
            ("none", _("No automatic compensation; require manual follow-up")),
        ),
    )

    def terms(self, capability: str) -> PolicyTerms:
        """Build the closed domain value after form parsing."""
        return PolicyTerms(
            capability=capability,
            autonomy_ceiling=self.cleaned_data["autonomy_ceiling"],
            tools=tuple(self.cleaned_data["tools"]),
            principal_id=self.cleaned_data["principal_id"],
            enabled=self.cleaned_data["enabled"],
            budget_actions=self.cleaned_data["budget_actions"],
            budget_micros=self.cleaned_data["budget_micros"],
            timeout_seconds=self.cleaned_data["timeout_seconds"],
            compensation=self.cleaned_data["compensation"],
        )


class ActionDecisionForm(forms.Form):
    """Selectors and exact-effect identity stay in the POST body."""

    proposal_id = forms.UUIDField()
    digest = forms.RegexField(regex=r"^[0-9a-f]{64}$")
    command = forms.ChoiceField(choices=(("approve", "approve"), ("reject", "reject")))
