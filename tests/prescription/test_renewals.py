"""Renewal is a new physician-reviewed draft, never a remembered class."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ehr.services import ClinicalAccessDeniedError, open_encounter
from apps.intake.models import PatientClinicEnrollment
from apps.prescription.models import DocumentKind
from apps.prescription.renewals import renew_draft
from apps.prescription.services import create_document_draft, draft_items
from apps.tenancy.db import tenant_context

from ehr.test_autosave import registration
from patient_service_support import runtime_role
from prescription.test_document_kinds import rendered, saved
from renewal.test_signatures import setup
from scheduling.appointment_service_support import (
    AppointmentSetup,
    arrived_synthetic_appointment,
)

if TYPE_CHECKING:
    from renewal.test_signatures import SigningSetup

__all__ = ("setup",)
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("existing_kind", [None, DocumentKind.CERTIFICATE])
def test_renewal_retains_source_but_requires_a_new_manual_class(
    setup: SigningSetup,
    existing_kind: str | None,
) -> None:
    # Given: a completed synthetic source and a later visit for the same patient.
    setup.issue()
    graph = setup.graph
    if existing_kind:
        registration(graph, graph.physician, graph.clinic_a)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=setup.document.patient_id, clinic_id=graph.clinic_a
        )
        appointment = arrived_synthetic_appointment(
            AppointmentSetup(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                actor_id=graph.shared_user,
                practitioner_id=graph.physician,
                enrollment_id=enrollment.pk,
                patient_id=setup.document.patient_id,
            ),
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T11:00",
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
        if existing_kind:
            sibling = create_document_draft(
                clinic_id=graph.clinic_a,
                encounter_id=encounter.pk,
                kind=existing_kind,
            )
        # When: the physician explicitly requests a renewal.
        renewed = renew_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            source_document_id=setup.document.pk,
        )
        # Then: directions are copied for review, but no class or issuance carries over.
        assert renewed.category is None
        assert renewed.renewal_source_id == setup.document.pk
        assert draft_items(renewed) == setup.document.frozen_input["items"]
        assert renewed.state == "draft"
        assert renewed.kind == DocumentKind.PRESCRIPTION
        if existing_kind:
            assert sibling.pk != renewed.pk
            assert sibling.kind == existing_kind


def test_completed_non_prescription_source_is_not_renewable(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    registration(graph, graph.physician, graph.clinic_a)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        certificate = saved(graph, setup.scope, DocumentKind.CERTIFICATE)
        setup.document = rendered(graph, setup.scope, certificate)
    setup.issue()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        renew_draft(
            clinic_id=graph.clinic_a,
            encounter_id=setup.scope["encounter_id"],
            source_document_id=setup.document.pk,
        )
