"""Operational services, publishing guards and their actual stored subjects."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.billing import pix
from apps.billing import services as billing
from apps.consent import services as consent
from apps.ehr.finalization import finalize_version
from apps.identity.clinic_configuration import (
    DEFAULT_BRAND,
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.management.context import LifecycleContext, assume_runtime_owner
from apps.identity.models import ClinicConfiguration
from apps.intake import patient_access, questionnaires
from apps.intake import views as intake_views
from apps.intake.models import (
    PatientClinicEnrollment,
    PatientSession,
    QuestionnaireResponse,
)
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.retention import services as retention
from apps.scheduling import appointment_values, availability_retirement, booking_queries
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context
from django.db import connection
from PIL import Image

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from identity.legacy_parity_support import (
    ADMINS,
    LEGACY,
    MANAGERS,
    PHYSICIAN,
    Boundary,
    has_rows,
)
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    from apps.billing.models import Invoice
    from apps.retention.services import ExportPackage

    from identity.legacy_parity_support import LegacyWorld

QUESTIONS = [
    {
        "id": "q_synthetic",
        "label": "Sintetico",
        "type": "text",
        "required": False,
        "max_length": 80,
        "options": [],
    }
]


@dataclass(frozen=True)
class OperationalSubjects:
    enrollment: UUID
    response: QuestionnaireResponse
    invoice: Invoice
    export: ExportPackage
    availability_id: UUID
    patient_session: UUID
    grant_id: UUID


def seed_operational(w: LegacyWorld) -> OperationalSubjects:
    with setup_context(w.graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=w.appointment.patient_id, clinic_id=w.clinic
        ).pk
    with runtime_role(), tenant_context(w.graph.clinic_admin, w.graph.organization_a):
        template = questionnaires.publish_template(
            clinic_id=w.clinic,
            key="synthetic-parity",
            title="Sintetico",
            questions=QUESTIONS,
        )
        text = consent.publish_text(
            clinic_id=w.clinic, purpose="teleconsultation", text="Sintetico"
        )
        ai_text = consent.publish_text(
            clinic_id=w.clinic, purpose="ai_assistance", text="Sintetico IA"
        )
        invitation = issue_invitation(clinic_id=w.clinic, enrollment_id=enrollment)
        buffer = io.BytesIO()
        Image.new("RGB", (1, 1)).save(buffer, format="PNG")
        ClinicConfiguration.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            version=1,
            display_name="Sintetico",
            contact_email="",
            contact_phone="",
            brand_token=DEFAULT_BRAND,
            reminder_hours=24,
            logo_png=buffer.getvalue(),
            published_by_id=w.graph.clinic_admin,
        )
    with runtime_role():
        session = redeem_invitation(w.clinic, invitation.secret)
    assert session is not None
    with runtime_role(), patient_session_context(session):
        _, offer = consent.prepare_acceptance(text_id=text.pk)
        consent.record_consent(offer=offer, purpose="teleconsultation", accepted=True)
        _, refusal_offer = consent.prepare_acceptance(text_id=ai_text.pk)
        consent.record_refusal(offer=refusal_offer, purpose="ai_assistance")
    with runtime_role(), tenant_context(w.graph.shared_user, w.graph.organization_a):
        invoice = billing.create_invoice(
            clinic_id=w.clinic,
            patient_id=w.appointment.patient_id,
            amount_minor=100,
            idempotency_key=uuid4(),
        )
        invoice = billing.issue_invoice(
            clinic_id=w.clinic, invoice_id=invoice.pk, expected_revision=1
        )
        availability = create_availability(
            clinic_id=w.clinic,
            practitioner_id=w.graph.physician,
            start_local="2035-06-03T08:00",
            end_local="2035-06-03T09:00",
            idempotency_key=uuid4(),
        )
    doctor_request = verified_request(w.graph.physician, verified_at=STEP_UP_NOW)
    with runtime_role(), tenant_context(w.graph.physician, w.graph.organization_a):
        finalized = finalize_version(
            clinic_id=w.clinic,
            version_id=w.version.pk,
            expected_revision=w.version.revision,
            request=doctor_request,
        )
        consent.record_ai_disclosure(
            clinic_id=w.clinic,
            encounter_id=w.version.document.encounter_id,
            informed=True,
            refused=False,
        )
        retention.release_version(clinic_id=w.clinic, version_id=finalized.pk)
        response = questionnaires.assign_questionnaire(
            clinic_id=w.clinic,
            enrollment_id=enrollment,
            template_id=template.pk,
            appointment_id=w.appointment.pk,
        )
        export = retention.export_staff_records(
            clinic_id=w.clinic, patient_id=w.appointment.patient_id
        )
    with setup_context(w.graph.organization_a):
        grant_id = PatientSession.objects.get(pk=session).grant_id
    return OperationalSubjects(
        enrollment, response, invoice, export, availability.pk, session, grant_id
    )


def _owner(w: LegacyWorld, valid: bool) -> object:
    context = LifecycleContext(
        operator_id=w.actor.pk,
        organization_id=w.graph.organization_a,
        clinic_id=w.clinic_for(valid),
    )
    with assume_runtime_owner(context), connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    # The owner lifecycle context deliberately restores the owner connection;
    # put the test connection back under the same runtime role for its assertion.
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
    return True


def boundaries(subject: OperationalSubjects) -> tuple[Boundary, ...]:
    return (
        Boundary(
            "apps.scheduling.booking_queries.prepare_booking",
            "operational",
            MANAGERS,
            lambda w, ok: booking_queries.prepare_booking(
                clinic_id=w.clinic_for(ok), enrollment_id=subject.enrollment
            ),
        ),
        Boundary(
            "apps.scheduling.appointment_values._enrollment",
            "binding",
            LEGACY,
            lambda w, ok: appointment_values._enrollment(
                w.appointment.clinic, subject.enrollment if ok else uuid4()
            ),
        ),
        Boundary(
            "apps.scheduling.availability_retirement._target",
            "binding",
            LEGACY,
            lambda w, ok: availability_retirement._target(
                w.graph.organization_a, w.clinic_for(ok), subject.availability_id
            ),
        ),
        Boundary(
            "apps.scheduling.availability_retirement.retire_availability",
            "operational",
            MANAGERS,
            lambda w, ok: availability_retirement.retire_availability(
                clinic_id=w.clinic_for(ok), availability_id=subject.availability_id
            ),
        ),
        Boundary(
            "apps.intake.patient_access.revoke_patient_access",
            "operational",
            MANAGERS,
            lambda w, ok: patient_access.revoke_patient_access(
                clinic_id=w.clinic_for(ok),
                enrollment_id=subject.enrollment,
                grant_id=subject.grant_id,
            ),
        ),
        Boundary(
            "apps.intake.views._render_access",
            "http_private",
            MANAGERS,
            lambda w, ok: intake_views._render_access(
                w.request, w.clinic_for(ok), subject.enrollment
            ),
        ),
        Boundary(
            "apps.intake.questionnaires.completion_status",
            "resolver",
            LEGACY,
            lambda w, ok: questionnaires.completion_status(
                clinic_id=w.clinic_for(ok), enrollment_id=subject.enrollment
            ),
            has_rows,
        ),
        Boundary(
            "apps.billing.services._clinic",
            "operational",
            MANAGERS,
            lambda w, ok: billing._clinic(w.clinic_for(ok)),
        ),
        Boundary(
            "apps.billing.services._invoice",
            "operational",
            MANAGERS,
            lambda w, ok: billing._invoice(w.clinic_for(ok), subject.invoice.pk),
        ),
        Boundary(
            "apps.billing.pix.prepare_pix_charge",
            "operational",
            MANAGERS,
            lambda w, ok: pix.prepare_pix_charge(
                clinic_id=w.clinic_for(ok), invoice_id=subject.invoice.pk
            ),
        ),
        Boundary(
            "apps.consent.services.publish_text",
            "operational",
            ADMINS,
            lambda w, ok: consent.publish_text(
                clinic_id=w.clinic_for(ok),
                purpose="teleconsultation",
                text="Sintetico parity",
            ),
        ),
        Boundary(
            "apps.consent.services.publish_notice",
            "operational",
            ADMINS,
            lambda w, ok: consent.publish_notice(
                clinic_id=w.clinic_for(ok),
                topic="ai_use",
                text="Sintetico parity aviso",
            ),
        ),
        Boundary(
            "apps.consent.services.staff_receipts",
            "operational",
            LEGACY,
            lambda w, ok: consent.staff_receipts(
                clinic_id=w.clinic_for(ok), enrollment_id=subject.enrollment
            ),
            has_rows,
        ),
        Boundary(
            "apps.consent.services.staff_refusals",
            "operational",
            LEGACY,
            lambda w, ok: consent.staff_refusals(
                clinic_id=w.clinic_for(ok), enrollment_id=subject.enrollment
            ),
            has_rows,
        ),
        Boundary(
            "apps.consent.services.record_ai_disclosure",
            "operational",
            PHYSICIAN,
            lambda w, ok: consent.record_ai_disclosure(
                clinic_id=w.clinic_for(ok),
                encounter_id=w.encounter_for(ok),
                informed=True,
                refused=False,
            ),
        ),
        Boundary(
            "apps.consent.services.ai_disclosure_status",
            "operational",
            LEGACY,
            lambda w, ok: consent.ai_disclosure_status(
                clinic_id=w.clinic_for(ok), encounter_id=w.encounter_for(ok)
            ),
            has_rows,
        ),
        Boundary(
            "apps.consent.services.acknowledge_participant",
            "operational",
            PHYSICIAN,
            lambda w, ok: consent.acknowledge_participant(
                clinic_id=w.clinic_for(ok),
                session_id=w.encounter_for(ok),
                participant_kind="companion",
            ),
        ),
        Boundary(
            "apps.consent.services.consent_for_future_use",
            "operational",
            LEGACY,
            lambda w, ok: consent.consent_for_future_use(
                clinic_id=w.clinic_for(ok),
                enrollment_id=subject.enrollment,
                purpose="teleconsultation",
            ),
            has_rows,
        ),
        Boundary(
            "apps.identity.clinic_configuration.publish_configuration",
            "operational",
            ADMINS,
            lambda w, ok: publish_configuration(
                clinic_id=w.clinic_for(ok),
                expected_version=1,
                content=ConfigurationContent("Sintetico parity"),
            ),
        ),
        Boundary(
            "apps.identity.management.context.assume_runtime_owner",
            "owner",
            ("owner",),
            _owner,
        ),
        Boundary(
            "apps.intake.questionnaires.publish_template",
            "operational",
            ADMINS,
            lambda w, ok: questionnaires.publish_template(
                clinic_id=w.clinic_for(ok),
                key="parity-second",
                title="Sintetico",
                questions=QUESTIONS,
            ),
        ),
        Boundary(
            "apps.intake.questionnaires.assign_questionnaire",
            "operational",
            PHYSICIAN,
            lambda w, ok: questionnaires.assign_questionnaire(
                clinic_id=w.clinic_for(ok),
                enrollment_id=subject.enrollment,
                template_id=subject.response.template_id,
            ),
        ),
        Boundary(
            "apps.intake.questionnaires.published_templates",
            "operational",
            PHYSICIAN,
            lambda w, ok: questionnaires.published_templates(
                clinic_id=w.clinic_for(ok)
            ),
        ),
        Boundary(
            "apps.intake.questionnaires.appointment_enrollment",
            "operational",
            PHYSICIAN,
            lambda w, ok: questionnaires.appointment_enrollment(
                clinic_id=w.clinic_for(ok), appointment_id=w.appointment.pk
            ),
        ),
        Boundary(
            "apps.intake.questionnaires.clinical_response",
            "operational",
            PHYSICIAN,
            lambda w, ok: questionnaires.clinical_response(
                clinic_id=w.clinic_for(ok), response_id=subject.response.pk
            ),
        ),
        Boundary(
            "apps.retention.services.release_version",
            "operational",
            PHYSICIAN,
            lambda w, ok: retention.release_version(
                clinic_id=w.clinic_for(ok), version_id=w.version.pk
            ),
        ),
        Boundary(
            "apps.retention.services.export_staff_records",
            "operational",
            PHYSICIAN,
            lambda w, ok: retention.export_staff_records(
                clinic_id=w.clinic_for(ok), patient_id=w.appointment.patient_id
            ),
        ),
        # The physician fallback is essential: the old harvested role tuple
        # incorrectly represented this real boundary as admin-only.
        Boundary(
            "apps.retention.services.verify_stored_export",
            "operational",
            (*ADMINS, *PHYSICIAN),
            lambda w, ok: retention.verify_stored_export(
                clinic_id=w.clinic_for(ok),
                export_id=subject.export.export.pk,
                data=subject.export.data,
            ),
        ),
    )
