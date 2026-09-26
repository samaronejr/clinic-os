"""Body-only intake forms that keep patient input out of every URL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.intake.demographics import (
    DEMOGRAPHICS_FIELD_VALUES,
    STATUS_FIELDS,
)
from apps.intake.models import (
    ADDRESS_KIND_VALUES,
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

# Stored values are stable codes; every label a person reads is translated.
SEX_AT_BIRTH_LABELS: Final = {
    "female": _("Female"),
    "intersex": _("Intersex"),
    "male": _("Male"),
    "not_informed": _("Not informed"),
    "declined": _("Declined to answer"),
}
GENDER_IDENTITY_LABELS: Final = {
    "woman": _("Woman"),
    "man": _("Man"),
    "non_binary": _("Non-binary"),
    "other": pgettext_lazy("gender identity", "Other"),
    "not_informed": _("Not informed"),
    "declined": _("Declined to answer"),
}
UNKNOWN_STATUS_LABELS: Final = {
    "not_informed": _("Not informed"),
    "declined": _("Declined to answer"),
}
IDENTIFIER_KIND_LABELS: Final = {
    "cpf": _("CPF"),
    "cns": _("CNS"),
    "rg": _("RG"),
    "passport": _("Passport"),
    "other": _("Other document"),
}
ADDRESS_KIND_LABELS: Final = {
    "home": _("Home"),
    "work": _("Work"),
    "other": pgettext_lazy("address kind", "Other"),
}
DEMOGRAPHIC_FIELD_LABELS: Final = {
    "legal_name": _("Legal name"),
    "social_name": _("Social name"),
    "preferred_name": _("Preferred name"),
    "birth_date": _("Birth date"),
    "sex_at_birth": _("Sex at birth"),
    "gender_identity": _("Gender identity"),
    "pronouns": _("Pronouns"),
    "language": _("Preferred language"),
    "accessibility_needs": _("Accessibility needs"),
    "occupation": _("Occupation"),
}
UF_VALUES: Final = (
    "AC",
    "AL",
    "AM",
    "AP",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MG",
    "MS",
    "MT",
    "PA",
    "PB",
    "PE",
    "PI",
    "PR",
    "RJ",
    "RN",
    "RO",
    "RR",
    "RS",
    "SC",
    "SE",
    "SP",
    "TO",
)
INVALID_CREATE_MESSAGE: Final = _(
    "Check the registration fields: the birth date cannot be in the future "
    "and the document number must be valid."
)
NAME_REQUIRED_MESSAGE: Final = _("Enter the legal name or the social name.")
VALUE_AND_REASON_MESSAGE: Final = _(
    "Leave the field empty to record why it was not informed."
)
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
        choices=tuple(IDENTIFIER_KIND_LABELS.items()),
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


def _status_field(field: str) -> forms.ChoiceField:
    """Build the companion "why is it empty" select for one field."""
    return forms.ChoiceField(
        label=_("%(field)s: reason when empty")
        % {"field": DEMOGRAPHIC_FIELD_LABELS[field]},
        choices=(("", _("Not applicable")), *UNKNOWN_STATUS_LABELS.items()),
        required=False,
    )


def _value_field(field: str) -> forms.Field:
    """Build the input for one demographic field with its translated label."""
    label = DEMOGRAPHIC_FIELD_LABELS[field]
    if field == "birth_date":
        return forms.DateField(
            label=label,
            required=False,
            widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        )
    if field in ("sex_at_birth", "gender_identity"):
        labels = (
            SEX_AT_BIRTH_LABELS if field == "sex_at_birth" else GENDER_IDENTITY_LABELS
        )
        allowed = (
            SEX_AT_BIRTH_VALUES if field == "sex_at_birth" else GENDER_IDENTITY_VALUES
        )
        return forms.ChoiceField(
            label=label,
            choices=(
                ("", _("Not recorded")),
                *((value, labels[value]) for value in labels if value in allowed),
            ),
            required=False,
        )
    if field == "accessibility_needs":
        return forms.CharField(
            label=label,
            max_length=255,
            strip=True,
            required=False,
            widget=forms.Textarea(attrs={"rows": 3}),
        )
    max_length = 64 if field in ("pronouns", "language") else 255
    return forms.CharField(
        label=label, max_length=max_length, strip=True, required=False
    )


class DemographicFieldsMixin:
    """Pair demographic inputs with their deliberate-non-answer selects."""

    fields: dict[str, forms.Field]
    cleaned_data: dict[str, object]
    demographic_fields: tuple[str, ...] = ()

    def _add_demographic_fields(self, names: tuple[str, ...]) -> None:
        self.demographic_fields = names
        for field in names:
            if field not in self.fields:
                self.fields[field] = _value_field(field)
            if field in STATUS_FIELDS:
                self.fields[f"{field}_status"] = _status_field(field)

    def _check_value_or_reason(self, form: forms.Form) -> None:
        for field in self.demographic_fields:
            status = self.cleaned_data.get(f"{field}_status")
            if status and self.cleaned_data.get(field) not in (None, ""):
                form.add_error(
                    f"{field}_status",
                    ValidationError(VALUE_AND_REASON_MESSAGE, code="conflict"),
                )

    def field_groups(self) -> list[tuple[forms.BoundField, forms.BoundField | None]]:
        """Return each value field with its status select, in form order."""
        form = cast("forms.Form", self)
        return [
            (
                form[field],
                form[f"{field}_status"] if f"{field}_status" in self.fields else None,
            )
            for field in self.demographic_fields
        ]

    def cleaned_unknown(self) -> dict[str, str]:
        """Return the deliberate non-answers the form records."""
        return {
            field: str(self.cleaned_data.get(f"{field}_status") or "")
            for field in self.demographic_fields
            if field in STATUS_FIELDS
        }

    def cleaned_values(self, names: tuple[str, ...]) -> dict[str, object]:
        """Return field values as the service expects (``""`` clears)."""
        values: dict[str, object] = {}
        for field in names:
            value = self.cleaned_data.get(field)
            values[field] = value if isinstance(value, date) else str(value or "")
        return values


@dataclass(frozen=True, slots=True)
class RegistrationInputs:
    """The validated registration values ``register_patient`` takes."""

    idempotency_key: UUID
    legal_name: str
    social_name: str
    birth_date: date | None
    changes: dict[str, object]
    unknown: dict[str, str]
    identifier_kind: str
    identifier_value: str


class PatientCreateForm(DemographicFieldsMixin, forms.Form):
    """Collect one idempotent patient registration from the POST body.

    Only a legal or a social name is structurally needed; the birth date and
    everything else is optional unless the clinic intake policy requires it,
    and a required field may be answered with an explicit non-answer.
    ``full_name`` keeps its historical form name and carries the legal name.
    """

    full_name = forms.CharField(
        label=_("Legal name"), max_length=255, strip=True, required=False
    )
    social_name = forms.CharField(
        label=_("Social name"),
        max_length=255,
        strip=True,
        required=False,
    )
    birth_date = forms.DateField(
        label=_("Birth date"),
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    identifier_kind = forms.ChoiceField(
        label=_("Document type"),
        choices=tuple(IDENTIFIER_KIND_LABELS.items()),
        required=False,
    )
    identifier_value = forms.CharField(
        label=_("Document number"),
        max_length=MAX_IDENTIFIER_LENGTH,
        strip=True,
        required=False,
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())

    def __init__(
        self,
        data: QueryDict | None = None,
        *,
        required_fields: tuple[str, ...] = (),
    ) -> None:
        """Add the policy-required fields next to the registration basics."""
        super().__init__(data=data)
        self.required_fields = required_fields
        extra = tuple(
            field
            for field in DEMOGRAPHICS_FIELD_VALUES
            if field in required_fields
            and field not in ("legal_name", "social_name", "birth_date")
        )
        self.fields["full_name_status"] = _status_field("legal_name")
        self.fields["birth_date_status"] = _status_field("birth_date")
        self._add_demographic_fields(extra)
        _describe(self, CREATE_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        _bind_error_descriptions(self, CREATE_DESCRIPTIONS)

    def clean(self) -> dict[str, object]:
        """Require a name and a document type, never both value and reason."""
        cleaned = super().clean()
        if cleaned is None:
            return {}
        if not cleaned.get("full_name") and not cleaned.get("social_name"):
            self.add_error(
                "full_name",
                ValidationError(NAME_REQUIRED_MESSAGE, code="required"),
            )
        if cleaned.get("identifier_value") and not cleaned.get("identifier_kind"):
            self.add_error(
                "identifier_kind",
                ValidationError(_("Choose the document type."), code="required"),
            )
        for field, status in (
            ("full_name", "full_name_status"),
            ("birth_date", "birth_date_status"),
        ):
            if cleaned.get(status) and cleaned.get(field):
                self.add_error(
                    status, ValidationError(VALUE_AND_REASON_MESSAGE, code="conflict")
                )
        self._check_value_or_reason(self)
        return cleaned

    def registration_inputs(self) -> RegistrationInputs:
        """Return the values ``register_patient`` takes."""
        data = self.cleaned_data
        birth_date = data.get("birth_date")
        idempotency_key = data["idempotency_key"]
        if not isinstance(idempotency_key, UUID):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        unknown = self.cleaned_unknown()
        unknown["legal_name"] = str(data.get("full_name_status") or "")
        unknown["birth_date"] = str(data.get("birth_date_status") or "")
        return RegistrationInputs(
            idempotency_key=idempotency_key,
            legal_name=str(data.get("full_name") or ""),
            social_name=str(data.get("social_name") or ""),
            birth_date=birth_date if isinstance(birth_date, date) else None,
            changes=self.cleaned_values(self.demographic_fields),
            unknown={field: status for field, status in unknown.items() if status},
            identifier_kind=str(data.get("identifier_kind") or ""),
            identifier_value=str(data.get("identifier_value") or ""),
        )


class DemographicsForm(DemographicFieldsMixin, EnrollmentForm):
    """Collect one versioned demographics correction from the POST body."""

    reason = forms.CharField(
        label=_("Correction reason"), max_length=512, strip=True, required=False
    )
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput())

    def __init__(self, data: QueryDict | dict[str, object] | None = None) -> None:
        """Add every demographic field with its non-answer select."""
        super().__init__(data=data)
        self._add_demographic_fields(DEMOGRAPHICS_FIELD_VALUES)
        # Keep the correction reason last in the rendered order.
        self.fields["reason"] = self.fields.pop("reason")

    def clean(self) -> dict[str, object]:
        """Never accept both a value and a reason it is empty."""
        cleaned = super().clean()
        if cleaned is None:
            return {}
        self._check_value_or_reason(self)
        return cleaned

    def selected_version(self) -> int:
        """Return the demographics version the form rendered."""
        version = self.cleaned_data["expected_version"]
        if not isinstance(version, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return version

    def cleaned_changes(self) -> dict[str, object]:
        """Return the field map the correction carries into the service."""
        return self.cleaned_values(DEMOGRAPHICS_FIELD_VALUES)


class IdentifierAddForm(EnrollmentForm):
    """Collect one new identifier for the enrolled patient."""

    kind = forms.ChoiceField(
        label=_("Document type"),
        choices=tuple(IDENTIFIER_KIND_LABELS.items()),
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
        choices=tuple((value, value) for value in IDENTIFIER_KIND_VALUES),
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


class SectionForm(EnrollmentForm):
    """Common body of one address/contact/membership save or retirement."""

    section_fields: tuple[str, ...] = ()
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput())

    def selected_version(self) -> int:
        """Return the rendered row version (0 creates into an empty slot)."""
        version = self.cleaned_data["expected_version"]
        if not isinstance(version, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return version

    def section_values(self) -> dict[str, object]:
        """Return the entered values as the service expects them."""
        values: dict[str, object] = {}
        for field in self.section_fields:
            value = self.cleaned_data.get(field)
            values[field] = (
                value if isinstance(value, date) or value is None else str(value)
            )
            if values[field] == "":
                values[field] = None
        return values


class AddressForm(SectionForm):
    """Collect one address of a kind."""

    section_fields = (
        "postal_code",
        "street",
        "street_number",
        "complement",
        "district",
        "city",
        "state_code",
    )
    kind = forms.ChoiceField(
        label=_("Address type"), choices=tuple(ADDRESS_KIND_LABELS.items())
    )
    postal_code = forms.CharField(
        label=_("CEP"), max_length=9, strip=True, required=False
    )
    street = forms.CharField(
        label=_("Street"), max_length=255, strip=True, required=False
    )
    street_number = forms.CharField(
        label=_("Number"), max_length=32, strip=True, required=False
    )
    complement = forms.CharField(
        label=_("Complement"), max_length=255, strip=True, required=False
    )
    district = forms.CharField(
        label=_("District"), max_length=255, strip=True, required=False
    )
    city = forms.CharField(label=_("City"), max_length=255, strip=True, required=False)
    state_code = forms.ChoiceField(
        label=pgettext_lazy("address", "State"),
        choices=(("", _("Not recorded")), *((uf, uf) for uf in UF_VALUES)),
        required=False,
    )

    def selected_kind(self) -> str:
        """Return the validated address kind."""
        kind = str(self.cleaned_data["kind"])
        if kind not in ADDRESS_KIND_VALUES:
            raise ValidationError(_("Choose the address type."), code="invalid")
        return kind


class _SlotForm(SectionForm):
    sequence = forms.TypedChoiceField(
        coerce=int,
        choices=((1, "1"), (2, "2"), (3, "3")),
        widget=forms.HiddenInput(),
    )

    def selected_sequence(self) -> int:
        """Return the validated slot number."""
        sequence = self.cleaned_data["sequence"]
        if not isinstance(sequence, int):
            raise ValidationError(_("Enter a valid version."), code="invalid")
        return sequence


class EmergencyContactForm(_SlotForm):
    """Collect one emergency contact slot."""

    section_fields = ("name", "relationship", "phone")
    name = forms.CharField(
        label=_("Contact name"), max_length=255, strip=True, required=False
    )
    relationship = forms.CharField(
        label=_("Relationship"), max_length=255, strip=True, required=False
    )
    phone = forms.CharField(label=_("Phone"), max_length=32, strip=True, required=False)


class MembershipForm(_SlotForm):
    """Collect one insurance membership slot."""

    section_fields = (
        "payer_name",
        "ans_number",
        "membership_number",
        "plan_name",
        "valid_until",
    )
    payer_name = forms.CharField(
        label=_("Health plan operator"), max_length=255, strip=True, required=False
    )
    ans_number = forms.CharField(
        label=_("ANS registration"), max_length=32, strip=True, required=False
    )
    membership_number = forms.CharField(
        label=_("Card number"), max_length=64, strip=True, required=False
    )
    plan_name = forms.CharField(
        label=_("Plan"), max_length=255, strip=True, required=False
    )
    valid_until = forms.DateField(
        label=_("Valid until"),
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )


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
