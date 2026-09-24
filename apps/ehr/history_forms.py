"""Native explicit history input, with no blank-to-absence conversion."""

from django import forms

from apps.ehr.models import HistoryAssessment, Problem


class HistoryForm(forms.Form):
    """Bind record identity and expected revision to this tab's submitted body."""

    encounter_id = forms.UUIDField(widget=forms.HiddenInput)
    kind = forms.ChoiceField(choices=HistoryAssessment.Kind, widget=forms.HiddenInput)
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    entry_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    state = forms.ChoiceField(
        label="Situação da avaliação", choices=HistoryAssessment.State
    )
    description = forms.CharField(
        label="Descrição registrada pelo médico",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    status = forms.ChoiceField(
        label="Estado do registro",
        required=False,
        choices=[("", "Sem registro individual"), *Problem.Status.choices],
    )
    reason = forms.CharField(label="Motivo do registro ou da alteração", max_length=255)
