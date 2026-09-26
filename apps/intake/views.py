"""Accessible POST-only patient search and registration screens."""

from __future__ import annotations

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

from apps.identity.otp import privileged_totp_required
from apps.intake.access import authorized_manager_clinic
from apps.intake.forms import (
    ADDRESS_KIND_LABELS,
    CONFLICTING_KEY_MESSAGE,
    DEMOGRAPHIC_FIELD_LABELS,
    IDENTIFIER_KIND_LABELS,
    INVALID_CREATE_MESSAGE,
    NAME_REQUIRED_MESSAGE,
    UNKNOWN_STATUS_LABELS,
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
    SectionForm,
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
    DemographicsInputError,
    DemographicsNameRequiredError,
    DemographicsRequiredError,
    DemographicsStaleError,
    IdentifierConflictError,
    PatientAccessDeniedError,
    PatientBirthDateError,
    PatientCreateInputError,
    PatientIdempotencyConflictError,
    PatientSearchInputError,
    access_overview,
    add_identifier,
    contact_for_edit,
    contact_overview,
    demographics_profile,
    issue_invitation,
    register_patient,
    registration_required_fields,
    retire_identifier,
    revoke_patient_access,
    save_contact_destination,
    save_emergency_contact,
    save_insurance_membership,
    save_patient_address,
    search_patient_identifiers,
    search_patients,
    set_purpose_channel,
    update_demographics,
    verify_contact,
)

if TYPE_CHECKING:
    from datetime import date

    from django.http import HttpRequest

    from apps.intake.services import (
        AccessOverview,
        ContactEditTarget,
        ContactOverview,
        DemographicsProfile,
        PatientSearchPage,
    )

SEE_OTHER: Final = 303
NO_CONTENT: Final = 204
RESULTS_PARTIAL: Final = "intake/partials/patient_results.html"
REGISTERED_MESSAGE: Final = "intake.patient.registered"
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


