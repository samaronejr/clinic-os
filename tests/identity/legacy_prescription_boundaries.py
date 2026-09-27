"""Rendered prescription and signing subjects, without a provider call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.identity.models import PhysicianProfile
from apps.identity.physician_registry import SigningIdentity
from apps.identity.physician_verification import verify_physician_for_signing
from apps.prescription import services, signing, verification
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import render_document
from apps.tenancy.db import tenant_context

from identity.legacy_parity_support import PHYSICIAN, Boundary
from patient_service_support import runtime_role
from renewal.test_document_artifacts import ITEM
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    from apps.prescription.models import PrescriptionDocument, SignatureOperation

    from identity.legacy_parity_support import LegacyWorld


@dataclass(frozen=True)
class PrescriptionSubjects:
    document: PrescriptionDocument
    operation: SignatureOperation


def seed_prescription(w: LegacyWorld) -> PrescriptionSubjects:
    with setup_context(w.graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=w.graph.organization_a,
            user_id=w.graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-PARITY",
            signing_subject=f"synthetic:physician:{w.graph.physician}",
        )
    with runtime_role(), tenant_context(w.graph.physician, w.graph.organization_a):
        scope = {
            "clinic_id": w.clinic,
            "encounter_id": w.encounter,
            "patient_id": w.appointment.patient_id,
            "issuer_id": w.graph.physician,
        }
        draft = services.create_draft(**scope, category=SYNTHETIC_CATEGORY)
        services.save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        document = render_document(
            clinic_id=w.clinic,
            encounter_id=w.encounter,
            patient_id=w.appointment.patient_id,
            issuer_id=w.graph.physician,
            draft_id=draft.pk,
            expected_version=2,
        )
        operation = signing.initiate_signature(
            clinic_id=w.clinic, document_id=document.pk
        )
        # Resolve the real related row while the seed physician has authority;
        # later probes must enter the document-binding guard itself.
        assert document.draft.pk == draft.pk
    return PrescriptionSubjects(document, operation)


def boundaries(subject: PrescriptionSubjects) -> tuple[Boundary, ...]:
    return (
        Boundary(
            "apps.identity.physician_verification.verify_physician_for_signing",
            "prescription",
            PHYSICIAN,
            lambda w, ok: verify_physician_for_signing(
                request=w.request,
                clinic_id=w.clinic_for(ok),
                encounter_id=w.encounter,
                signer=SigningIdentity(
                    w.graph.physician, subject.operation.signer_subject, synthetic=True
                ),
                synthetic=True,
            ),
        ),
        Boundary(
            "apps.prescription.signing._prepare_signature",
            "prescription",
            PHYSICIAN,
            lambda w, ok: signing._prepare_signature(
                request=w.request,
                clinic_id=w.clinic_for(ok),
                operation_id=subject.operation.pk,
            ),
        ),
        Boundary(
            "apps.prescription.signing.initiate_signature",
            "prescription",
            PHYSICIAN,
            lambda w, ok: signing.initiate_signature(
                clinic_id=w.clinic_for(ok), document_id=subject.document.pk
            ),
        ),
        Boundary(
            "apps.prescription.verification._issuer_document",
            "prescription",
            PHYSICIAN,
            lambda w, ok: verification._issuer_document(
                clinic_id=w.clinic_for(ok), document_id=subject.document.pk
            ),
        ),
        Boundary(
            "apps.prescription.signing._document_for_signing",
            "prescription",
            PHYSICIAN,
            lambda w, ok: signing._document_for_signing(
                w.clinic_for(ok), subject.document.pk
            ),
        ),
        Boundary(
            "apps.prescription.signing.signature_status",
            "prescription",
            PHYSICIAN,
            lambda w, ok: signing.signature_status(
                clinic_id=w.clinic_for(ok), operation_id=subject.operation.pk
            ),
        ),
    )
