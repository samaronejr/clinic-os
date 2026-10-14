"""Accessible POST-only patient search and registration screens."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django import forms
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import bind_patient_context
from apps.identity.otp import privileged_totp_required
from apps.intake.access import authorized_manager_clinic
from apps.intake.alias_access import patient_alias_overview, review_actor
from apps.intake.demographics import (
    DemographicsInputError,
    DemographicsNameRequiredError,
    DemographicsRequiredError,
    DemographicsStaleError,
    IdentifierConflictError,
    IdentifierInputError,
    add_identifier,
    billing_demographics,
    demographics_profile,
    register_patient,
    registration_required_fields,
    retire_identifier,
    save_emergency_contact,
    save_insurance_membership,
    save_patient_address,
    search_patient_identifiers,
    update_demographics,
)
from apps.intake.forms import (
    ADDRESS_KIND_LABELS,
    CONFLICTING_KEY_MESSAGE,
    DEMOGRAPHIC_FIELD_LABELS,
    IDENTIFIER_KIND_LABELS,
    INVALID_CREATE_MESSAGE,
    AccessRevokeForm,
    AddressForm,
    ContactChannelForm,
    ContactDestinationForm,
    ContactPreferenceForm,
    ContactVerifyForm,
    DemographicsForm,
    EmergencyContactForm,
    EnrollmentForm,
    IdentifierAddForm,
    IdentifierRetireForm,
    MembershipForm,
    PatientAccessForm,
    PatientCreateForm,
    PatientSearchForm,
)
from apps.intake.models import (
    CONTACT_CHANNEL_VALUES,
    CONTACT_PURPOSE_VALUES,
    PatientChannelPreference,
    PatientContact,
    PatientContactEvent,
)
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    end_patient_session,
    patient_session_overview,
    redeem_invitation,
)
from apps.intake.services import (
    ContactConflictError,
    ContactInputError,
    PatientAccessDeniedError,
    PatientBirthDateError,
    PatientCreateInputError,
    PatientIdempotencyConflictError,
    PatientSearchInputError,
    access_overview,
    contact_for_edit,
    contact_overview,
    issue_invitation,
    revoke_patient_access,
    save_contact_destination,
    search_patients,
    set_purpose_channel,
    verify_contact,
)
from apps.tenancy.middleware import HANDLED_REJECTION_ATTR

if TYPE_CHECKING:
    from datetime import date

    from django.http import HttpRequest

    from apps.intake.demographics import DemographicsProfile
    from apps.intake.services import (
        AccessOverview,
        ContactEditTarget,
        ContactOverview,
        PatientSearchPage,
    )

SEE_OTHER: Final = 303
NO_CONTENT: Final = 204
RESULTS_PARTIAL: Final = "intake/partials/patient_results.html"
REGISTERED_MESSAGE: Final = "intake.patient.registered"
DUPLICATE_MESSAGE: Final = "intake.patient.possible_duplicate"
CONTACTS_TEMPLATE: Final = "intake/patient_contacts.html"
CONTACT_EDIT_TEMPLATE: Final = "intake/patient_contact_edit.html"
CONTACT_SAVED_MESSAGE: Final = _("Contact saved.")
CONTACT_VERIFIED_MESSAGE: Final = _("Destination verified for automated messages.")
PREFERENCES_SAVED_MESSAGE: Final = _("Messaging preferences saved.")
PREFERENCES_REVOKED_MESSAGE: Final = _(
    "Automated messages for this purpose were revoked."
)
CONTACT_CONFLICT_MESSAGE: Final = _(
    "This contact changed since you opened it. Review the current destination "
    "and save again."
)
CONTACT_VERIFY_CONFLICT_MESSAGE: Final = _(
    "This destination changed since you opened it. Review the current "
    "destination and verify it again."
)
INVALID_CONTACT_MESSAGE: Final = _("Enter a valid destination for this channel.")
CHANNEL_LABELS: Final = {
    PatientContact.Channel.SMS: _("SMS"),
    PatientContact.Channel.EMAIL: _("Email"),
    PatientContact.Channel.WHATSAPP: _("WhatsApp"),
}
PURPOSE_LABELS: Final = {
    PatientChannelPreference.Purpose.APPOINTMENT_REMINDER: _("Appointment reminders"),
    PatientChannelPreference.Purpose.BOOKING_CONFIRMATION: _("Booking confirmations"),
    PatientChannelPreference.Purpose.WAITLIST_OFFER: "Ofertas da lista de espera",
}
EVENT_LABELS: Final = {
    PatientContactEvent.EventType.CONTACT_SAVED: _("Contact saved"),
    PatientContactEvent.EventType.CONTACT_VERIFIED: _("Destination verified"),
    PatientContactEvent.EventType.VERIFICATION_INVALIDATED: _(
        "Verification invalidated by a destination change"
    ),
    PatientContactEvent.EventType.PREFERENCE_OPTED_IN: _("Messages allowed"),
    PatientContactEvent.EventType.PREFERENCE_OPTED_OUT: _("Messages revoked"),
}


def patient_list_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe search challenge at the blank clinic patient list."""
    return reverse("intake:patient-list", args=(clinic_id,))


