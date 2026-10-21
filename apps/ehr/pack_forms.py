"""Native calculator fields preserve canonical decimal strings and explicit units."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from django.http import QueryDict

    from apps.ehr.calculators.contracts import CalculatorDefinition


def calculator_form(
    definition: CalculatorDefinition, data: QueryDict | None = None
) -> forms.Form:
    """Avoid locale coercion: ambiguous numeric text reaches the pure refusal."""
    form = forms.Form(data, prefix=definition.key)
    for spec in definition.inputs:
        label = _(spec.label)
        if spec.key.startswith("item_"):
            label = f"{label} {spec.key.removeprefix('item_')}"
        if spec.unit:
            label = f"{label} ({_(spec.unit)})"
        field: forms.Field
        if spec.choices:
            field = forms.ChoiceField(
                label=label,
                choices=[
                    ("", _("Choose an option")),
                    *[(v, _(v)) for v in spec.choices],
                ],
            )
        else:
            field = forms.CharField(
                label=label,
                max_length=18,
                strip=False,
                help_text=_("Use a decimal point, without thousands separators."),
            )
        form.fields[spec.key] = field
    return form
