"""History preservation observed through public clinical and billing seams."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from apps.billing.models import Invoice
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.prescription.models import PrescriptionDocument
from apps.tenancy.db import tenant_context

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_history_support import retained_histories

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_survivor_timeline_preserves_clinical_billing_and_artifact_snapshots(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: two distinct retained charts, issued bytes and exact-money references.
    world = retained_histories(rbac_graph, services)
    with owner_context(rbac_graph.organization_a):
        encounters_before = tuple(
            Encounter.objects.filter(pk__in=world.encounter_ids)
            .order_by("pk")
            .values_list("id", "patient_id", "appointment_id")
        )
        versions_before = tuple(
            ClinicalDocumentVersion.objects.filter(pk__in=world.version_ids)
            .order_by("pk")
            .values_list("id", "content_digest", "revision", "state")
        )
        invoices_before = tuple(
            Invoice.objects.filter(pk__in=world.invoice_ids)
            .order_by("pk")
            .values_list("id", "patient_id", "reference", "amount_minor")
        )
        artifact_before = (
            PrescriptionDocument.objects.filter(pk=world.artifact_id)
            .values_list("patient_id", "input_digest", "pdf_digest")
            .get()
        )
    client = verified_client(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=session_key(client)
        ),
    ):
        # When: a human explicitly merges the identity without moving any history.
        services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=world.candidate.pk,
            decision="merge",
            survivor_patient_id=world.first.patient.pk,
            field_choices={},
            reason="Synthetic retained-history adjudication",
        )
    # Then: the survivor reads both timelines, with original lineage and digests.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        timeline = services.patient_record_timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.first.enrollment.pk,
        )
        assert {row.encounter_id for row in timeline.records} == set(
            world.encounter_ids
        )
        assert {row.patient_id for row in timeline.records} == {
            world.first.patient.pk,
            world.second.patient.pk,
        }
        assert timeline.unified_at is not None
    with owner_context(rbac_graph.organization_a):
        assert (
            tuple(
                Encounter.objects.filter(pk__in=world.encounter_ids)
                .order_by("pk")
                .values_list("id", "patient_id", "appointment_id")
            )
            == encounters_before
        )
        assert (
            tuple(
                ClinicalDocumentVersion.objects.filter(pk__in=world.version_ids)
                .order_by("pk")
                .values_list("id", "content_digest", "revision", "state")
            )
            == versions_before
        )
        assert (
            tuple(
                Invoice.objects.filter(pk__in=world.invoice_ids)
                .order_by("pk")
                .values_list("id", "patient_id", "reference", "amount_minor")
            )
            == invoices_before
        )
        assert (
            PrescriptionDocument.objects.filter(pk=world.artifact_id)
            .values_list("patient_id", "input_digest", "pdf_digest")
            .get()
            == artifact_before
        )
