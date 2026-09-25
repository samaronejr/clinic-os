"""Body-only intake forms that keep patient input out of every URL."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.intake.models import (
    CONTACT_CHANNEL_VALUES,
    CONTACT_PURPOSE_VALUES,
    GENDER_IDENTITY_VALUES,
    IDENTIFIER_KIND_VALUES,
    SEX_AT_BIRTH_VALUES,
)
from apps.intake.patient_search import MAX_QUERY_LENGTH, MIN_QUERY_LENGTH

if TYPE_CHECKING:
    from django.http import QueryDict

FIRST_PAGE: Final = 1
MAX_IDENTIFIER_LENGTH: Final = 64

SEX_AT_BIRTH_LABELS: Final = {
    "female": _("Female"),
    "intersex": _("Intersex"),
    "male": _("Male"),
    "not_informed": _("Not informed"),
}
GENDER_IDENTITY_LABELS: Final = {
    "man": _("Man"),
    "non_binary": _("Non-binary"),
    "not_informed": _("Not informed"),
    "other": _("Other"),
    "woman": _("Woman"),
}
INVALID_CREATE_MESSAGE: Final = _("Enter a patient name and a valid birth date.")
CONFLICTING_KEY_MESSAGE: Final = _(
    "This registration was already submitted differently."
)
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
    """Collect one registry search or exact-identifier lookup from the body.

    When ``identifier_value`` is present the form performs an exact
    identifier search instead of a name search: ``q`` and ``birth_date``
    are ignored and ``identifier_kind`` becomes required.
    """

    q = forms.CharField(
        label=_("Patient name"),
        min_length=MIN_QUERY_LENGTH,
        max_length=MAX_QUERY_LENGTH,
        strip=True,
        required=False,
    )
    birth_date = forms.DateField(
        label=_("Birth date (optional)"),
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    identifier_kind = forms.ChoiceField(
        label=_("Document type"),
        choices=tuple((value, value.upper()) for value in IDENTIFIER_KIND_VALUES),
        required=False,
    )
    identifier_value = forms.CharField(
        label=_("Document number (exact)"),
        max_length=MAX_IDENTIFIER_LENGTH,
        strip=True,
        required=False,
    )
    page = forms.IntegerField(
        label=_("Results page"),
        min_value=FIRST_PAGE,
        required=False,
        widget=forms.HiddenInput(),
    )

    def __init__(self, data: QueryDict | None = None) -> None:
        """Attach the accessible descriptions this screen always renders."""
        super().__init__(data=data)
        _describe(self, SEARCH_DESCRIPTIONS)
        self.fields["identifier_value"].widget.attrs.update(
            {
                "autocomplete": "off",
                "aria-describedby": "patient-search-identifier-help",
            }
        )

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        _bind_error_descriptions(self, SEARCH_DESCRIPTIONS)

    def clean(self) -> dict[str, object]:
        """Require a name unless an exact identifier lookup was requested."""
        cleaned = super().clean()
        if cleaned is None:
            return {}
        identifier_value = cleaned.get("identifier_value")
        if identifier_value:
            if not cleaned.get("identifier_kind"):
                self.add_error(
                    "identifier_kind",
                    ValidationError(_("Choose the document type."), code="required"),
                )
            return cleaned
        if not cleaned.get("q"):
            self.add_error(
                "q",
                ValidationError(
                    _("Enter a name or a document number to search."),
                    code="required",
                ),
            )
        return cleaned

    def selected_page(self) -> int:
        """Return the requested page, defaulting to the first result page."""
        page = self.cleaned_data.get("page")
        return page if isinstance(page, int) else FIRST_PAGE

    def identifier_search(self) -> tuple[str, str] | None:
        """Return ``(kind, value)`` when the form is an identifier lookup."""
        kind = self.cleaned_data.get("identifier_kind")
        value = self.cleaned_data.get("identifier_value")
        if not kind or not value:
            return None
        return str(kind), str(value)


class EnrollmentForm(forms.Form):
    """Bind one clinic enrollment carried in the POST body."""

    enrollment_id = forms.UUIDField(widget=forms.HiddenInput())

    def selected_enrollment(self) -> UUID:
        """Return the validated enrollment identifier."""
        enrollment_id = self.cleaned_data["enrollment_id"]
        if not isinstance(enrollment_id, UUID):
            raise ValidationError(_("Enter a valid enrollment."), code="invalid")
        return enrollment_id


class ContactChannelForm(EnrollmentForm):
    """Select one contact channel for the edit or verify action."""

    channel = forms.ChoiceField(
        choices=tuple((value, value) for value in CONTACT_CHANNEL_VALUES),
        widget=forms.HiddenInput(),
    )

    def selected_channel(self) -> str:
        """Return the validated channel value."""
        return str(self.cleaned_data["channel"])


class ContactVerifyForm(ContactChannelForm):
    """Bind the destination version the manage screen rendered.

    Verification attests to the exact destination the operator saw, so
    the action carries that version and the service rejects a stale
    form instead of verifying an unseen replacement.
    """

    expected_version = forms.IntegerField(
        min_value=1,
        widget=forms.HiddenInput(),
    )

    def selected_version(self) -> int:
        """Return the rendered destination version."""
        version = self.cleaned_data["expected_version"]
        if not isinstance(version, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return version


class ContactDestinationForm(ContactChannelForm):
    """Collect one destination and the version the edit screen rendered."""

    destination = forms.CharField(
        label=_("Destination"),
        max_length=255,
        strip=True,
    )
    expected_version = forms.IntegerField(
        min_value=0,
        widget=forms.HiddenInput(),
    )

    def __init__(self, data: QueryDict | dict[str, str] | None = None) -> None:
        """Attach the accessible description this screen always renders."""
        super().__init__(data=data)
        self.fields["destination"].widget.attrs.update(
            {"autocomplete": "off", "aria-describedby": "contact-destination-help"}
        )

    def full_clean(self) -> None:
        """Connect a bound destination error to its rendered description."""
        super().full_clean()
        description_ids = ["contact-destination-help"]
        if "destination" in self.errors:
            description_ids.append(f"{self['destination'].auto_id}_error")
        self.fields["destination"].widget.attrs["aria-describedby"] = " ".join(
            description_ids
        )

    def selected_version(self) -> int | None:
        """Return the rendered version; zero means no contact existed."""
        version = self.cleaned_data["expected_version"]
        if not isinstance(version, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return version if version > 0 else None


class ContactPreferenceForm(EnrollmentForm):
    """Choose one channel for a purpose, or revoke it with an empty value."""

    purpose = forms.ChoiceField(
        choices=tuple((value, value) for value in CONTACT_PURPOSE_VALUES),
        widget=forms.HiddenInput(),
    )
    channel = forms.ChoiceField(
        choices=tuple((value, value) for value in CONTACT_CHANNEL_VALUES),
        required=False,
        widget=forms.HiddenInput(),
    )

    def selected_purpose(self) -> str:
        """Return the validated purpose value."""
        return str(self.cleaned_data["purpose"])

    def selected_channel(self) -> str | None:
        """Return the chosen channel, or ``None`` for a revocation."""
        channel = self.cleaned_data.get("channel")
        return str(channel) if channel else None


class PatientCreateForm(forms.Form):
    """Collect one idempotent patient registration from the POST body."""

    full_name = forms.CharField(label=_("Full name"), max_length=255, strip=True)
    birth_date = forms.DateField(
        label=_("Birth date"),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    social_name = forms.CharField(
        label=_("Social name (optional)"),
        max_length=255,
        strip=True,
        required=False,
    )
    identifier_kind = forms.ChoiceField(
        label=_("Document type"),
        choices=tuple((value, value.upper()) for value in IDENTIFIER_KIND_VALUES),
        required=False,
    )
    identifier_value = forms.CharField(
        label=_("Document number (optional)"),
        max_length=MAX_IDENTIFIER_LENGTH,
        strip=True,
        required=False,
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

    def clean(self) -> dict[str, object]:
        """Require the document type when a document number was entered."""
        cleaned = super().clean()
        if cleaned is None:
            return {}
        if cleaned.get("identifier_value") and not cleaned.get("identifier_kind"):
            self.add_error(
                "identifier_kind",
                ValidationError(_("Choose the document type."), code="required"),
            )
        return cleaned


class DemographicsForm(EnrollmentForm):
    """Collect one versioned demographics correction from the POST body."""

    social_name = forms.CharField(
        label=_("Social name"), max_length=255, strip=True, required=False
    )
    preferred_name = forms.CharField(
        label=_("Preferred name"), max_length=255, strip=True, required=False
    )
    sex_at_birth = forms.ChoiceField(
        label=_("Sex at birth"),
        choices=(
            ("", _("Not recorded")),
            *[(value, value) for value in SEX_AT_BIRTH_VALUES],
        ),
        required=False,
    )
    gender_identity = forms.ChoiceField(
        label=_("Gender identity"),
        choices=(
            ("", _("Not recorded")),
            *[(value, value) for value in GENDER_IDENTITY_VALUES],
        ),
        required=False,
    )
    pronouns = forms.CharField(
        label=_("Pronouns"), max_length=64, strip=True, required=False
    )
    language = forms.CharField(
        label=_("Preferred language"), max_length=64, strip=True, required=False
    )
    accessibility_needs = forms.CharField(
        label=_("Accessibility needs"),
        max_length=255,
        strip=True,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    occupation = forms.CharField(
        label=_("Occupation"), max_length=255, strip=True, required=False
    )
    reason = forms.CharField(
        label=_("Correction reason"), max_length=512, strip=True, required=False
    )
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput())

    def selected_version(self) -> int:
        """Return the demographics version the form rendered."""
        version = self.cleaned_data["expected_version"]
        if not isinstance(version, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return version

    def cleaned_changes(self) -> dict[str, str]:
        """Return the field map the correction carries into the service."""
        return {
            field: str(self.cleaned_data.get(field) or "")
            for field in (
                "social_name",
                "preferred_name",
                "sex_at_birth",
                "gender_identity",
                "pronouns",
                "language",
                "accessibility_needs",
                "occupation",
            )
        }


class IdentifierAddForm(EnrollmentForm):
    """Collect one new identifier for the enrolled patient."""

    kind = forms.ChoiceField(
        label=_("Document type"),
        choices=tuple((value, value.upper()) for value in IDENTIFIER_KIND_VALUES),
    )
    value = forms.CharField(label=_("Document number"), max_length=64, strip=True)
    issuer = forms.CharField(
        label=_("Issuing body (optional)"),
        max_length=255,
        strip=True,
        required=False,
    )

    def selected_kind(self) -> str:
        """Return the validated identifier kind."""
        return str(self.cleaned_data["kind"])


class IdentifierRetireForm(EnrollmentForm):
    """Bind the kind and rendered version of an identifier retirement."""

    kind = forms.ChoiceField(
        choices=tuple((value, value.upper()) for value in IDENTIFIER_KIND_VALUES),
        widget=forms.HiddenInput(),
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput())

    def selected_kind(self) -> str:
        """Return the validated identifier kind."""
        return str(self.cleaned_data["kind"])

    def selected_version(self) -> int:
        """Return the rendered identifier version."""
        version = self.cleaned_data["expected_version"]
        if not isinstance(version, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return version


class AccessRevokeForm(EnrollmentForm):
    """Bind the grant a staff revocation targets."""

    grant_id = forms.UUIDField(widget=forms.HiddenInput())

    def selected_grant(self) -> UUID:
        """Return the validated grant identifier."""
        grant_id = self.cleaned_data["grant_id"]
        if not isinstance(grant_id, UUID):
            raise ValidationError(_("Enter a valid invitation."), code="invalid")
        return grant_id


class PatientAccessForm(forms.Form):
    """Collect one invitation code from the redemption POST body."""

    code = forms.CharField(
        label=_("Access code"),
        min_length=1,
        max_length=128,
        strip=True,
    )

    def __init__(self, data: QueryDict | None = None) -> None:
        """Attach the accessible description this screen always renders."""
        super().__init__(data=data)
        self.fields["code"].widget.attrs.update(
            {
                "autocomplete": "off",
                "aria-describedby": "patient-access-help",
                "inputmode": "text",
            }
        )

    def full_clean(self) -> None:
        """Connect a bound code error to its rendered description."""
        super().full_clean()
        description_ids = ["patient-access-help"]
        if "code" in self.errors:
            description_ids.append(f"{self['code'].auto_id}_error")
        self.fields["code"].widget.attrs["aria-describedby"] = " ".join(description_ids)

    def selected_code(self) -> str:
        """Return the submitted code exactly as typed."""
        return str(self.cleaned_data["code"])
