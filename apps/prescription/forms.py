"""Explicit bounded entry forms; the class and dose never have defaults."""

from typing import Any

from django import forms
from django.forms import formset_factory
from django.utils.translation import gettext_lazy as _

from apps.prescription.class_values import PrescriptionClass
from apps.prescription.policy import DOSE_UNIT_LABELS, MAX_ITEMS


class PrescriptionDraftForm(forms.Form):
    """Untrusted scope assertions checked against the stored encounter by services."""

    encounter_id = forms.UUIDField(widget=forms.HiddenInput)
    patient_id = forms.UUIDField(widget=forms.HiddenInput)
    issuer_id = forms.UUIDField(widget=forms.HiddenInput)
    draft_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    category = forms.TypedChoiceField(
        label=_("Prescription class"),
        choices=[("", _("Choose a class")), *PrescriptionClass.choices],
        required=False,
        empty_value=None,
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
    quantity = forms.CharField(
        label="Quantidade", max_length=80, strip=False, required=False
    )
    dose_unit = forms.ChoiceField(
        label=_("Dose unit for structured directions"),
        choices=[
            ("", _("Free-text directions")),
            *DOSE_UNIT_LABELS.items(),
        ],
        required=False,
        help_text=_(
            "With a unit, enter numeric dose, administrations per day and days. "
            "Quantity is their exact product in that unit; no conversion or rounding."
        ),
    )
    catalog_entry_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    medication_statement_id = forms.UUIDField(
        label=_("Link to medication reconciliation"),
        required=False,
        widget=forms.Select(choices=[("", _("No reconciliation link"))]),
    )
    instructions = forms.CharField(
        label="Orientações registradas pelo médico",
        max_length=2000,
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def clean(self) -> dict[str, Any]:
        """Keep legacy free text exact; optional links never become defaults."""
        data = self.cleaned_data
        if not data.get("dose_unit") and not data.get("quantity"):
            self.add_error(
                "quantity", _("Enter a quantity or choose structured directions.")
            )
        for field in ("dose_unit", "catalog_entry_id", "medication_statement_id"):
            if data.get(field):
                data[field] = str(data[field])
            else:
                data.pop(field, None)
        return data


PrescriptionItemFormSet = formset_factory(
    PrescriptionItemForm,
    extra=1,
    min_num=1,
    max_num=MAX_ITEMS,
    validate_min=True,
    validate_max=True,
    absolute_max=MAX_ITEMS,
)