def patient_create_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe registration challenge at the clinic create form."""
    return reverse("intake:patient-create", args=(clinic_id,))


def patient_contacts_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe contacts challenge at the blank clinic patient list."""
    return reverse("intake:patient-list", args=(clinic_id,))


def patient_contacts_url(clinic_id: UUID) -> str:
    """Return the POST-only contacts endpoint for one clinic."""
    return reverse("intake:patient-contacts", args=(clinic_id,))


def patient_access_url(clinic_id: UUID) -> str:
    """Return the POST-only patient-access endpoint for one clinic."""
    return reverse("intake:patient-access", args=(clinic_id,))


def patient_access_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe access challenge at the blank access screen."""
    return patient_access_url(clinic_id)


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _require_clinic(clinic_id: UUID) -> None:
    """Refuse a blank screen for a clinic the actor may not work in.

    The unsafe methods already fail closed inside the services; the blank GET
    answers with the same 404 so the rendered clinic context is never a
    clinic the form could not submit to.
    """
    try:
        authorized_manager_clinic(clinic_id)
    except PatientAccessDeniedError as error:
        raise Http404 from error


def _results_status(results: PatientSearchPage | None, query: str) -> str:
    """Describe one result page in words: count first, then the page."""
    if results is None:
        return _("Submit a search to list patients.")
    if results.total == 0:
        return _("No patient named \u201c%(term)s\u201d in this clinic.") % {
            "term": query
        }
    count = ngettext(
        "%(total)s matching patient.",
        "%(total)s matching patients.",
        results.total,
    ) % {"total": results.total}
    page = _("Page %(page)s of %(pages)s.") % {
        "page": results.page,
        "pages": results.page_count,
    }
    return f"{count} {page}"


def _search_context(
    clinic_id: UUID,
    form: PatientSearchForm,
    results: PatientSearchPage | None,
    birth_date: date | None,
) -> dict[str, object]:
    query = form.data.get("q", "") if results is not None else ""
    identifier_kind = form.data.get("identifier_kind", "")
    try:
        review_actor(clinic_id)
        can_review_duplicates = True
    except PatientAccessDeniedError:
        can_review_duplicates = False
    return {
        "can_review_duplicates": can_review_duplicates,
        "access_url": patient_access_url(clinic_id),
        "birth_date_value": birth_date.isoformat() if birth_date else "",
        "book_url": reverse("scheduling:appointment-create", args=(clinic_id,)),
        "clinic_id": clinic_id,
        "contacts_url": patient_contacts_url(clinic_id),
        "create_url": patient_create_continuation(clinic_id),
        "form": form,
        "list_url": patient_list_continuation(clinic_id),
        "next_page": (
            results.page + 1
            if results is not None and results.page < results.page_count
            else None
        ),
        "previous_page": (
            results.page - 1 if results is not None and results.page > 1 else None
        ),
        "query": query,
        "results": results,
        "status": (
            _("No patient with this document in this clinic.")
            if identifier_kind and results is not None and results.total == 0
            else _results_status(results, query)
        ),
    }


def _render_search(
    request: HttpRequest,
    context: dict[str, object],
) -> HttpResponseBase:
    template = RESULTS_PARTIAL if _is_htmx(request) else "intake/patient_list.html"
    return render(request, template, context)


@privileged_totp_required(patient_list_continuation)
@require_http_methods(["GET", "POST"])
def patient_list_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Render a blank search on GET and one deterministic page on POST."""
    if request.method != "POST":
        _require_clinic(clinic_id)
        blank = PatientSearchForm()
        return render(
            request,
            "intake/patient_list.html",
            _search_context(clinic_id, blank, None, None),
        )
    form = PatientSearchForm(data=request.POST)
    results: PatientSearchPage | None = None
    birth_date: date | None = None
    if form.is_valid():
        birth_date = form.cleaned_data["birth_date"]
        try:
            if form.cleaned_data["identifier_kind"]:
                _require_clinic(clinic_id)
                results = search_patient_identifiers(
                    clinic_id=clinic_id,
                    kind=form.cleaned_data["identifier_kind"],
                    value=form.cleaned_data["identifier_value"],
                )
            else:
                results = search_patients(
                    clinic_id=clinic_id,
                    query=form.cleaned_data["q"],
                    page=form.selected_page(),
                    birth_date=birth_date,
                )
        except PatientAccessDeniedError as error:
            raise Http404 from error
        except DemographicsInputError:
            form.add_error("identifier_value", _("Enter a valid document number."))
        except PatientSearchInputError:
            form.add_error(None, INVALID_CREATE_MESSAGE)
    return _render_search(
        request,
        _search_context(clinic_id, form, results, birth_date),
    )


def _create_context(
    clinic_id: UUID,
    form: PatientCreateForm,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "clinic_id": clinic_id,
        "create_url": patient_create_continuation(clinic_id),
        "form": form,
        "idempotency_key": idempotency_key,
        "list_url": patient_list_continuation(clinic_id),
    }


