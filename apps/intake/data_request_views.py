"""Patient data-subject request screen and the privacy staff queue.

Every selector travels in a POST body. Patient refusals render the same
access gate for unknown, foreign, malformed and out-of-scope requests, so a
response never tells a delegate whether another person's request exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.consent.models import ConsentPurpose
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError
from apps.intake.data_requests import (
    DATA_REQUEST_DETAILS_MAX,
    DATA_REQUEST_REJECTION_REASONS,
    DataRequestConflictError,
    DataRequestInputError,
    export_data_subject_copy,
    fulfill_data_subject_request,
    patient_data_subject_requests,
    patient_request_scope,
    reject_data_subject_request,
    sharing_information,
    staff_data_subject_requests,
    start_data_subject_request,
    submit_data_subject_request,
)
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase
    from django_stubs_ext import StrOrPromise

    from apps.intake.data_requests import PatientRequestScope, StaffDataRequest

PATIENT_TEMPLATE: Final = "intake/data_requests.html"
STAFF_TEMPLATE: Final = "intake/data_requests_staff.html"
KIND_LABELS: Final[dict[str, tuple[StrOrPromise, StrOrPromise]]] = {
    "access": (
        _("A copy of my data"),
        _(
            "Download the records the clinic released to you. The clinic also "
            "confirms what else it holds about you."
        ),
    ),
    "correction": (
        _("Correct my data"),
        _(
            "Tell the clinic what is wrong. Clinical notes change only through an "
            "amendment by your clinician."
        ),
    ),
    "portability": (
        _("Take my data to another provider"),
        _("Receive the released records in a verifiable package you can share."),
    ),
    "sharing_information": (
        _("Who may handle my data"),
        _(
            "See which kinds of service providers the clinic may use and whether "
            "any of them is active."
        ),
    ),
    "objection": (
        _("Withdraw an optional consent"),
        _(
            "Stop an optional use of your data. Care, records and billing "
            "continue normally."
        ),
    ),
}
STATE_LABELS: Final[dict[str, StrOrPromise]] = {
    "received": _("Received"),
    "in_progress": _("In review"),
    "fulfilled": _("Answered"),
    "rejected": _("Declined"),
}
REJECTION_LABELS: Final[dict[str, StrOrPromise]] = {
    "duplicate": _("It repeats another request."),
    "identity_unverified": _("The clinic could not confirm who made the request."),
    "legal_retention": _("The law requires the clinic to keep this record."),
    "outside_scope": _("The request does not apply to data this clinic holds."),
}
SHARING_STATE_LABELS: Final[dict[str, StrOrPromise]] = {
    "unregistered": _("Not in use"),
    "researched": _("Not in use"),
    "selected_in_plan": _("Not in use"),
    "approved_to_test": _("Testing without patient data"),
    "sandbox": _("Testing without patient data"),
    "production_authorized": _("Authorized, not yet active"),
    "activated": _("Authorized by the clinic"),
    "degraded": _("Authorized by the clinic"),
    "revoked": _("No longer in use"),
}
CAPABILITY_LABELS: Final[dict[str, StrOrPromise]] = {
    "asr": _("Speech transcription"),
    "attachment_scanning": _("File safety scanning"),
    "attachment_storage": _("Document storage"),
    "data_at_rest": _("Storage encryption"),
    "email": _("Email delivery"),
    "hosted_pitr": _("Database backups"),
    "llm_inference": _("AI drafting assistance"),
    "managed_secrets": _("Key and secret storage"),
    "nfse": _("Service invoices (NFS-e)"),
    "object_storage_media": _("Audio and media storage"),
    "pdf_rendering": _("Document generation"),
    "physician_registration": _("Professional registry checks"),
    "pix": _("PIX payments"),
    "psp_card": _("Card payments"),
    "qualified_signing": _("Digital signatures"),
    "rnds": _("National health data network (RNDS)"),
    "signature_verification": _("Signature verification"),
    "sms": _("Text messages (SMS)"),
    "sncr": _("Controlled prescription registry (SNCR)"),
    "tenant_key_management": _("Encryption keys"),
    "tiss": _("Health insurance billing (TISS)"),
    "tls_transport": _("Secure connections"),
    "video": _("Video consultations"),
    "whatsapp": _("WhatsApp messages"),
}


def _purpose_label(value: str) -> str:
    return str(ConsentPurpose(value).label) if value in ConsentPurpose.values else value


def _refused(request: HttpRequest) -> HttpResponse:
    """One gate for every patient refusal; no selector influences the bytes."""
    return render(
        request, "intake/patient_gate.html", {"state": "required"}, status=403
    )


def _patient_context(scope: PatientRequestScope, **extra: object) -> dict[str, object]:
    requests = [
        {
            "row": row,
            "kind_label": KIND_LABELS[row.kind][0],
            "state_label": STATE_LABELS[row.state],
            "purpose_label": _purpose_label(row.purpose) if row.purpose else "",
            "rejection_label": REJECTION_LABELS.get(row.rejection_reason, ""),
            "exportable": row.kind in {"access", "portability"}
            and row.state != "rejected",
        }
        for row in patient_data_subject_requests()
    ]
    sharing = (
        [
            {
                "entry": entry,
                "label": CAPABILITY_LABELS.get(entry.capability_key, gettext("Other")),
                "state_label": SHARING_STATE_LABELS.get(entry.state, entry.state),
            }
            for entry in sharing_information()
        ]
        if "sharing_information" in scope.kinds
        else []
    )
    return {
        "scope": scope,
        "kinds": [
            {
                "value": kind,
                "label": KIND_LABELS[kind][0],
                "hint": KIND_LABELS[kind][1],
                "key": uuid4(),
            }
            for kind in scope.kinds
        ],
        "purposes": [
            {"value": value, "label": _purpose_label(value)}
            for value in scope.objection_purposes
        ],
        "requests": requests,
        "sharing": sharing,
        "details_max": DATA_REQUEST_DETAILS_MAX,
        **extra,
    }


def _parsed_key(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


def _input_error(request: HttpRequest, scope: PatientRequestScope) -> HttpResponse:
    return render(
        request,
        PATIENT_TEMPLATE,
        _patient_context(
            scope,
            error=gettext(
                "Choose one request type and fill in only what it asks for. "
                "A correction needs a description of up to 2,000 characters."
            ),
        ),
        status=400,
    )


def _submit(request: HttpRequest, scope: PatientRequestScope) -> HttpResponseBase:
    key = _parsed_key(request.POST.get("idempotency_key", ""))
    if key is None:
        return _input_error(request, scope)
    try:
        submit_data_subject_request(
            clinic_id=scope.clinic_id,
            kind=request.POST.get("kind", ""),
            details=request.POST.get("details", ""),
            purpose=request.POST.get("purpose", ""),
            idempotency_key=key,
        )
    except PatientAccessDeniedError:
        return _refused(request)
    except DataRequestInputError:
        return _input_error(request, scope)
    except DataRequestConflictError:
        return render(
            request,
            PATIENT_TEMPLATE,
            _patient_context(
                scope,
                error=gettext(
                    "This form was already sent with different answers. "
                    "Review your requests below before sending a new one."
                ),
            ),
            status=409,
        )
    return render(
        request,
        PATIENT_TEMPLATE,
        _patient_context(
            scope,
            notice=gettext(
                "Request received. The clinic answers within 15 days and shows "
                "the progress here."
            ),
        ),
    )


def _export(request: HttpRequest, scope: PatientRequestScope) -> HttpResponseBase:
    try:
        copy = export_data_subject_copy(
            clinic_id=scope.clinic_id,
            request_id=UUID(request.POST.get("request_id", "")),
        )
    except (ValueError, PatientAccessDeniedError, DataRequestInputError):
        return _refused(request)
    except DataRequestConflictError:
        return render(
            request,
            PATIENT_TEMPLATE,
            _patient_context(
                scope,
                error=gettext(
                    "The copy is too large to download here. The clinic team "
                    "will deliver it as part of your request."
                ),
            ),
            status=409,
        )
    return HttpResponse(
        copy.data,
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{copy.file_name}"',
            "Cache-Control": "no-store",
        },
    )


@never_cache
@sensitive_post_parameters("details")
@require_http_methods(["GET", "POST"])
def patient_data_requests(request: HttpRequest) -> HttpResponseBase:
    """List, file and download the session subject's data-subject requests."""
    scope = patient_request_scope()
    if scope is None:
        return _refused(request)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "submit":
            return _submit(request, scope)
        if action == "export":
            return _export(request, scope)
        return _refused(request)
    return render(request, PATIENT_TEMPLATE, _patient_context(scope))


