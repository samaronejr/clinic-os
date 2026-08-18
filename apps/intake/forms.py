"""Body-only intake forms that keep patient input out of every URL."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django import forms

from apps.intake.patient_search import MAX_QUERY_LENGTH, MIN_QUERY_LENGTH

if TYPE_CHECKING:
    from django.http import QueryDict

FIRST_PAGE: Final = 1
INVALID_CREATE_MESSAGE: Final = "Enter a patient name and a valid birth date."
CONFLICTING_KEY_MESSAGE: Final = "This registration was already submitted differently."
SEARCH_DESCRIPTIONS: Final = {
    "q": "patient-search-help",
    "birth_date": "patient-search-birth-date-help",
}
CREATE_DESCRIPTIONS: Final = {
    "full_name": "patient-create-name-help",
    "birth_date": "patient-create-birth-date-help",
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


class PatientSearchForm(forms.Form):
    """Collect one deterministic registry selection from the POST body."""

    q = forms.CharField(
        label="Patient name",
        min_length=MIN_QUERY_LENGTH,
        max_length=MAX_QUERY_LENGTH,
        strip=True,
    )
    birth_date = forms.DateField(
        label="Birth date (optional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    page = forms.IntegerField(
        label="Results page",
        min_value=FIRST_PAGE,
        required=False,
        widget=forms.HiddenInput(),
    )

    def __init__(self, data: QueryDict | None = None) -> None:
        """Attach the accessible descriptions this screen always renders."""
        super().__init__(data=data)
        _describe(self, SEARCH_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        _bind_error_descriptions(self, SEARCH_DESCRIPTIONS)

    def selected_page(self) -> int:
        """Return the requested page, defaulting to the first result page."""
        page = self.cleaned_data.get("page")
        return page if isinstance(page, int) else FIRST_PAGE


class PatientCreateForm(forms.Form):
    """Collect one idempotent patient registration from the POST body."""

    full_name = forms.CharField(label="Full name", max_length=255, strip=True)
    birth_date = forms.DateField(
        label="Birth date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())

    def __init__(self, data: QueryDict | None = None) -> None:
        """Attach the accessible descriptions this screen always renders."""
        super().__init__(data=data)
        _describe(self, CREATE_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        _bind_error_descriptions(self, CREATE_DESCRIPTIONS)
