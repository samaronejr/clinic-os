"""Two retained clinical histories, real SOAP digests and a rendered artifact."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.billing.services import create_invoice
from apps.ehr.finalization import finalize_version
from apps.ehr.services import (
    create_draft,
    open_encounter,
    publish_template,
    record_clinical_note,
)
from apps.prescription import services as prescriptions
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.tenancy.db import tenant_context

from auth.stepup_test_support import verified_request
from consent.test_purposes import register_physician
from patient_service_support import runtime_role
from patients.duplicate_support import _pair
from renewal.test_document_artifacts import ITEM
from renewal.test_encounters import CONTENT
from scheduling.appointment_service_support import (
    arrive_appointment,
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from types import ModuleType

    from apps.intake.models import DuplicateCandidate
    from apps.intake.patient_creation import PatientRegistration

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class HistoryWorld:
    first: PatientRegistration
    second: PatientRegistration
    candidate: DuplicateCandidate
    encounter_ids: tuple[UUID, ...]
    version_ids: tuple[UUID, ...]
    invoice_ids: tuple[UUID, ...]
    artifact_id: UUID


def retained_histories(graph: RbacGraph, services: ModuleType) -> HistoryWorld:
    first, second, candidate = _pair(graph, services)
    register_physician(graph)
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        template = publish_template(
            clinic_id=graph.clinic_a,
            key="synthetic-t18",
            title="Synthetic retained record",
            prompts=dict.fromkeys(CONTENT, "Synthetic note"),
        )
    encounters: list[UUID] = []
    versions: list[UUID] = []
    invoices: list[UUID] = []
    request = verified_request(graph.physician)
    for index, registration in enumerate((first, second)):
        with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
            appointment = create_synthetic_appointment(
                replace(
                    setup,
                    enrollment_id=registration.enrollment.pk,
                    patient_id=registration.patient.pk,
                ),
                start_local=f"2035-06-02T{9 + index:02d}:00",
                end_local=f"2035-06-02T{10 + index:02d}:00",
            )
            invoices.append(
                create_invoice(
                    clinic_id=graph.clinic_a,
                    patient_id=registration.patient.pk,
                    appointment_id=appointment.pk,
                    amount_minor=12345 + index,
                    idempotency_key=uuid4(),
                ).pk
            )
            arrive_appointment(appointment)
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            encounter = open_encounter(
                clinic_id=graph.clinic_a, appointment_id=appointment.pk
            )
            draft = create_draft(
                clinic_id=graph.clinic_a,
                encounter_id=encounter.pk,
                template_id=template.pk,
            )
            saved = record_clinical_note(
                clinic_id=graph.clinic_a,
                version_id=draft.pk,
                expected_revision=1,
                content=CONTENT,
            )
            finalized = finalize_version(
                clinic_id=graph.clinic_a,
                version_id=saved.pk,
                expected_revision=saved.revision,
                request=request,
            )
            encounters.append(encounter.pk)
            versions.append(finalized.pk)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        scope = {
            "clinic_id": graph.clinic_a,
            "encounter_id": encounters[1],
            "patient_id": second.patient.pk,
            "issuer_id": graph.physician,
        }
        prescription_draft = prescriptions.create_draft(
            **scope, category=SYNTHETIC_CATEGORY
        )
        prescriptions.save_draft(
            **scope,
            draft_id=prescription_draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        artifact = prescriptions.render_document(
            clinic_id=graph.clinic_a,
            encounter_id=encounters[1],
            patient_id=second.patient.pk,
            issuer_id=graph.physician,
            draft_id=prescription_draft.pk,
            expected_version=2,
        )
    return HistoryWorld(
        first,
        second,
        candidate,
        tuple(encounters),
        tuple(versions),
        tuple(invoices),
        artifact.pk,
    )