def _results_status(
    results: PatientSearchPage | None,
    query: str,
    *,
    identifier: bool = False,
) -> str:
    """Describe one result page in words: count first, then the page."""
    if results is None:
        return _("Submit a search to list patients.")
    if results.total == 0:
        if identifier:
            return _("No patient with that document in this clinic.")
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
    identifier_lookup = results is not None and bool(form.data.get("identifier_value"))
    return {
        "access_url": patient_access_url(clinic_id),
        "birth_date_value": birth_date.isoformat() if birth_date else "",
        "book_url": reverse("scheduling:appointment-create", args=(clinic_id,)),
        "clinic_id": clinic_id,
        "contacts_url": patient_contacts_url(clinic_id),
        "create_url": patient_create_continuation(clinic_id),
        "demographics_url": patient_demographics_continuation(clinic_id),
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
        "status": _results_status(results, query, identifier=identifier_lookup),
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
        identifier = form.identifier_search()
        try:
            if identifier is not None:
                results = search_patient_identifiers(
                    clinic_id=clinic_id,
                    kind=identifier[0],
                    value=identifier[1],
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
        except (PatientSearchInputError, DemographicsInputError):
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
        "required_labels": [
            DEMOGRAPHIC_FIELD_LABELS[field] for field in form.required_fields
        ],
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
    return redirect(target, permanent=False)


@privileged_totp_required(patient_create_continuation)
@require_http_methods(["GET", "POST"])
def patient_create_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Issue one registration key on GET and register one patient on POST.

    The clinic intake policy decides which fields registration must answer;
    the form renders those fields and accepts an explicit non-answer.
    """
    try:
        required = registration_required_fields(clinic_id=clinic_id)
    except PatientAccessDeniedError as error:
        raise Http404 from error
    if request.method != "POST":
        blank = PatientCreateForm(required_fields=required)
        return render(
            request,
            "intake/patient_create.html",
            _create_context(clinic_id, blank, str(uuid4())),
        )
    submitted_key = request.POST.get("idempotency_key", "")
    form = PatientCreateForm(data=request.POST, required_fields=required)
    if form.is_valid() and _register_patient(request, clinic_id, form):
        response = _created_response(request, clinic_id)
        if response.status_code != NO_CONTENT:
            response.status_code = SEE_OTHER
        return response
    return render(
        request,
        "intake/patient_create.html",
        _create_context(clinic_id, form, submitted_key),
    )


def _register_patient(
    request: HttpRequest, clinic_id: UUID, form: PatientCreateForm
) -> bool:
    """Register one patient; attach a service refusal to the form.

    ``register_patient`` keeps the patient, its first demographics version
    and any document in one transaction, so a refused value leaves nothing
    half-registered and the idempotency key stays reusable.
    """
    inputs = form.registration_inputs()
    try:
        outcome = register_patient(
            clinic_id=clinic_id,
            idempotency_key=inputs.idempotency_key,
            legal_name=inputs.legal_name,
            social_name=inputs.social_name,
            birth_date=inputs.birth_date,
            changes=inputs.changes,
            unknown=inputs.unknown,
            identifier_kind=inputs.identifier_kind,
            identifier_value=inputs.identifier_value,
        )
    except PatientAccessDeniedError as error:
        raise Http404 from error
    except (
        PatientIdempotencyConflictError,
        IdentifierConflictError,
        DemographicsStaleError,
    ):
        form.add_error(None, CONFLICTING_KEY_MESSAGE)
        return False
    except DemographicsNameRequiredError:
        form.add_error("full_name", NAME_REQUIRED_MESSAGE)
        return False
    except DemographicsRequiredError:
        form.add_error(None, REQUIRED_FIELDS_MESSAGE)
        return False
    except (PatientBirthDateError, PatientCreateInputError, DemographicsInputError):
        form.add_error(None, INVALID_CREATE_MESSAGE)
        return False
    if outcome.matching_enrollment_id is not None:
        messages.warning(
            request,
            DUPLICATE_WARNING_MESSAGE,
            extra_tags="intake.patient.possible_duplicate",
        )
    return True


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


# --------------------------------------------------------------------------
# Demographics: versioned profile, identifiers and correction history
# --------------------------------------------------------------------------

DEMOGRAPHICS_TEMPLATE: Final = "intake/patient_demographics.html"
DEMOGRAPHICS_SAVED_MESSAGE: Final = _("Demographics saved.")
DEMOGRAPHICS_STALE_MESSAGE: Final = _(
    "This record changed since you opened it. Review the current values and save again."
)
DEMOGRAPHICS_INVALID_MESSAGE: Final = _("Check the highlighted fields and try again.")
REQUIRED_FIELDS_MESSAGE: Final = _(
    "This clinic requires some fields. Answer them, or record why they were "
    "not informed."
)
IDENTIFIER_SAVED_MESSAGE: Final = _("Document recorded.")
IDENTIFIER_RETIRED_MESSAGE: Final = _("Document removed from the record.")
SECTION_SAVED_MESSAGE: Final = _("Record saved.")
SECTION_RETIRED_MESSAGE: Final = _("Record removed. Earlier versions stay in history.")
DUPLICATE_WARNING_MESSAGE: Final = _(
    "Another registration already uses this document. A reviewer will "
    "confirm whether they are the same patient."
)
SECTION_FORMS: Final[dict[str, type[SectionForm]]] = {
    "address": AddressForm,
    "contact": EmergencyContactForm,
    "membership": MembershipForm,
}
type SectionOverride = tuple[str, str, SectionForm]


def patient_demographics_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe demographics challenge at the blank screen."""
    return reverse("intake:patient-demographics", args=(clinic_id,))


def _demographics_context(
    clinic_id: UUID,
    overview: object,
    *,
    error: str = "",
    warning: str = "",
) -> dict[str, object]:
    return {
        "clinic_id": clinic_id,
        "demographics_url": patient_demographics_continuation(clinic_id),
        "error": error,
        "list_url": patient_list_continuation(clinic_id),
        "overview": overview,
        "warning": warning,
    }


def _section_form(
    section: str,
    key: str,
    initial: dict[str, object],
    override: SectionOverride | None,
) -> SectionForm:
    """Build one section form with page-unique ids, or the rejected one."""
    if override is not None and override[:2] == (section, key):
        return override[2]
    return SECTION_FORMS[section](initial=initial, auto_id=f"{section}-{key}-%s")


def _address_forms(
    overview: DemographicsProfile, override: SectionOverride | None
) -> tuple[list[dict[str, object]], SectionForm | None]:
    rows: list[dict[str, object]] = []
    for address in overview.addresses:
        form = _section_form(
            "address",
            address.kind,
            {
                "enrollment_id": overview.enrollment_id,
                "expected_version": address.version,
                "kind": address.kind,
                **{
                    field: getattr(address, field)
                    for field in AddressForm.section_fields
                },
            },
            override,
        )
        form.fields["kind"].widget = forms.HiddenInput()
        rows.append(
            {
                "label": ADDRESS_KIND_LABELS[address.kind],
                "form": form,
                "key": address.kind,
                "version": address.version,
            }
        )
    if not overview.free_address_kinds:
        return rows, None
    new = _section_form(
        "address",
        "new",
        {"enrollment_id": overview.enrollment_id, "expected_version": 0},
        override,
    )
    kind_field = new.fields["kind"]
    if isinstance(kind_field, forms.ChoiceField):
        kind_field.choices = [
            (kind, ADDRESS_KIND_LABELS[kind]) for kind in overview.free_address_kinds
        ]
    return rows, new


def _slot_forms(
    section: str,
    overview: DemographicsProfile,
    override: SectionOverride | None,
) -> tuple[list[dict[str, object]], SectionForm | None]:
    views: tuple[object, ...] = (
        overview.emergency_contacts if section == "contact" else overview.memberships
    )
    free = (
        overview.free_contact_slots
        if section == "contact"
        else overview.free_membership_slots
    )
    form_class = SECTION_FORMS[section]
    rows: list[dict[str, object]] = []
    for view in views:
        sequence = getattr(view, "sequence")  # noqa: B009 - two view types
        version = getattr(view, "version")  # noqa: B009 - two view types
        form = _section_form(
            section,
            str(sequence),
            {
                "enrollment_id": overview.enrollment_id,
                "expected_version": version,
                "sequence": sequence,
                **{field: getattr(view, field) for field in form_class.section_fields},
            },
            override,
        )
        rows.append({"form": form, "key": sequence, "version": version})
    if not free:
        return rows, None
    new = _section_form(
        section,
        "new",
        {
            "enrollment_id": overview.enrollment_id,
            "expected_version": 0,
            "sequence": free[0],
        },
        override,
    )
    return rows, new


def _correction_rows(overview: DemographicsProfile) -> list[dict[str, object]]:
    return [
        {
            "version": correction.version,
            "fields": ", ".join(
                str(DEMOGRAPHIC_FIELD_LABELS.get(field, field))
                for field in correction.changed_fields
            ),
            "actor_label": correction.actor_label,
            "source": correction.source,
            "created_at": correction.created_at,
            "reason": correction.reason,
        }
        for correction in overview.corrections
    ]


def _render_demographics(  # noqa: PLR0913 - one profile render needs all
    request: HttpRequest,
    clinic_id: UUID,
    enrollment_id: UUID,
    *,
    error: str = "",
    warning: str = "",
    form: DemographicsForm | None = None,
    section_override: SectionOverride | None = None,
) -> HttpResponseBase:
    overview = demographics_profile(clinic_id=clinic_id, enrollment_id=enrollment_id)
    if form is None:
        form = DemographicsForm(
            data={
                "enrollment_id": str(enrollment_id),
                "expected_version": str(overview.version),
                **overview.values,
                **{
                    f"{field}_status": status
                    for field, status in overview.unknown.items()
                },
            }
        )
        form.is_valid()
    address_rows, new_address = _address_forms(overview, section_override)
    contact_rows, new_contact = _slot_forms("contact", overview, section_override)
    membership_rows, new_membership = _slot_forms(
        "membership", overview, section_override
    )
    context = _demographics_context(clinic_id, overview, error=error, warning=warning)
    context.update(
        {
            "form": form,
            "required_fields": set(overview.required_fields),
            "legal_name_status": UNKNOWN_STATUS_LABELS.get(
                overview.unknown.get("legal_name", ""), ""
            ),
            "identifier_rows": [
                {"view": view, "label": IDENTIFIER_KIND_LABELS[view.kind]}
                for view in overview.identifiers
            ],
            "identifier_form": IdentifierAddForm(
                initial={"enrollment_id": overview.enrollment_id},
                auto_id="identifier-new-%s",
            ),
            "address_rows": address_rows,
            "new_address": new_address,
            "contact_rows": contact_rows,
            "new_contact": new_contact,
            "membership_rows": membership_rows,
            "new_membership": new_membership,
            "correction_rows": _correction_rows(overview),
        }
    )
    return render(request, DEMOGRAPHICS_TEMPLATE, context)


_SECTION_ACTIONS: Final = {
    "save_address": ("address", False),
    "retire_address": ("address", True),
    "save_contact": ("contact", False),
    "retire_contact": ("contact", True),
    "save_membership": ("membership", False),
    "retire_membership": ("membership", True),
}


@privileged_totp_required(patient_demographics_continuation)
@require_http_methods(["GET", "POST"])
def patient_demographics_view(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Manage one enrolled patient's identity, documents, sections and history.

    GET renders only the blank entry state; the enrollment travels in the
    POST body so patient identity never reaches a URL. Identifier values
    are sensitive and masked on this surface.
    """
    try:
        if request.method != "POST":
            _require_clinic(clinic_id)
            return render(
                request,
                DEMOGRAPHICS_TEMPLATE,
                _demographics_context(clinic_id, None),
            )
        action = request.POST.get("action", "")
        if action == "manage":
            enrollment_id = _enrollment_from(EnrollmentForm(data=request.POST))
            return _render_demographics(request, clinic_id, enrollment_id)
        if action == "save":
            return _demographics_save(request, clinic_id)
        if action == "add_identifier":
            return _demographics_add_identifier(request, clinic_id)
        if action == "retire_identifier":
            return _demographics_retire_identifier(request, clinic_id)
        if action in _SECTION_ACTIONS:
            section, retire = _SECTION_ACTIONS[action]
            return _demographics_section(request, clinic_id, section, retire=retire)
        raise Http404
    except PatientAccessDeniedError as error:
        raise Http404 from error


def _demographics_save(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Persist one versioned demographics correction from the POST body."""
    enrollment_id = _enrollment_from(EnrollmentForm(data=request.POST))
    form = DemographicsForm(data=request.POST)
    if not form.is_valid():
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            form=form,
            error=DEMOGRAPHICS_INVALID_MESSAGE,
        )
    failure = ""
    try:
        update_demographics(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            expected_version=form.selected_version(),
            changes=form.cleaned_changes(),
            unknown=form.cleaned_unknown(),
            reason=str(form.cleaned_data.get("reason") or ""),
        )
    except DemographicsStaleError:
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            error=DEMOGRAPHICS_STALE_MESSAGE,
        )
    except DemographicsNameRequiredError:
        form.add_error("legal_name", NAME_REQUIRED_MESSAGE)
        failure = DEMOGRAPHICS_INVALID_MESSAGE
    except DemographicsRequiredError:
        failure = REQUIRED_FIELDS_MESSAGE
    except DemographicsInputError:
        failure = DEMOGRAPHICS_INVALID_MESSAGE
    if failure:
        return _render_demographics(
            request, clinic_id, enrollment_id, form=form, error=failure
        )
    messages.success(request, str(DEMOGRAPHICS_SAVED_MESSAGE))
    return _render_demographics(request, clinic_id, enrollment_id)


def _demographics_section(
    request: HttpRequest,
    clinic_id: UUID,
    section: str,
    *,
    retire: bool,
) -> HttpResponseBase:
    """Save or retire one address, emergency contact or membership."""
    enrollment_id = _enrollment_from(EnrollmentForm(data=request.POST))
    form = SECTION_FORMS[section](data=request.POST)
    key_name = "kind" if section == "address" else "sequence"
    raw_key = str(request.POST.get(key_name, ""))
    override_key = raw_key if request.POST.get("slot") != "new" else "new"
    form.auto_id = f"{section}-{override_key}-%s"
    if not form.is_valid():
        if retire:
            raise Http404
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            error=DEMOGRAPHICS_INVALID_MESSAGE,
            section_override=(section, override_key, form),
        )
    values = None if retire else form.section_values()
    try:
        if isinstance(form, AddressForm):
            save_patient_address(
                clinic_id=clinic_id,
                enrollment_id=enrollment_id,
                kind=form.selected_kind(),
                expected_version=form.selected_version(),
                address=values,
            )
        elif isinstance(form, EmergencyContactForm):
            save_emergency_contact(
                clinic_id=clinic_id,
                enrollment_id=enrollment_id,
                sequence=form.selected_sequence(),
                expected_version=form.selected_version(),
                contact=values,
            )
        elif isinstance(form, MembershipForm):
            save_insurance_membership(
                clinic_id=clinic_id,
                enrollment_id=enrollment_id,
                sequence=form.selected_sequence(),
                expected_version=form.selected_version(),
                membership=values,
            )
    except DemographicsStaleError:
        return _render_demographics(
            request, clinic_id, enrollment_id, error=DEMOGRAPHICS_STALE_MESSAGE
        )
    except DemographicsInputError:
        if retire:
            raise Http404 from None
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            error=DEMOGRAPHICS_INVALID_MESSAGE,
            section_override=(section, override_key, form),
        )
    messages.success(
        request,
        str(SECTION_RETIRED_MESSAGE if retire else SECTION_SAVED_MESSAGE),
    )
    return _render_demographics(request, clinic_id, enrollment_id)


