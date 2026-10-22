"""Real two-clinic duplicate transfers and retained reversal review."""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import Invoice
from apps.billing.services import create_invoice
from apps.intake.models import (
    PatientAccessGrant,
    PatientChannelPreference,
    PatientClinicEnrollment,
    PatientContact,
    PatientMergeTransfer,
)
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context
from django.urls import reverse

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_support import _pair
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


def _enroll_in_second_clinic(
    graph: RbacGraph, patient_id: UUID
) -> PatientClinicEnrollment:
    with owner_context(graph.organization_a):
        return PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            patient_id=patient_id,
            idempotency_key=uuid4(),
            create_fingerprint=b"m" * 32,
        )


@pytest.mark.parametrize("survivor_has_contact", [False, True])
def test_two_clinic_merge_transfers_one_contact_and_scopes_preferences_and_grants(
    rbac_graph: RbacGraph,
    survivor_has_contact: bool,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: the same identities are enrolled in two currently authorized clinics.
    first, second, candidate = _pair(rbac_graph, services)
    _enroll_in_second_clinic(rbac_graph, first.patient.pk)
    second_b = _enroll_in_second_clinic(rbac_graph, second.patient.pk)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        old_contact = services.save_contact_destination(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            channel="email",
            destination="synthetic-original@example.invalid",
            expected_version=None,
        )
        services.verify_contact(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            channel="email",
            expected_version=1,
        )
        if survivor_has_contact:
            services.save_contact_destination(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=first.enrollment.pk,
                channel="email",
                destination="synthetic-survivor@example.invalid",
                expected_version=None,
            )
        old_grants = []
        for enrollment in (second.enrollment, second_b):
            services.set_purpose_channel(
                clinic_id=enrollment.clinic_id,
                enrollment_id=enrollment.pk,
                purpose="booking_confirmation",
                channel="email",
            )
            old_grants.append(
                services.issue_invitation(
                    clinic_id=enrollment.clinic_id, enrollment_id=enrollment.pk
                ).grant
            )
    key = session_key(verified_client(rbac_graph))
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ),
    ):
        # When: one decision transfers forward work across both enrolled clinics.
        decision = services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic two-clinic contact review",
        )
        # Then: the organization-owned contact has exactly one retained receipt.
        contact_links = PatientMergeTransfer.objects.filter(
            merge_decision=decision, kind="contact", original_id=str(old_contact.pk)
        )
        assert contact_links.count() == 1
        successor = PatientContact.objects.get(
            patient_id=first.patient.pk, channel="email"
        )
        assert contact_links.get().replacement_id == str(successor.pk)
        assert contact_links.get().clinic_id == rbac_graph.clinic_a
        assert contact_links.get().action == (
            "kept_survivor" if survivor_has_contact else "recreated"
        )
        assert not successor.is_verified
        assert successor.destination == (
            "synthetic-survivor@example.invalid"
            if survivor_has_contact
            else "synthetic-original@example.invalid"
        )
        assert PatientContact.objects.get(pk=old_contact.pk).is_verified
        assert (
            PatientContact.objects.get(pk=old_contact.pk).patient_id
            == second.patient.pk
        )
        # Clinic-owned work is still handled once in each clinic, never globally.
        for kind in ("preference", "access_grant"):
            assert set(
                PatientMergeTransfer.objects.filter(
                    merge_decision=decision, kind=kind
                ).values_list("clinic_id", flat=True)
            ) == {rbac_graph.clinic_a, rbac_graph.clinic_b}
            assert (
                PatientMergeTransfer.objects.filter(
                    merge_decision=decision, kind=kind
                ).count()
                == 2
            )
        assert not PatientChannelPreference.objects.filter(
            patient_id__in=(first.patient.pk, second.patient.pk), opted_in=True
        ).exists()
        assert (
            PatientAccessGrant.objects.filter(
                pk__in=[grant.pk for grant in old_grants], revoked_at__isnull=False
            ).count()
            == 2
        )
        assert {row.grant.clinic_id for row in decision.issued_invitations} == {
            rbac_graph.clinic_a,
            rbac_graph.clinic_b,
        }
        assert len(decision.issued_invitations) == 2


