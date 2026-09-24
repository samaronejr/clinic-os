"""Native explicit choices; consent is never preselected."""

from django import forms

from apps.consent.models import ConsentText
from apps.consent.services import MAX_TEXT


class AcceptanceForm(forms.Form):
    """A session-bound displayed version and a required deliberate choice."""

    offer = forms.CharField(widget=forms.HiddenInput)
    purpose = forms.ChoiceField(
        choices=ConsentText.Purpose.choices, widget=forms.HiddenInput
    )
    accepted = forms.BooleanField(
        label="Li o texto desta versão e aceito a teleconsulta.", required=True
    )


class TextForm(forms.Form):
    """Publish a new overlay without editing any prior accepted text."""

    purpose = forms.ChoiceField(label="Finalidade", choices=ConsentText.Purpose.choices)
    text = forms.CharField(
        label="Texto integral (pt-BR)",
        strip=False,
        max_length=MAX_TEXT,
        widget=forms.Textarea(attrs={"rows": 10}),
    )