def staff_continuation(clinic_id: UUID) -> str:
    """Resume a privileged step-up at the clinic's request queue."""
    return reverse("intake:data-requests", args=(clinic_id,))


def _patient_names(
    clinic_id: UUID, rows: tuple[StaffDataRequest, ...]
) -> dict[UUID, str]:
    """Show names only to staff who already read demographics in this clinic."""
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
    except CurrentActorError:
        return {}
    enrollments = PatientClinicEnrollment.objects.select_related("patient").filter(
        pk__in={row.enrollment_id for row in rows}, clinic_id=clinic_id
    )
    return {enrollment.pk: enrollment.patient.full_name for enrollment in enrollments}


def _staff_context(clinic_id: UUID, **extra: object) -> dict[str, object]:
    rows = staff_data_subject_requests(clinic_id=clinic_id)
    names = _patient_names(clinic_id, rows)
    return {
        "clinic_id": clinic_id,
        "rows": [
            {
                "row": row,
                "patient_name": names.get(row.enrollment_id, ""),
                "kind_label": KIND_LABELS[row.kind][0],
                "state_label": STATE_LABELS[row.state],
                "purpose_label": _purpose_label(row.purpose) if row.purpose else "",
                "rejection_label": REJECTION_LABELS.get(row.rejection_reason, ""),
            }
            for row in rows
        ],
        "reasons": [
            {"value": value, "label": REJECTION_LABELS[value]}
            for value in DATA_REQUEST_REJECTION_REASONS
        ],
        **extra,
    }