def test_two_clinic_unmerge_reviews_b_successor_and_later_rows_without_rebinding(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: A owns the decision, while B has distinct forward and later work.
    first, second, candidate = _pair(rbac_graph, services)
    _enroll_in_second_clinic(rbac_graph, first.patient.pk)
    second_b = _enroll_in_second_clinic(rbac_graph, second.patient.pk)
    _setup_a, setup_b = seed_cross_clinic_appointment_setups(rbac_graph)
    client = verified_client(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        original = create_synthetic_appointment(
            replace(
                setup_b,
                enrollment_id=second_b.pk,
                patient_id=second.patient.pk,
            )
        )
        with services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=session_key(client)
        ):
            decision = services.adjudicate_duplicate(
                clinic_id=rbac_graph.clinic_a,
                candidate_id=candidate.pk,
                decision="merge",
                survivor_patient_id=first.patient.pk,
                field_choices={},
                reason="Synthetic two-clinic reversal review",
            )
        link = PatientMergeTransfer.objects.get(
            merge_decision=decision, kind="appointment", original_id=str(original.pk)
        )
        later_b = create_invoice(
            clinic_id=rbac_graph.clinic_b,
            patient_id=first.patient.pk,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
        later_a = create_invoice(
            clinic_id=rbac_graph.clinic_a,
            patient_id=first.patient.pk,
            amount_minor=54321,
            idempotency_key=uuid4(),
        )
    with owner_context(rbac_graph.organization_a):
        appointment_bindings = tuple(
            Appointment.objects.order_by("id").values_list(
                "id", "clinic_id", "patient_id", "practitioner_id", "start_at", "end_at"
            )
        )
        invoice_bindings = tuple(
            Invoice.objects.order_by("id").values_list(
                "id", "clinic_id", "patient_id", "reference", "amount_minor", "revision"
            )
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=session_key(client)
        ),
    ):
        # When: the human reverses in A and reviews the retained work in B.
        services.unmerge(
            clinic_id=rbac_graph.clinic_a,
            merge_decision_id=decision.pk,
            reason="Synthetic later work retained",
        )
        review_b = services.merge_review(
            clinic_id=rbac_graph.clinic_b, merge_decision_id=decision.pk
        )
        # Then: B has its own successor/link and invoice, not A's later invoice.
        assert review_b.clinic.pk == rbac_graph.clinic_b
        assert {row.pk for row in review_b.transfers} == {link.pk}
        assert str(link.replacement_id) in {
            str(pk) for pk in review_b.later_appointments
        }
        assert review_b.later_invoices == (later_b.pk,)
        assert later_a.pk not in review_b.later_invoices
    with runtime_role():
        response = client.post(
            reverse("intake:patient-duplicates", args=(rbac_graph.clinic_b,)),
            {"action": "review_decision", "merge_decision_id": decision.pk},
        )
    assert response.status_code == 200
    assert str(link.replacement_id) in response.content.decode()
    assert str(later_b.pk) in response.content.decode()
    assert str(later_a.pk) not in response.content.decode()
    with owner_context(rbac_graph.organization_a):
        assert (
            tuple(
                Appointment.objects.order_by("id").values_list(
                    "id",
                    "clinic_id",
                    "patient_id",
                    "practitioner_id",
                    "start_at",
                    "end_at",
                )
            )
            == appointment_bindings
        )
        assert (
            tuple(
                Invoice.objects.order_by("id").values_list(
                    "id",
                    "clinic_id",
                    "patient_id",
                    "reference",
                    "amount_minor",
                    "revision",
                )
            )
            == invoice_bindings
        )
    # Current authority is checked for each offered clinic and for the direct path.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        limited = services.merge_review(
            clinic_id=rbac_graph.clinic_a, merge_decision_id=decision.pk
        )
        assert {clinic.pk for clinic in limited.review_clinics} == {rbac_graph.clinic_a}
        for decision_id in (decision.pk, uuid4()):
            with pytest.raises(services.PatientAccessDeniedError):
                services.merge_review(
                    clinic_id=rbac_graph.clinic_b, merge_decision_id=decision_id
                )
