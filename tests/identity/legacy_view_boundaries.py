"""Exercise actual decorated staff views and private clinical selection guards."""

from __future__ import annotations

from importlib import import_module
from inspect import signature
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from apps.ehr import attachment_views, history_views
from apps.ehr import views as ehr_views
from apps.prescription import views as prescription_views
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django_otp.plugins.otp_totp.models import TOTPDevice

from identity.legacy_parity_support import (
    ADMINS,
    LEGACY,
    MANAGERS,
    PHYSICIAN,
    Boundary,
    http_allowed,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from django.http import HttpResponseBase

    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld
    from identity.legacy_prescription_boundaries import PrescriptionSubjects

# GET reaches a real, renderable resource, not a mocked authorization helper.
VIEWS = {
    "apps.billing.views.charges_workspace": MANAGERS,
    "apps.billing.views.invoice_detail": MANAGERS,
    "apps.billing.views.invoice_status": MANAGERS,
    "apps.comms.views.reminders_view": MANAGERS,
    "apps.consent.views.staff_consent": LEGACY,
    "apps.core.views.workspace_home": LEGACY,
    "apps.ehr.attachment_views.attachment_workspace": PHYSICIAN,
    "apps.ehr.history_views.history_workspace": PHYSICIAN,
    "apps.ehr.views.encounter_workspace": PHYSICIAN,
    "apps.identity.clinic_settings_views.clinic_settings": ADMINS,
    "apps.identity.clinic_settings_views.clinic_logo": LEGACY,
    "apps.identity.views.protected_view": LEGACY,
    "apps.identity.preferences_views.preferences_view": LEGACY,
    "apps.identity.otp_views.enroll_view": (*ADMINS, *PHYSICIAN),
    "apps.identity.otp_views.verify_view": (*ADMINS, *PHYSICIAN),
    "apps.identity.stepup_views.step_up_view": LEGACY,
    "apps.intake.questionnaire_views.staff_questionnaires": PHYSICIAN,
    "apps.intake.views.patient_list_view": MANAGERS,
    "apps.intake.views.patient_create_view": MANAGERS,
    "apps.intake.views.patient_contacts_view": MANAGERS,
    "apps.intake.views.patient_access_view": MANAGERS,
    "apps.prescription.views.draft_workspace": PHYSICIAN,
    "apps.prescription.views.review_view": PHYSICIAN,
    "apps.prescription.views.signing_status_view": PHYSICIAN,
    "apps.retention.views.retention_workspace": LEGACY,
    "apps.scheduling.agenda_views.agenda_view": LEGACY,
    "apps.scheduling.booking_views.appointment_create_view": MANAGERS,
    "apps.scheduling.transition_views.appointment_reschedule_view": MANAGERS,
    "apps.scheduling.transition_views.appointment_cancel_view": MANAGERS,
    "apps.scheduling.views.availability_list_view": LEGACY,
    "apps.scheduling.views.availability_retire_view": MANAGERS,
    "apps.scheduling.waitlist_views.waitlist_view": MANAGERS,
    "apps.teleconsult.views.staff_teleconsult": PHYSICIAN,
}


def _view(
    symbol: str,
    subjects: OperationalSubjects,
    prescription: PrescriptionSubjects,
    w: LegacyWorld,
    valid: bool,
) -> object:
    module, name = symbol.rsplit(".", 1)
    view = cast("Callable[..., HttpResponseBase]", getattr(import_module(module), name))
    values: dict[str, UUID] = {
        "clinic_id": w.clinic,
        "appointment_id": w.appointment.pk,
        "encounter_id": w.encounter,
        "invoice_id": subjects.invoice.pk,
        "document_id": prescription.document.pk,
        "operation_id": prescription.operation.pk,
        "availability_id": subjects.availability_id,
    }
    kwargs = {key: values[key] for key in signature(view).parameters if key in values}
    request = w.request
    original_user = request.user
    original_method, original_post = request.method, request.POST
    if symbol == "apps.intake.questionnaire_views.staff_questionnaires":
        request.method = "POST"
        request.POST = (
            RequestFactory()
            .post("/", {"action": "inspect", "response_id": str(subjects.response.pk)})
            .POST
        )
    if symbol == "apps.scheduling.booking_views.appointment_create_view":
        request.method = "POST"
        request.POST = (
            RequestFactory()
            .post("/", {"mode": "prepare", "enrollment_id": str(subjects.enrollment)})
            .POST
        )
    if symbol == "apps.scheduling.views.availability_retire_view":
        request.method = "POST"
    if symbol == "apps.identity.preferences_views.preferences_view":
        request.method = "POST"
        request.POST = (
            RequestFactory().post("/", {"theme": "dark", "density": "compact"}).POST
        )
    if symbol.startswith("apps.identity.otp_views."):
        request.user = w.actor  # A password-authenticated, not yet OTP-verified user.
        if symbol.endswith(".enroll_view"):
            TOTPDevice.objects.filter(user_id=w.actor.pk).update(confirmed=False)
    if symbol == "apps.identity.stepup_views.step_up_view":
        request.session.pop("otp_verified_at", None)
    if not valid:
        request.user = AnonymousUser()
    try:
        return view(request, **kwargs)
    finally:
        request.user = original_user
        request.method, request.POST = original_method, original_post


def boundaries(
    op: OperationalSubjects, rx: PrescriptionSubjects
) -> tuple[Boundary, ...]:
    result = []
    for symbol, allowed in VIEWS.items():

        def invoke(w: LegacyWorld, ok: bool, target: str = symbol) -> object:
            return _view(target, op, rx, w, ok)

        result.append(Boundary(symbol, "http", allowed, invoke, http_allowed))
    return (
        *result,
        Boundary(
            "apps.ehr.attachment_views._workspace",
            "http_private",
            PHYSICIAN,
            lambda w, ok: attachment_views._workspace(w.request, w.clinic_for(ok)),
            http_allowed,
        ),
        Boundary(
            "apps.ehr.history_views._workspace",
            "http_private",
            PHYSICIAN,
            lambda w, ok: history_views._workspace(w.request, w.clinic_for(ok)),
            http_allowed,
        ),
        Boundary(
            "apps.ehr.views._resume",
            "http_private",
            PHYSICIAN,
            lambda w, ok: ehr_views._resume(
                w.request, w.clinic_for(ok), f"ehr.encounter.{w.clinic}"
            ),
            http_allowed,
        ),
        Boundary(
            "apps.ehr.views._show", "http_private", PHYSICIAN, _show, http_allowed
        ),
        Boundary(
            "apps.prescription.views._workspace",
            "http_private",
            PHYSICIAN,
            lambda w, ok: prescription_views._workspace(
                w.request, w.clinic_for(ok), w.encounter
            ),
            http_allowed,
        ),
        Boundary(
            "apps.prescription.views._selected_encounter",
            "http_private",
            PHYSICIAN,
            lambda w, ok: prescription_views._selected_encounter(
                w.request, w.clinic_for(ok), "parity", w.encounter
            ),
        ),
        Boundary(
            "apps.ehr.views._selected",
            "http_private",
            PHYSICIAN,
            lambda w, ok: ehr_views._selected(w.clinic_for(ok), w.encounter),
        ),
        Boundary(
            "apps.prescription.views._draft_document",
            "binding",
            PHYSICIAN,
            lambda w, ok: prescription_views._draft_document(
                rx.document.draft, rx.document.pk if ok else uuid4()
            ),
        ),
        Boundary(
            "apps.prescription.views._issuer_document",
            "http_private",
            PHYSICIAN,
            lambda w, ok: prescription_views._issuer_document(
                w.clinic_for(ok), rx.document.pk
            ),
        ),
    )


def _show(w: LegacyWorld, valid: bool) -> object:
    data = (
        RequestFactory()
        .post("/", {"encounter_id": str(w.encounter if valid else uuid4())})
        .POST
    )
    previous = w.request.POST
    w.request.POST = data
    try:
        return ehr_views._show(w.request, w.clinic, f"ehr.encounter.{w.clinic}")
    finally:
        w.request.POST = previous