@privileged_totp_required(staff_continuation)
@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def staff_data_requests(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Answer the clinic's data-subject requests within their due dates."""
    try:
        if request.method != "POST":
            return render(request, STAFF_TEMPLATE, _staff_context(clinic_id))
        action = request.POST.get("action", "")
        request_id = UUID(request.POST.get("request_id", ""))
        revision = int(request.POST.get("expected_revision", ""))
        if action == "start":
            start_data_subject_request(
                clinic_id=clinic_id, request_id=request_id, expected_revision=revision
            )
        elif action == "fulfill":
            fulfill_data_subject_request(
                clinic_id=clinic_id, request_id=request_id, expected_revision=revision
            )
        elif action == "reject":
            reject_data_subject_request(
                clinic_id=clinic_id,
                request_id=request_id,
                expected_revision=revision,
                reason_code=request.POST.get("reason", ""),
            )
        else:
            raise Http404
    # DataRequestInputError is a ValueError: answer it before the malformed
    # selectors, which share the unknown-request denial.
    except DataRequestInputError:
        return render(
            request,
            STAFF_TEMPLATE,
            _staff_context(
                clinic_id, error=gettext("Choose one of the listed reasons to decline.")
            ),
            status=400,
        )
    except (ValueError, PatientAccessDeniedError) as error:
        raise Http404 from error
    except DataRequestConflictError:
        return render(
            request,
            STAFF_TEMPLATE,
            _staff_context(
                clinic_id,
                error=gettext(
                    "This request changed since you opened the page. Review its "
                    "current state and try again."
                ),
            ),
            status=409,
        )
    messages.success(request, gettext("Request updated."))
    return redirect(staff_continuation(clinic_id))
