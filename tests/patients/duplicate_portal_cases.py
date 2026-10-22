"""Released lineage remains readable; original release and consent rows do not move."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.billing.services import issue_invoice, release_invoice
from apps.consent.services import (
    patient_receipts,
    prepare_acceptance,
    publish_text,
    record_consent,
)
from apps.intake import services
from apps.intake.alias_access import patient_alias_overview
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.retention.models import RecordRelease
from apps.retention.services import (
    export_patient_records,
    export_staff_records,
    patient_released_records,
    release_version,
    verify_export_package,
)
from apps.tenancy.db import tenant_context
from django.test import Client

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_history_support import retained_histories

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_survivor_portal_preserves_released_history_and_original_receipts(
    rbac_graph: RbacGraph,
) -> None:
    world = retained_histories(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        for invoice_id in world.invoice_ids:
            issue_invoice(
                clinic_id=rbac_graph.clinic_a,
                invoice_id=invoice_id,
                expected_revision=1,
            )
            release_invoice(clinic_id=rbac_graph.clinic_a, invoice_id=invoice_id)
        text = publish_text(
            clinic_id=rbac_graph.clinic_a,
            purpose="teleconsultation",
            text="Synthetic separately recorded consent",
        )
        invitation = services.issue_invitation(
            clinic_id=rbac_graph.clinic_a, enrollment_id=world.second.enrollment.pk
        )
        survivor_invitation = services.issue_invitation(
            clinic_id=rbac_graph.clinic_a, enrollment_id=world.first.enrollment.pk
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        for version_id in world.version_ids:
            release_version(clinic_id=rbac_graph.clinic_a, version_id=version_id)
    with runtime_role():
        original_session = services.redeem_invitation(
            rbac_graph.clinic_a, invitation.secret
        )
        survivor_session = services.redeem_invitation(
            rbac_graph.clinic_a, survivor_invitation.secret
        )
        assert original_session is not None
        assert survivor_session is not None
        with patient_session_context(original_session):
            _offered, offer = prepare_acceptance(text_id=text.pk)
            receipt = record_consent(
                offer=offer, purpose="teleconsultation", accepted=True
            )
    with owner_context(rbac_graph.organization_a):
        release_before = tuple(
            RecordRelease.objects.order_by("id").values_list(
                "id", "patient_id", "version_id"
            )
        )
    key = session_key(verified_client(rbac_graph))
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ),
    ):
        services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=world.candidate.pk,
            decision="merge",
            survivor_patient_id=world.first.patient.pk,
            field_choices={},
            reason="Synthetic released lineage reviewed",
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        package = export_staff_records(
            clinic_id=rbac_graph.clinic_a, patient_id=world.first.patient.pk
        )
        assert package.export.record_count == 2
        assert package.export.manifest["unified_at"]
        assert verify_export_package(data=package.data).ok
    with runtime_role(), patient_session_context(survivor_session):
        assert {row.version_id for row in patient_released_records()} == set(
            world.version_ids
        )
        assert {row.pk for row in patient_receipts()} == {receipt.pk}
        assert patient_receipts()[0].patient_id == world.second.patient.pk
        assert patient_alias_overview() is not None
        package = export_patient_records()
        assert package.export.record_count == 2
        assert verify_export_package(data=package.data).ok
    patient = Client()
    signed_session = patient.session
    signed_session[PATIENT_SESSION_KEY] = str(survivor_session)
    signed_session.save()
    with runtime_role():
        charges = patient.get("/patient/charges/")
    assert charges.status_code == 200
    with owner_context(rbac_graph.organization_a):
        assert (
            tuple(
                RecordRelease.objects.order_by("id").values_list(
                    "id", "patient_id", "version_id"
                )
            )
            == release_before
        )
