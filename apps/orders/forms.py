"""Native result forms; selectors and source identity stay in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.orders.values import CATALOG

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class OrderForm(forms.Form):
    """Explicit patient and catalog choices, without clinical finding defaults."""

    enrollment_id = forms.ChoiceField(label=_("Patient"))
    exam_codes = forms.MultipleChoiceField(
        label=_("Requested examinations"), choices=tuple(CATALOG.items())
    )
    command_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)

    def __init__(
        self,
        patients: Sequence[tuple[str, str]],
        data: Mapping[str, str] | None = None,
    ) -> None:
        """Bind choices from the currently authorized clinic."""
        super().__init__(data=data)
        self.fields["enrollment_id"] = forms.ChoiceField(
            label=_("Patient"), choices=(("", _("Choose a patient")), *patients)
        )


class UploadForm(forms.Form):
    """Keep source identity independent of the selected patient's chart."""

    order_id = forms.UUIDField(widget=forms.HiddenInput)
    event_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)
    result_id = forms.UUIDField(widget=forms.HiddenInput, required=False)
    predecessor_id = forms.UUIDField(widget=forms.HiddenInput, required=False)
    name = forms.CharField(
        label=_("Name on the result"), max_length=256, required=False
    )
    birth_date = forms.DateField(
        label=_("Date of birth on the result"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    identifier_kind = forms.ChoiceField(
        label=_("Identifier kind on the result"),
        required=False,
        choices=(
            ("", _("Not provided")),
            ("cpf", "CPF"),
            ("cns", "CNS"),
            ("rg", "RG"),
            ("passport", _("Passport")),
            ("other", _("Other")),
        ),
    )
    identifier = forms.CharField(
        label=_("Identifier on the result"), max_length=256, required=False
    )
    source = forms.FileField(
        label=_("Original PDF or image"),
        widget=forms.FileInput(
            attrs={"accept": "application/pdf,image/jpeg,image/png"}
        ),
    )
    critical = forms.BooleanField(
        label=_("Source explicitly marks this result critical"), required=False
    )
    value = forms.CharField(
        label=_("Value exactly as printed"), max_length=128, required=False
    )
    observation_code = forms.ChoiceField(
        label=_("Examination for this source value"),
        required=False,
        choices=(
            ("", _("Choose the examination printed on the source")),
            *CATALOG.items(),
        ),
    )
    unit = forms.CharField(
        label=_("Unit exactly as printed"), max_length=64, required=False
    )
    reference_interval = forms.CharField(
        label=_("Reference interval exactly as printed"), max_length=256, required=False
    )
    abnormal_flag = forms.ChoiceField(
        label=_("Abnormal flag from source"),
        required=False,
        choices=(
            ("", _("Not provided")),
            ("normal", _("Normal")),
            ("low", _("Low")),
            ("high", _("High")),
            ("critical", _("Critical")),
        ),
    )
