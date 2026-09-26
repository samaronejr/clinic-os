"""Patient/capability adapters for the behavioural classification census."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from apps.billing import presentation as billing_presentation
from apps.billing import services as billing
from apps.consent import services as consent
from apps.intake import patient_access, questionnaire_views, questionnaires
from apps.prescription import verification
from apps.retention import services as retention
from apps.scheduling import (
    patient_authority,
    patient_booking,
    patient_views,
    waitlist,
    waitlist_views,
)
from apps.teleconsult import services as teleconsult
from apps.teleconsult import views as teleconsult_views
from apps.tenancy.middleware import TenantMiddleware
from django.db import transaction
from django.http import HttpResponse

from identity.legacy_parity_support import http_allowed
from identity.nonstaff_differential import DifferentialProbe
from identity.nonstaff_subjects import patient_context_probe

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

    from identity.nonstaff_subjects import NonstaffSubjects


def _patient_middleware(data: NonstaffSubjects) -> object:
    request = data.request()
    request.session[patient_access.PATIENT_SESSION_KEY] = str(data.op.patient_session)

    def response(_request: HttpRequest) -> HttpResponse:
        transaction.set_rollback(True)
        return HttpResponse()

    return TenantMiddleware(response)._patient(request)


def _patient_call(invoke: Callable[[], object], session: UUID | None) -> object:
    # consent_session explicitly rejects a mixed staff/patient GUC context.
    # Enter the real production boundary, which clears ambient staff GUCs;
    # leave the request user and the stored membership variation untouched.
    with patient_access.patient_session_context(session or UUID(int=0)):
        try:
            return invoke()
        finally:
            transaction.set_rollback(True)


def patient_probes(d: NonstaffSubjects) -> list[DifferentialProbe]:
    w = d.legacy
    probes = [
        DifferentialProbe(
            "apps.billing.presentation.patient_charge",
            lambda: billing_presentation.patient_charge(invoice_id=d.op.invoice.pk),
            bool,
        ),
        DifferentialProbe(
            "apps.billing.services.patient_charges", billing.patient_charges, bool
        ),
        DifferentialProbe(
            "apps.consent.services.patient_authority", consent.patient_authority
        ),
        DifferentialProbe(
            "apps.consent.services.prepare_acceptance",
            lambda: consent.prepare_acceptance(text_id=d.acceptance.text_id),
        ),
        DifferentialProbe(
            "apps.consent.services._patient_audit",
            lambda: consent._patient_audit(
                "consent.accepted", d.acceptance.pk, d.authority
            ),
        ),
        DifferentialProbe(
            "apps.consent.services.record_consent",
            lambda: consent.record_consent(
                offer=d.consent_offer, purpose="teleconsultation", accepted=True
            ),
        ),
        DifferentialProbe(
            "apps.consent.services.revoke_consent",
            lambda: consent.revoke_consent(acceptance_id=d.acceptance.pk),
        ),
        DifferentialProbe(
            "apps.intake.patient_access.redeem_invitation",
            lambda: patient_access.redeem_invitation(w.clinic, d.invitation.secret),
            bool,
        ),
        DifferentialProbe(
            "apps.intake.patient_access.patient_session_context",
            lambda: patient_context_probe(d),
            owns_transaction=True,
        ),
        DifferentialProbe(
            "apps.intake.patient_access.patient_session_overview",
            patient_access.patient_session_overview,
            bool,
        ),
        DifferentialProbe(
            "apps.intake.patient_access.end_patient_session",
            lambda: patient_access.end_patient_session(d.op.patient_session),
        ),
        DifferentialProbe(
            "apps.intake.questionnaire_views.patient_questionnaires",
            lambda: questionnaire_views.patient_questionnaires(d.request()),
            http_allowed,
        ),
        DifferentialProbe(
            "apps.intake.questionnaires.patient_responses",
            questionnaires.patient_responses,
            bool,
        ),
        DifferentialProbe(
            "apps.intake.questionnaires.patient_response",
            lambda: questionnaires.patient_response(d.op.response.pk),
        ),
        DifferentialProbe(
            "apps.prescription.verification.patient_documents",
            verification.patient_documents,
            bool,
        ),
        DifferentialProbe(
            "apps.prescription.verification._record_patient_download",
            lambda: verification._record_patient_download(d.rx.document.pk),
        ),
        DifferentialProbe(
            "apps.prescription.verification._patient_session_scope",
            verification._patient_session_scope,
        ),
        DifferentialProbe(
            "apps.prescription.verification.patient_document_download",
            lambda: verification.patient_document_download(
                document_id=d.rx.document.pk
            ),
        ),
        DifferentialProbe(
            "apps.retention.services._session_scope", retention._session_scope
        ),
        DifferentialProbe(
            "apps.retention.services._record_patient_view",
            lambda: retention._record_patient_view(
                session_id=d.op.patient_session,
                organization_id=w.graph.organization_a,
                clinic_id=w.clinic,
                version_id=w.version.pk,
            ),
        ),
        DifferentialProbe(
            "apps.retention.services.patient_released_records",
            retention.patient_released_records,
            bool,
        ),
        DifferentialProbe(
            "apps.scheduling.patient_authority.patient_booking_scope",
            patient_authority.patient_booking_scope,
            bool,
        ),
        DifferentialProbe(
            "apps.scheduling.patient_authority.require_patient_booking_scope",
            patient_authority.require_patient_booking_scope,
        ),
        DifferentialProbe(
            "apps.scheduling.patient_authority.patient_enrollment",
            lambda: patient_authority.patient_enrollment(
                w.appointment.clinic, d.op.enrollment
            ),
            bool,
        ),
        DifferentialProbe(
            "apps.scheduling.patient_authority.lock_patient_availability",
            lambda: patient_authority.lock_patient_availability(
                (w.graph.physician,), ((d.slot.start_at, d.slot.end_at),)
            ),
            bool,
        ),
        DifferentialProbe(
            "apps.scheduling.patient_booking._slot",
            lambda: patient_booking._slot(d.slot.token),
        ),
        DifferentialProbe(
            "apps.scheduling.patient_booking.patient_appointment",
            lambda: patient_booking.patient_appointment(d.appointment.pk),
        ),
        DifferentialProbe(
            "apps.scheduling.patient_booking.reschedule_patient_appointment",
            lambda: patient_booking.reschedule_patient_appointment(
                d.appointment.pk, d.slot.token
            ),
        ),
        DifferentialProbe(
            "apps.scheduling.patient_views._submit",
            lambda: patient_views._submit(
                d.request(
                    {
                        "action": "reschedule",
                        "appointment_id": str(d.appointment.pk),
                        "slot": d.slot.token,
                    }
                ),
                str(d.appointment.pk),
            ),
        ),
        DifferentialProbe(
            "apps.scheduling.waitlist.respond_to_offer",
            lambda: (
                waitlist.respond_to_offer(d.waitlist_offer.pk, accept=True).state
                == "accepted"
            ),
        ),
        DifferentialProbe(
            "apps.scheduling.waitlist_views._patient_submit",
            lambda: (
                waitlist_views._patient_submit(
                    d.request(
                        {"action": "accept", "offer_id": str(d.waitlist_offer.pk)}
                    )
                ).state
                == "accepted"
            ),
        ),
        DifferentialProbe(
            "apps.teleconsult.services._enter_as_patient",
            lambda: teleconsult._enter_as_patient(d.credential),
        ),
        DifferentialProbe(
            "apps.teleconsult.services.patient_session_events",
            lambda: teleconsult.patient_session_events(session_id=d.tc.session.pk),
            bool,
        ),
        DifferentialProbe(
            "apps.teleconsult.views._patient_status",
            lambda: teleconsult_views._patient_status(d.tc.session.pk),
            http_allowed,
        ),
        DifferentialProbe(
            "apps.tenancy.middleware.TenantMiddleware._patient",
            lambda: _patient_middleware(d),
            http_allowed,
            owns_transaction=True,
        ),
    ]
    bound = [replace(p, patient_session=d.op.patient_session) for p in probes]
    # Denials vary patient authority, not staff state. These are separate fixed
    # request scenarios replayed under the same complete staff matrix.
    credential_guards = {
        "apps.consent.services.patient_authority",
        "apps.intake.patient_access.patient_session_overview",
        "apps.intake.questionnaires.patient_response",
        "apps.prescription.verification.patient_document_download",
        "apps.retention.services._session_scope",
        "apps.scheduling.patient_authority.require_patient_booking_scope",
        "apps.teleconsult.views._patient_status",
    }
    scenarios = [
        *bound,
        *(replace(p, expected=False) for p in probes if p.symbol in credential_guards),
    ]
    return [
        p
        if p.owns_transaction
        else replace(
            p,
            invoke=partial(_patient_call, p.invoke, p.patient_session),
            owns_transaction=True,
        )
        for p in scenarios
    ]
