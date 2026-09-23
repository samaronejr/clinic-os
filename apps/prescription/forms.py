"""Explicit bounded entry forms; no dose defaults, normalization or advice."""

from django import forms
from django.forms import formset_factory

from apps.prescription.policy import MAX_ITEMS, SYNTHETIC_CATEGORY


class PrescriptionDraftForm(forms.Form):
    """Untrusted scope assertions checked against the stored encounter by services."""

    encounter_id = forms.UUIDField(widget=forms.HiddenInput)
    patient_id = forms.UUIDField(widget=forms.HiddenInput)
    issuer_id = forms.UUIDField(widget=forms.HiddenInput)
    draft_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    category = forms.ChoiceField(
        label="Categoria do documento",
        choices=[(SYNTHETIC_CATEGORY, "Ensaio sintético não controlado — sem emissão")],
    )


class PrescriptionItemForm(forms.Form):
    """Preserve the exact entered text, including leading and trailing spaces."""

    medication_description = forms.CharField(
        label="Descrição do medicamento", max_length=240, strip=False
    )
    strength_form = forms.CharField(
        label="Concentração e forma", max_length=160, strip=False
    )
    dose = forms.CharField(label="Dose", max_length=160, strip=False)
    route = forms.CharField(label="Via", max_length=80, strip=False)
    frequency = forms.CharField(label="Frequência", max_length=160, strip=False)
    duration = forms.CharField(label="Duração", max_length=160, strip=False)
    quantity = forms.CharField(label="Quantidade", max_length=80, strip=False)
    instructions = forms.CharField(
        label="Orientações registradas pelo médico",
        max_length=2000,
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


PrescriptionItemFormSet = formset_factory(
    PrescriptionItemForm,
    extra=1,
    min_num=1,
    max_num=MAX_ITEMS,
    validate_min=True,
    validate_max=True,
    absolute_max=MAX_ITEMS,
)