def _demographics_add_identifier(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Record one identifier and surface a same-organization duplicate hint."""
    add_form = IdentifierAddForm(data=request.POST)
    if not add_form.is_valid():
        raise Http404
    enrollment_id = add_form.selected_enrollment()
    try:
        outcome = add_identifier(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            kind=add_form.selected_kind(),
            value=add_form.cleaned_data["value"],
            issuer=add_form.cleaned_data.get("issuer"),
        )
    except IdentifierConflictError:
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            error=DEMOGRAPHICS_STALE_MESSAGE,
        )
    except DemographicsInputError:
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            error=DEMOGRAPHICS_INVALID_MESSAGE,
        )
    warning = (
        str(DUPLICATE_WARNING_MESSAGE)
        if outcome.matching_enrollment_id is not None
        else ""
    )
    messages.success(request, str(IDENTIFIER_SAVED_MESSAGE))
    return _render_demographics(request, clinic_id, enrollment_id, warning=warning)


def _demographics_retire_identifier(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Retire one rendered identifier version without deleting history."""
    retire_form = IdentifierRetireForm(data=request.POST)
    if not retire_form.is_valid():
        raise Http404
    enrollment_id = retire_form.selected_enrollment()
    try:
        retire_identifier(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            kind=retire_form.selected_kind(),
            expected_version=retire_form.selected_version(),
        )
    except DemographicsStaleError:
        return _render_demographics(
            request,
            clinic_id,
            enrollment_id,
            error=DEMOGRAPHICS_STALE_MESSAGE,
        )
    except DemographicsInputError:
        raise Http404 from None
    messages.success(request, str(IDENTIFIER_RETIRED_MESSAGE))
    return _render_demographics(request, clinic_id, enrollment_id)


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
        {"overview": overview},
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
