"""Bounded native POST contracts; patient selectors never become URL parameters."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.ai.brief_contracts import MAX_QUESTION


class ChartQuestionForm(forms.Form):
    """One patient selector, one isolated turn, and no caller-chosen authority."""

    enrollment_id = forms.UUIDField(required=False)
    encounter_id = forms.UUIDField(required=False)
    command = forms.ChoiceField(
        choices=(("generate", "generate"), ("ask", "ask"), ("display", "display"))
    )
    question = forms.CharField(max_length=MAX_QUESTION, required=False)
    invocation_id = forms.UUIDField(required=False)
    surface = forms.ChoiceField(
        choices=(("today", "today"), ("workspace", "workspace"))
    )

    def clean(self) -> dict[str, object]:
        """Reject ambiguous selectors and incomplete commands without echoing input."""
        data: dict[str, object] = super().clean() or {}
        if (
            bool(data.get("enrollment_id")) == bool(data.get("encounter_id"))
            or (data.get("command") == "ask" and not data.get("question"))
            or (data.get("command") == "display" and not data.get("invocation_id"))
            or (data.get("command") != "display" and data.get("invocation_id"))
            or (data.get("command") != "ask" and data.get("question"))
        ):
            raise forms.ValidationError(
                _("Choose one record and a valid chart question.")
            )
        return data