def _created_response(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    target = patient_list_continuation(clinic_id)
    # Completion feedback carries no patient data: the search screen names the
    # next step and the receptionist finds the patient by name.
    messages.success(
        request,
        _("The patient can now be booked in this clinic. Search by name to book."),
        extra_tags=REGISTERED_MESSAGE,
    )
    if _is_htmx(request):
        response: HttpResponseBase = HttpResponse(status=NO_CONTENT)
        response.headers["HX-Redirect"] = target
        return response
    response = redirect(target, permanent=False)
    response.status_code = SEE_OTHER
    return response


@privileged_totp_required(patient_create_continuation)
@require_http_methods(["GET", "POST"])
def patient_create_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Issue one registration key on GET and register one patient on POST."""
    try:
        required_fields = registration_required_fields(clinic_id=clinic_id)
    except PatientAccessDeniedError as denied:
        raise Http404 from denied
    if request.method != "POST":
        _require_clinic(clinic_id)
        blank = PatientCreateForm(required_fields=required_fields)
        return render(
            request,
            "intake/patient_create.html",
            _create_context(clinic_id, blank, str(uuid4())),
        )
    submitted_key = request.POST.get("idempotency_key", "")
    form = PatientCreateForm(data=request.POST, required_fields=required_fields)
    if form.is_valid():
        try:
            inputs = form.registration_inputs()
            outcome = register_patient(
                clinic_id=clinic_id,
                legal_name=inputs.legal_name,
                social_name=inputs.social_name,
                birth_date=inputs.birth_date,
                idempotency_key=inputs.idempotency_key,
                changes=inputs.changes,
                unknown=inputs.unknown,
                identifier_kind=inputs.identifier_kind,
                identifier_value=inputs.identifier_value,
            )
        except PatientAccessDeniedError as error:
            raise Http404 from error
        except PatientIdempotencyConflictError:
            form.add_error(None, CONFLICTING_KEY_MESSAGE)
        except DemographicsRequiredError:
            form.add_error(
                None,
                _("Complete the required fields or record why they are unanswered."),
            )
        except (
            DemographicsInputError,
            PatientBirthDateError,
            PatientCreateInputError,
        ) as error:
            _bind_registration_error(form, error)
        else:
            if outcome.matching_enrollment_id is not None:
                messages.warning(
                    request,
                    _("Possible duplicate. The registration was saved."),
                    extra_tags=DUPLICATE_MESSAGE,
                )
            return _created_response(request, clinic_id)
    response = render(
        request,
        "intake/patient_create.html",
        _create_context(clinic_id, form, submitted_key),
    )
    setattr(response, HANDLED_REJECTION_ATTR, True)
    return response


def _registration_error(
    error: PatientBirthDateError | PatientCreateInputError,
) -> str:
    """Present the existing birth-date refusal separately from other input."""
    if isinstance(error, PatientBirthDateError) or isinstance(
        error.__cause__, PatientBirthDateError
    ):
        return _("Enter a patient name and a valid birth date.")
    return str(INVALID_CREATE_MESSAGE)


def _bind_registration_error(
    form: PatientCreateForm,
    error: DemographicsInputError | PatientBirthDateError | PatientCreateInputError,
) -> None:
    """Bind one handled registration error to the field or the whole form."""
    if isinstance(error, IdentifierInputError):
        form.add_error("identifier_value", _("Enter a valid document number."))
        return
    if isinstance(error, DemographicsInputError):
        if error.field is not None:
            field = "full_name" if error.field == "legal_name" else error.field
            form.add_error(field, _("Check the entered values."))
            return
        form.add_error(None, INVALID_CREATE_MESSAGE)
        return
    form.add_error(None, _registration_error(error))


def _demographics_context(
    clinic_id: UUID, profile: DemographicsProfile
) -> dict[str, object]:
    """Build separately versioned POST forms from the authorized profile."""
    demographics = DemographicsForm()
    demographics.initial = {
        **profile.values,
        **{f"{field}_status": value for field, value in profile.unknown.items()},
        "enrollment_id": profile.enrollment_id,
        "expected_version": profile.version,
    }
    identifier = IdentifierAddForm()
    identifier.initial = {"enrollment_id": profile.enrollment_id}
    identifier.auto_id = "identifier-new-%s"
    addresses: list[dict[str, object]] = []
    for address in profile.addresses:
        address_form = AddressForm()
        address_form.initial = {
            **asdict(address),
            "enrollment_id": profile.enrollment_id,
            "expected_version": address.version,
        }
        address_form.auto_id = f"address-{address.kind}-%s"
        address_form.fields["kind"].widget = forms.HiddenInput()
        addresses.append(
            {"label": ADDRESS_KIND_LABELS[address.kind], "form": address_form}
        )
    new_address = AddressForm()
    new_address.initial = {
        "enrollment_id": profile.enrollment_id,
        "expected_version": 0,
    }
    new_address.auto_id = "address-new-%s"
    new_address.fields["kind"] = forms.ChoiceField(
        label=_("Address type"),
        choices=tuple(
            (kind, ADDRESS_KIND_LABELS[kind]) for kind in profile.free_address_kinds
        ),
    )
    contacts: list[dict[str, object]] = []
    for contact in profile.emergency_contacts:
        contact_form = EmergencyContactForm()
        contact_form.initial = {
            **asdict(contact),
            "enrollment_id": profile.enrollment_id,
            "expected_version": contact.version,
        }
        contact_form.auto_id = f"contact-{contact.sequence}-%s"
        contact_form.fields["sequence"].widget = forms.HiddenInput()
        contacts.append({"form": contact_form})
    new_contact = EmergencyContactForm()
    new_contact.initial = {
        "enrollment_id": profile.enrollment_id,
        "expected_version": 0,
    }
    new_contact.auto_id = "contact-new-%s"
    new_contact.fields["sequence"] = forms.TypedChoiceField(
        label=_("Contact slot"),
        choices=tuple((str(slot), str(slot)) for slot in profile.free_contact_slots),
        coerce=int,
    )
    memberships: list[dict[str, object]] = []
    for membership in profile.memberships:
        membership_form = MembershipForm()
        membership_form.initial = {
            **asdict(membership),
            "enrollment_id": profile.enrollment_id,
            "expected_version": membership.version,
        }
        membership_form.auto_id = f"membership-{membership.sequence}-%s"
        membership_form.fields["sequence"].widget = forms.HiddenInput()
        memberships.append({"form": membership_form})
    new_membership = MembershipForm()
    new_membership.initial = {
        "enrollment_id": profile.enrollment_id,
        "expected_version": 0,
    }
    new_membership.auto_id = "membership-new-%s"
    new_membership.fields["sequence"] = forms.TypedChoiceField(
        label=_("Membership slot"),
        choices=tuple((str(slot), str(slot)) for slot in profile.free_membership_slots),
        coerce=int,
    )
    return {
        "profile": profile,
        "demographics_form": demographics,
        "identifier_form": identifier,
        "identifier_rows": [
            {"record": record, "label": IDENTIFIER_KIND_LABELS[record.kind]}
            for record in profile.identifiers
        ],
        "addresses": addresses,
        "new_address": new_address,
        "contacts": contacts,
        "new_contact": new_contact,
        "memberships": memberships,
        "new_membership": new_membership,
        "corrections": [
            {
                "record": correction,
                "fields": [
                    DEMOGRAPHIC_FIELD_LABELS[field]
                    for field in correction.changed_fields
                ],
            }
            for correction in profile.corrections
        ],
        "manage_url": reverse("intake:patient-demographics", args=(clinic_id,)),
        "list_url": patient_list_continuation(clinic_id),
    }


def _identifier_action(
    request: HttpRequest,
    clinic_id: UUID,
    action: str,
    holder: dict[str, forms.Form],
) -> forms.Form:
    """Add or retire a document through the scoped versioned service."""
    if action == "add_identifier":
        form = IdentifierAddForm(data=request.POST)
        holder["form"] = form
        if form.is_valid():
            result = add_identifier(
                clinic_id=clinic_id,
                enrollment_id=form.selected_enrollment(),
                kind=form.selected_kind(),
                value=str(form.cleaned_data["value"]),
                issuer=str(form.cleaned_data["issuer"]),
            )
            if result.matching_enrollment_id is not None:
                messages.warning(
                    request, _("Possible duplicate. The document was saved.")
                )
        return form
    retirement = IdentifierRetireForm(data=request.POST)
    holder["form"] = retirement
    if retirement.is_valid():
        retire_identifier(
            clinic_id=clinic_id,
            enrollment_id=retirement.selected_enrollment(),
            kind=retirement.selected_kind(),
            expected_version=retirement.selected_version(),
        )
    return retirement


def _section_action(
    request: HttpRequest,
    clinic_id: UUID,
    action: str,
    holder: dict[str, forms.Form],
) -> forms.Form:
    """Save or retire one address, emergency contact or membership version."""
    match action:
        case "save_address" | "retire_address":
            address = AddressForm(data=request.POST)
            holder["form"] = address
            if address.is_valid():
                save_patient_address(
                    clinic_id=clinic_id,
                    enrollment_id=address.selected_enrollment(),
                    kind=address.selected_kind(),
                    expected_version=address.selected_version(),
                    address=(
                        None if action == "retire_address" else address.section_values()
                    ),
                )
            return address
        case "save_contact" | "retire_contact":
            contact = EmergencyContactForm(data=request.POST)
            holder["form"] = contact
            if contact.is_valid():
                save_emergency_contact(
                    clinic_id=clinic_id,
                    enrollment_id=contact.selected_enrollment(),
                    sequence=contact.selected_sequence(),
                    expected_version=contact.selected_version(),
                    contact=(
                        None if action == "retire_contact" else contact.section_values()
                    ),
                )
            return contact
        case "save_membership" | "retire_membership":
            membership = MembershipForm(data=request.POST)
            holder["form"] = membership
            if membership.is_valid():
                save_insurance_membership(
                    clinic_id=clinic_id,
                    enrollment_id=membership.selected_enrollment(),
                    sequence=membership.selected_sequence(),
                    expected_version=membership.selected_version(),
                    membership=(
                        None
                        if action == "retire_membership"
                        else membership.section_values()
                    ),
                )
            return membership
        case _:
            raise Http404


@privileged_totp_required(patient_list_continuation)
@require_http_methods(["POST"])
def patient_demographics_view(
    request: HttpRequest, clinic_id: UUID
) -> HttpResponseBase:
    """Read or mutate one exact enrollment selected only in the POST body."""
    selection = EnrollmentForm(data=request.POST)
    if not selection.is_valid():
        raise Http404
    enrollment_id = selection.selected_enrollment()
    action = request.POST.get("action", "manage")
    try:
        # Access preflight only: the returned profile is superseded by the
        # post-mutation read below, so it must not write a viewed event.
        demographics_profile(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            record_view=False,
        )
    except PatientAccessDeniedError as error:
        if action != "manage":
            raise Http404 from error
        try:
            minimal = billing_demographics(
                clinic_id=clinic_id, enrollment_id=enrollment_id
            )
        except PatientAccessDeniedError as denied:
            raise Http404 from denied
        return render(
            request,
            "intake/patient_demographics_minimal.html",
            {"identity": minimal, "list_url": patient_list_continuation(clinic_id)},
        )
    bound_form, rejected = _profile_feedback(request, clinic_id, enrollment_id, action)
    profile = demographics_profile(
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        record_view=not rejected,
    )
    bind_patient_context(
        request,
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        record_view=not rejected,
    )
    context = _demographics_context(clinic_id, profile)
    if bound_form is not None and bound_form.errors:
        bound_form.auto_id = "rejected-%s"
        context.update({"rejected_form": bound_form, "rejected_action": action})
    response = render(request, "intake/patient_demographics.html", context)
    if rejected:
        setattr(response, HANDLED_REJECTION_ATTR, True)
    return response


def _profile_action(
    request: HttpRequest,
    clinic_id: UUID,
    enrollment_id: UUID,
    action: str,
    holder: dict[str, forms.Form],
) -> forms.Form | None:
    """Route one explicit action to the corresponding scoped service."""
    match action:
        case "manage":
            return None
        case "save":
            correction = DemographicsForm(data=request.POST)
            holder["form"] = correction
            if correction.is_valid():
                update_demographics(
                    clinic_id=clinic_id,
                    enrollment_id=enrollment_id,
                    expected_version=correction.selected_version(),
                    changes=correction.cleaned_changes(),
                    unknown=correction.cleaned_unknown(),
                    reason=str(correction.cleaned_data["reason"]),
                )
            return correction
        case "add_identifier" | "retire_identifier":
            return _identifier_action(request, clinic_id, action, holder)
        case _:
            return _section_action(request, clinic_id, action, holder)


_PROFILE_ERROR_FEEDBACK: Final = (
    (
        DemographicsNameRequiredError,
        "legal_name",
        "Enter a legal or a social name.",
        "Enter a legal or a social name.",
    ),
    (
        IdentifierInputError,
        "value",
        "Enter a valid document number.",
        "Enter a valid document number.",
    ),
    (
        DemographicsRequiredError,
        None,
        "Check the entered values.",
        "Check the entered values and try again.",
    ),
    (
        PatientCreateInputError,
        None,
        "Check the entered values.",
        "Check the entered values and try again.",
    ),
    (
        DemographicsInputError,
        None,
        "Check the entered values.",
        "Check the entered values and try again.",
    ),
)


def _profile_error_feedback(
    error: ValueError,
) -> tuple[str | None, str, str]:
    """Return (field, form message, flash) for one rejected write."""
    for kind, field, message, flash in _PROFILE_ERROR_FEEDBACK:
        if isinstance(error, kind):
            return field, message, flash
    return None, "Check the entered values.", "Check the entered values and try again."


def _profile_feedback(
    request: HttpRequest, clinic_id: UUID, enrollment_id: UUID, action: str
) -> tuple[forms.Form | None, bool]:
    """Preserve validation feedback and exact denial/conflict distinctions."""
    holder: dict[str, forms.Form] = {}
    bound_form: forms.Form | None = None
    rejected = False
    try:
        bound_form = _profile_action(request, clinic_id, enrollment_id, action, holder)
    except PatientAccessDeniedError as denied:
        raise Http404 from denied
    except DemographicsStaleError:
        rejected = True
        messages.error(request, _("This record changed. Reload it before editing."))
    except IdentifierConflictError:
        rejected = True
        messages.error(
            request, _("This document already exists. Reload it before editing.")
        )
    except (
        DemographicsInputError,
        DemographicsNameRequiredError,
        DemographicsRequiredError,
        IdentifierInputError,
        PatientCreateInputError,
    ) as invalid:
        rejected = True
        bound_form = bound_form or holder.get("form")
        field, message, flash = _profile_error_feedback(invalid)
        if bound_form is not None:
            bound_form.add_error(field, _(message))
        messages.error(request, _(flash))
    else:
        if bound_form is not None and not bound_form.errors:
            message = (
                _("Record removed. Earlier versions stay in history.")
                if action.startswith("retire_")
                else (
                    _("Demographics saved.") if action == "save" else _("Record saved.")
                )
            )
            messages.success(request, message)
    return bound_form, rejected or bool(bound_form is not None and bound_form.errors)


# --------------------------------------------------------------------------
# Contacts and messaging preferences
# --------------------------------------------------------------------------


def _purpose_rows(overview: ContactOverview) -> list[dict[str, object]]:
    """Pair each purpose with its current channel choice for the template."""
    rows: list[dict[str, object]] = []
    for purpose in CONTACT_PURPOSE_VALUES:
        current = next(
            (
                preference.channel
                for preference in overview.preferences
                if preference.purpose == purpose and preference.opted_in
            ),
            None,
        )
        rows.append(
            {
                "purpose": purpose,
                "label": PURPOSE_LABELS[PatientChannelPreference.Purpose(purpose)],
                "current": current,
                "channels": [
                    {
                        "value": channel,
                        "label": CHANNEL_LABELS[PatientContact.Channel(channel)],
                    }
                    for channel in CONTACT_CHANNEL_VALUES
                ],
            }
        )
    return rows


def _contact_rows(overview: ContactOverview) -> list[dict[str, object]]:
    """Pair each channel with its masked contact state for the template."""
    by_channel = {contact.channel: contact for contact in overview.contacts}
    return [
        {
            "channel": channel,
            "label": CHANNEL_LABELS[PatientContact.Channel(channel)],
            "contact": by_channel.get(channel),
        }
        for channel in CONTACT_CHANNEL_VALUES
    ]


def _history_rows(overview: ContactOverview) -> list[dict[str, object]]:
    """Translate the append-only history for the template."""
    return [
        {
            "label": EVENT_LABELS[PatientContactEvent.EventType(event.event_type)],
            "actor_label": event.actor_label,
            "channel_label": (
                CHANNEL_LABELS[PatientContact.Channel(event.channel)]
                if event.channel
                else ""
            ),
            "purpose_label": (
                PURPOSE_LABELS[PatientChannelPreference.Purpose(event.purpose)]
                if event.purpose
                else ""
            ),
            "version": event.version,
            "created_at": event.created_at,
        }
        for event in overview.history
    ]


def _manage_context(
    clinic_id: UUID,
    overview: ContactOverview,
    *,
    error: str = "",
) -> dict[str, object]:
    return {
        "clinic_id": clinic_id,
        "contacts_url": patient_contacts_url(clinic_id),
        "enrollment_id": overview.enrollment_id,
        "error": error,
        "history_rows": _history_rows(overview),
        "list_url": patient_list_continuation(clinic_id),
        "overview": overview,
        "contact_rows": _contact_rows(overview),
        "purpose_rows": _purpose_rows(overview),
    }


def _edit_context(
    clinic_id: UUID,
    target: ContactEditTarget,
    form: ContactDestinationForm,
    *,
    error: str = "",
) -> dict[str, object]:
    return {
        "channel_label": CHANNEL_LABELS[PatientContact.Channel(target.channel)],
        "clinic_id": clinic_id,
        "contacts_url": patient_contacts_url(clinic_id),
        "error": error,
        "form": form,
        "list_url": patient_list_continuation(clinic_id),
        "target": target,
    }


def _render_manage(
    request: HttpRequest,
    clinic_id: UUID,
    enrollment_id: UUID,
    *,
    error: str = "",
) -> HttpResponseBase:
    overview = contact_overview(clinic_id=clinic_id, enrollment_id=enrollment_id)
    bind_patient_context(request, clinic_id=clinic_id, enrollment_id=enrollment_id)
    return render(
        request,
        CONTACTS_TEMPLATE,
        _manage_context(clinic_id, overview, error=error),
    )


def _render_edit(  # noqa: PLR0913 - the edit surface needs its full context
    request: HttpRequest,
    clinic_id: UUID,
    enrollment_id: UUID,
    channel: str,
    *,
    form: ContactDestinationForm | None = None,
    error: str = "",
) -> HttpResponseBase:
    target = contact_for_edit(
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        channel=channel,
    )
    if form is None:
        form = ContactDestinationForm(
            data={
                "enrollment_id": str(enrollment_id),
                "channel": channel,
                "destination": target.destination,
                "expected_version": str(target.destination_version),
            }
        )
    return render(
        request,
        CONTACT_EDIT_TEMPLATE,
        _edit_context(clinic_id, target, form, error=error),
    )


def _contact_action(request: HttpRequest) -> str:
    action = request.POST.get("action", "")
    if action not in {"manage", "edit", "save", "verify", "preference"}:
        raise Http404
    return action


def _enrollment_from(form: EnrollmentForm) -> UUID:
    if not form.is_valid():
        raise Http404
    return form.selected_enrollment()


def _contacts_edit(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    form = ContactChannelForm(data=request.POST)
    if not form.is_valid():
        raise Http404
    return _render_edit(
        request,
        clinic_id,
        form.selected_enrollment(),
        form.selected_channel(),
    )


def _contacts_save(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    form = ContactDestinationForm(data=request.POST)
    if not form.is_valid():
        channel = request.POST.get("channel", "")
        if channel not in CONTACT_CHANNEL_VALUES:
            raise Http404
        enrollment_id = _enrollment_from(EnrollmentForm(data=request.POST))
        return _render_edit(
            request,
            clinic_id,
            enrollment_id,
            channel,
            form=form,
            error=INVALID_CONTACT_MESSAGE,
        )
    enrollment_id = form.selected_enrollment()
    channel = form.selected_channel()
    try:
        save_contact_destination(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            channel=channel,
            destination=form.cleaned_data["destination"],
            expected_version=form.selected_version(),
        )
    except ContactConflictError:
        return _render_edit(
            request,
            clinic_id,
            enrollment_id,
            channel,
            error=CONTACT_CONFLICT_MESSAGE,
        )
    except ContactInputError:
        form.add_error("destination", INVALID_CONTACT_MESSAGE)
        return _render_edit(
            request,
            clinic_id,
            enrollment_id,
            channel,
            form=form,
            error=INVALID_CONTACT_MESSAGE,
        )
    messages.success(request, str(CONTACT_SAVED_MESSAGE))
    return _render_manage(request, clinic_id, enrollment_id)


def _contacts_verify(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    form = ContactVerifyForm(data=request.POST)
    if not form.is_valid():
        raise Http404
    enrollment_id = form.selected_enrollment()
    try:
        verify_contact(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            channel=form.selected_channel(),
            expected_version=form.selected_version(),
        )
    except ContactConflictError:
        return _render_manage(
            request,
            clinic_id,
            enrollment_id,
            error=CONTACT_VERIFY_CONFLICT_MESSAGE,
        )
    except ContactInputError:
        raise Http404 from None
    messages.success(request, str(CONTACT_VERIFIED_MESSAGE))
    return _render_manage(request, clinic_id, enrollment_id)


def _contacts_preference(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    form = ContactPreferenceForm(data=request.POST)
    if not form.is_valid():
        raise Http404
    enrollment_id = form.selected_enrollment()
    channel = form.selected_channel()
    set_purpose_channel(
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        purpose=form.selected_purpose(),
        channel=channel,
    )
    messages.success(
        request,
        str(
            PREFERENCES_SAVED_MESSAGE
            if channel is not None
            else PREFERENCES_REVOKED_MESSAGE
        ),
    )
    return _render_manage(request, clinic_id, enrollment_id)


@privileged_totp_required(patient_contacts_continuation)
@require_http_methods(["GET", "POST"])
def patient_contacts_view(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Manage one enrolled patient's contacts and messaging preferences.

    GET renders only the blank entry state; the enrollment travels in the
    POST body so patient identity never reaches a URL. Destinations are
    masked here; the explicit edit screen is the only full-destination
    surface.
    """
    try:
        if request.method != "POST":
            _require_clinic(clinic_id)
            return render(
                request,
                CONTACTS_TEMPLATE,
                {
                    "clinic_id": clinic_id,
                    "contacts_url": patient_contacts_url(clinic_id),
                    "list_url": patient_list_continuation(clinic_id),
                    "overview": None,
                },
            )
        action = _contact_action(request)
        if action == "manage":
            enrollment_id = _enrollment_from(EnrollmentForm(data=request.POST))
            return _render_manage(request, clinic_id, enrollment_id)
        if action == "edit":
            return _contacts_edit(request, clinic_id)
        if action == "save":
            return _contacts_save(request, clinic_id)
        if action == "verify":
            return _contacts_verify(request, clinic_id)
        return _contacts_preference(request, clinic_id)
    except PatientAccessDeniedError as error:
        raise Http404 from error


# --------------------------------------------------------------------------
# Patient access: staff-issued invitations and the patient session surface
# --------------------------------------------------------------------------

ACCESS_TEMPLATE: Final = "intake/patient_access_manage.html"
ACCESS_ISSUED_MESSAGE: Final = _(
    "Invitation created. Share the code with the patient now; it is shown "
    "only once and expires in 24 hours."
)
ACCESS_REVOKED_MESSAGE: Final = _(
    "Patient access revoked. The invitation and its sessions no longer work."
)
INVALID_CODE_MESSAGE: Final = _(
    "This access code is not valid. Check the code or ask the clinic for a "
    "new invitation."
)


def _grant_rows(overview: AccessOverview) -> list[dict[str, object]]:
    """Translate invitation rows into display state for the template."""
    now = timezone.now()
    rows: list[dict[str, object]] = []
    for grant in overview.grants:
        if grant.revoked_at is not None:
            state = _("Revoked")
        elif grant.consumed_at is not None:
            state = _("Used")
        elif grant.expires_at <= now:
            state = _("Expired")
        else:
            state = _("Active")
        rows.append(
            {
                "grant_id": grant.grant_id,
                "issued_by_label": grant.issued_by_label,
                "created_at": grant.created_at,
                "expires_at": grant.expires_at,
                "state": state,
                "revocable": grant.revoked_at is None,
            }
        )
    return rows


def _session_rows(overview: AccessOverview) -> list[dict[str, object]]:
    """Translate session rows into display state for the template."""
    now = timezone.now()
    rows: list[dict[str, object]] = []
    for session in overview.sessions:
        if session.revoked_at is not None:
            state = _("Revoked")
        elif session.expires_at <= now or session.idle_expires_at <= now:
            state = _("Expired")
        else:
            state = _("Active")
        rows.append(
            {
                "session_id": session.session_id,
                "grant_id": session.grant_id,
                "created_at": session.created_at,
                "expires_at": session.expires_at,
                "idle_expires_at": session.idle_expires_at,
                "state": state,
            }
        )
    return rows


def _access_context(
    clinic_id: UUID,
    overview: AccessOverview | None,
    *,
    issued_code: str = "",
    error: str = "",
) -> dict[str, object]:
    context: dict[str, object] = {
        "access_url": patient_access_url(clinic_id),
        "clinic_id": clinic_id,
        "error": error,
        "issued_code": issued_code,
        "list_url": patient_list_continuation(clinic_id),
        "overview": overview,
    }
    if overview is not None:
        context["grant_rows"] = _grant_rows(overview)
        context["session_rows"] = _session_rows(overview)
    return context


def _render_access(
    request: HttpRequest,
    clinic_id: UUID,
    enrollment_id: UUID,
    *,
    issued_code: str = "",
    error: str = "",
) -> HttpResponseBase:
    overview = access_overview(clinic_id=clinic_id, enrollment_id=enrollment_id)
    bind_patient_context(request, clinic_id=clinic_id, enrollment_id=enrollment_id)
    return render(
        request,
        ACCESS_TEMPLATE,
        _access_context(
            clinic_id,
            overview,
            issued_code=issued_code,
            error=error,
        ),
    )


def _access_issue(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    form = EnrollmentForm(data=request.POST)
    if not form.is_valid():
        raise Http404
    enrollment_id = form.selected_enrollment()
    issued = issue_invitation(
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
    )
    messages.success(request, str(ACCESS_ISSUED_MESSAGE))
    return _render_access(
        request,
        clinic_id,
        enrollment_id,
        issued_code=issued.secret,
    )


def _access_revoke(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    form = AccessRevokeForm(data=request.POST)
    if not form.is_valid():
        raise Http404
    enrollment_id = form.selected_enrollment()
    revoke_patient_access(
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        grant_id=form.selected_grant(),
    )
    messages.success(request, str(ACCESS_REVOKED_MESSAGE))
    return _render_access(request, clinic_id, enrollment_id)


@privileged_totp_required(patient_access_continuation)
@require_http_methods(["GET", "POST"])
def patient_access_view(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Issue and revoke enrollment-bound patient invitations.

    GET renders only the blank entry state; the enrollment travels in the
    POST body so patient identity never reaches a URL. The issued code is
    shown once in the response body and never stored or logged.
    """
    try:
        if request.method != "POST":
            _require_clinic(clinic_id)
            return render(
                request,
                ACCESS_TEMPLATE,
                _access_context(clinic_id, None),
            )
        action = request.POST.get("action", "")
        if action == "manage":
            enrollment_id = _enrollment_from(EnrollmentForm(data=request.POST))
            return _render_access(request, clinic_id, enrollment_id)
        if action == "issue":
            return _access_issue(request, clinic_id)
        if action == "revoke":
            return _access_revoke(request, clinic_id)
        raise Http404
    except PatientAccessDeniedError as error:
        raise Http404 from error


@sensitive_post_parameters("code")
@require_http_methods(["GET", "POST"])
def patient_access_redeem_view(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Exchange one invitation code for a patient session.

    The code arrives only in the POST body; the clinic in the path is part
    of the lookup, so a code issued for another clinic fails identically
    to an unknown, expired, consumed or revoked code. A successful
    redemption rotates the session key and binds the patient session id
    to the signed cookie.
    """
    form = PatientAccessForm(data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        session_id = redeem_invitation(clinic_id, form.selected_code())
        if session_id is not None:
            request.session.cycle_key()
            request.session[PATIENT_SESSION_KEY] = str(session_id)
            return redirect("intake:patient-home", permanent=False)
        form.add_error(None, INVALID_CODE_MESSAGE)
    return render(
        request,
        "intake/patient_access.html",
        {"clinic_id": clinic_id, "form": form},
    )


@require_http_methods(["GET"])
def patient_home_view(request: HttpRequest) -> HttpResponseBase:
    """Render the bound enrollment for the current patient session.

    The resolver re-validates the session row; a session without the
    ``enrollment_view`` operation or a session revoked between the
    middleware touch and this read renders the same gate.
    """
    overview = patient_session_overview()
    if overview is None or "enrollment_view" not in overview.operations:
        return render(
            request,
            "intake/patient_gate.html",
            {"state": "required"},
            status=403,
        )
    return render(
        request,
        "intake/patient_home.html",
        {"overview": overview, "unified_at": patient_alias_overview()},
    )


@require_http_methods(["POST"])
def patient_logout_view(request: HttpRequest) -> HttpResponseBase:
    """Revoke the patient session server-side and drop it from the cookie."""
    raw_session_id = request.session.get(PATIENT_SESSION_KEY)
    try:
        session_id = UUID(str(raw_session_id))
    except (ValueError, TypeError, AttributeError):
        session_id = None
    if session_id is not None:
        end_patient_session(session_id)
    request.session.pop(PATIENT_SESSION_KEY, None)
    return render(
        request,
        "intake/patient_gate.html",
        {"state": "signed_out"},
    )
