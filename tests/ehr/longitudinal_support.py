"""Synthetic chart worlds with real professional, care and record policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.ehr.finalization import finalize_version
from apps.ehr.models import ClinicalDocumentVersion, Encounter, MedicationStatement
from apps.ehr.services import create_draft, record_clinical_note
from apps.ehr.tasks import refresh_timeline
from apps.intake.models import PatientClinicEnrollment

from auth.stepup_test_support import verified_request
from ehr.test_addenda import clinician, enrollment_of
from ehr.test_autosave import SECTIONS, as_actor, draft_world, registration
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class ChartWorld:
    version: ClinicalDocumentVersion
    enrollment: UUID
    patient: UUID


def chart_world(graph: RbacGraph) -> ChartWorld:
    version = draft_world(graph)
    patient = version.document.encounter.patient_id
    enrolled = enrollment_of(graph, patient, graph.clinic_a)
    registration(graph, graph.physician, graph.clinic_a)
    return ChartWorld(version, enrolled, patient)


def finalized(
    graph: RbacGraph, version: ClinicalDocumentVersion
) -> ClinicalDocumentVersion:
    request = verified_request(version.author_id)
    with as_actor(graph, version.author_id):
        saved = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=version.revision,
            content=SECTIONS,
        )
        return finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=saved.revision,
            request=request,
        )


def second_draft(
    graph: RbacGraph, world: ChartWorld, user: UUID
) -> ClinicalDocumentVersion:
    with owner_context(graph.organization_a):
        encounter = Encounter.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_id=world.patient,
            physician_id=user,
            unscheduled_reason="documentation_only",
        )
    with as_actor(graph, user):
        return create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=world.version.template_id,
        )


def care_reader(graph: RbacGraph, world: ChartWorld) -> UUID:
    user = clinician(graph, "physician", world.enrollment)
    second_draft(graph, world, user)
    return user


def drain_projection() -> int:
    with runtime_role():
        return refresh_timeline()


def medication_count(graph: RbacGraph) -> int:
    with owner_context(graph.organization_a):
        return MedicationStatement.objects.count()


def enrolled_patient(graph: RbacGraph, enrolled: UUID) -> UUID:
    with owner_context(graph.organization_a):
        return PatientClinicEnrollment.objects.get(pk=enrolled).patient_id
