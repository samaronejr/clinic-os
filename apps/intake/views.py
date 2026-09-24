"""Accessible POST-only patient search and registration screens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

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
    CONFLICTING_KEY_MESSAGE,
    INVALID_CREATE_MESSAGE,
    AccessRevokeForm,
    ContactChannelForm,
    ContactDestinationForm,
    ContactPreferenceForm,
    ContactVerifyForm,
    EnrollmentForm,
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
    create_patient,
    issue_invitation,
    revoke_patient_access,
    save_contact_destination,
    search_patients,
    set_purpose_channel,
    verify_contact,
)

if TYPE_CHECKING:
    from datetime import date

    from django.http import HttpRequest

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
    return {
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
        "status": _results_status(results, query),
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
            results = search_patients(
                clinic_id=clinic_id,
                query=form.cleaned_data["q"],
                page=form.selected_page(),
                birth_date=birth_date,
            )
        except PatientAccessDeniedError as error:
            raise Http404 from error
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
    return redirect(target, permanent=False)


@privileged_totp_required(patient_create_continuation)
@require_http_methods(["GET", "POST"])
def patient_create_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Issue one registration key on GET and register one patient on POST."""
    if request.method != "POST":
        _require_clinic(clinic_id)
        blank = PatientCreateForm()
        return render(
            request,
            "intake/patient_create.html",
            _create_context(clinic_id, blank, str(uuid4())),
        )
    submitted_key = request.POST.get("idempotency_key", "")
    form = PatientCreateForm(data=request.POST)
    if form.is_valid():
        try:
            create_patient(
                clinic_id=clinic_id,
                full_name=form.cleaned_data["full_name"],
                birth_date=form.cleaned_data["birth_date"],
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        except PatientAccessDeniedError as error:
            raise Http404 from error
        except PatientIdempotencyConflictError:
            form.add_error(None, CONFLICTING_KEY_MESSAGE)
        except (PatientBirthDateError, PatientCreateInputError):
            form.add_error(None, INVALID_CREATE_MESSAGE)
        else:
            response = _created_response(request, clinic_id)
            if response.status_code != NO_CONTENT:
                response.status_code = SEE_OTHER
            return response
    return render(
        request,
        "intake/patient_create.html",
        _create_context(clinic_id, form, submitted_key),
    )


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
