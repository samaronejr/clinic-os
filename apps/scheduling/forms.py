"""Body-only availability forms bound to resolver-approved practitioners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django import forms

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.http import QueryDict

BLANK_CHOICE: Final = ("", "Select a physician")
INVALID_CREATE_MESSAGE: Final = (
    "Enter a future clinic-local window that ends after it starts."
)
CONFLICTING_KEY_MESSAGE: Final = "This availability was already submitted differently."
OVERLAP_MESSAGE: Final = "That physician already promises part of this window."
PRACTITIONER_MESSAGE: Final = "Select an active physician for this clinic."
DEPENDENT_MESSAGE: Final = (
    "This block still has future appointments and cannot be retired."
)
CREATE_DESCRIPTIONS: Final = {
    "practitioner": "availability-practitioner-help",
    "local_date": "availability-date-help",
    "start_time": "availability-start-help",
    "end_time": "availability-end-help",
}


def _describe(form: forms.Form, descriptions: dict[str, str]) -> None:
    for field_name, description in descriptions.items():
        form.fields[field_name].widget.attrs.update(
            {"autocomplete": "off", "aria-describedby": description}
        )


def _bind_error_descriptions(form: forms.Form, descriptions: dict[str, str]) -> None:
    for field_name, description in descriptions.items():
        description_ids = [description]
        if field_name in form.errors:
            description_ids.append(f"{form[field_name].auto_id}_error")
        form.fields[field_name].widget.attrs["aria-describedby"] = " ".join(
            description_ids
        )


class AvailabilityCreateForm(forms.Form):
    """Collect one idempotent clinic-local availability window from the body."""

    practitioner = forms.ChoiceField(label="Physician")
    local_date = forms.DateField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    start_time = forms.TimeField(
        label="Start time",
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    end_time = forms.TimeField(
        label="End time",
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())

    def __init__(
        self,
        choices: Sequence[tuple[str, str]],
        data: QueryDict | None = None,
    ) -> None:
        """Bind the resolver-approved physician choices this clinic exposes."""
        super().__init__(data=data)
        field = self.fields["practitioner"]
        if isinstance(field, forms.ChoiceField):
            field.choices = [BLANK_CHOICE, *choices]
        _describe(self, CREATE_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        _bind_error_descriptions(self, CREATE_DESCRIPTIONS)

    def selected_practitioner(self) -> UUID:
        """Return the resolver-approved practitioner the body selected."""
        return UUID(str(self.cleaned_data["practitioner"]))

    def local_window(self) -> tuple[str, str]:
        """Return the submitted clinic-local start and end minute strings."""
        day = self.cleaned_data["local_date"].isoformat()
        start = self.cleaned_data["start_time"].strftime("%H:%M")
        end = self.cleaned_data["end_time"].strftime("%H:%M")
        return f"{day}T{start}", f"{day}T{end}"
