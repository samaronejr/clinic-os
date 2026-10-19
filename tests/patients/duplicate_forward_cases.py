"""Forward rows are cancelled/recreated explicitly, never silently repointed."""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.audit.models import AuditEvent
from apps.intake import merge_execution
from apps.intake.models import (
    MergeDecision,
    PatientAccessGrant,
    PatientAlias,
    PatientChannelPreference,
    PatientContact,
    PatientDemographics,
    PatientMergeTransfer,
)
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_support import _pair
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.intake.patient_access import IssuedInvitation

    from rbac_fixtures import RbacGraph


def test_merge_recreates_forward_rows_with_links_and_fresh_access(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: a future booking, a verified destination, an opt-in and an invitation.
    first, second, candidate = _pair(rbac_graph, services)
    setup = seed_appointment_setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        appointment = create_synthetic_appointment(
            replace(
                setup,
                enrollment_id=second.enrollment.pk,
                patient_id=second.patient.pk,
            )
        )
        contact = services.save_contact_destination(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            channel="email",
            destination="synthetic-t18@example.invalid",
            expected_version=None,
        )
        services.verify_contact(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            channel="email",
            expected_version=1,
        )
        services.set_purpose_channel(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            purpose="booking_confirmation",
            channel="email",
        )
        invitation = services.issue_invitation(
            clinic_id=rbac_graph.clinic_a, enrollment_id=second.enrollment.pk
        )
    client = verified_client(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=session_key(client)
        ),
    ):
        # When: the human adjudicates the identity and its explicit forward work.
        decision = services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic forward work reviewed",
        )
        # Then: each old binding remains, with a linked successor on the survivor.
        old = Appointment.objects.get(pk=appointment.pk)
        assert old.patient_id == second.patient.pk
        assert old.status == "cancelled"
        replacement = Appointment.objects.get(
            patient_id=first.patient.pk, status="scheduled"
        )
        assert replacement.pk != old.pk
        assert (replacement.start_at, replacement.end_at) == (old.start_at, old.end_at)
        assert PatientMergeTransfer.objects.filter(
            merge_decision=decision,
            kind="appointment",
            original_id=str(old.pk),
            replacement_id=str(replacement.pk),
        ).exists()
        assert PatientContact.objects.get(pk=contact.pk).patient_id == second.patient.pk
        cloned_contact = PatientContact.objects.get(
            patient_id=first.patient.pk, channel="email"
        )
        assert not cloned_contact.is_verified
        assert not PatientChannelPreference.objects.filter(
            patient_id=second.patient.pk, opted_in=True
        ).exists()
        assert not PatientChannelPreference.objects.filter(
            patient_id=first.patient.pk, opted_in=True
        ).exists()
        assert (
            PatientAccessGrant.objects.get(pk=invitation.grant.pk).revoked_at
            is not None
        )
        assert decision.issued_invitations
        assert all(
            row.grant.patient_id == first.patient.pk
            for row in decision.issued_invitations
        )
    with owner_context(rbac_graph.organization_a):
        assert PatientMergeTransfer.objects.filter(merge_decision=decision).count() >= 4


class ForwardTransferError(Exception):
    """Abort an actual scheduling transfer before the intake handler completes."""


def test_forward_handler_failure_rolls_back_the_complete_merge(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: a real future booking and a fresh adjudication session.
    first, second, candidate = _pair(rbac_graph, services)
    setup = replace(
        seed_appointment_setup(rbac_graph),
        enrollment_id=second.enrollment.pk,
        patient_id=second.patient.pk,
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        appointment = create_synthetic_appointment(setup)
    key = session_key(verified_client(rbac_graph))
    with owner_context(rbac_graph.organization_a):
        before = tuple(
            Appointment.objects.order_by("id").values_list("id", "patient_id", "status")
        )
        audit_count = AuditEvent.objects.count()

    def fail_transfer(
        *, clinic_id: UUID, merge_decision_id: UUID
    ) -> tuple[IssuedInvitation, ...]:
        raise ForwardTransferError

    monkeypatch.setattr(merge_execution, "transfer_intake", fail_transfer)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ),
        pytest.raises(ForwardTransferError),
    ):
        # When: intake fails after the real scheduling handler changed rows.
        services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic forward failure rollback",
        )
    # Then: no decision, alias, survivor version, transfer or audit survives.
    with owner_context(rbac_graph.organization_a):
        assert (
            tuple(
                Appointment.objects.order_by("id").values_list(
                    "id", "patient_id", "status"
                )
            )
            == before
        )
        assert Appointment.objects.get(pk=appointment.pk).status == "scheduled"
        assert not MergeDecision.objects.filter(candidate=candidate).exists()
        assert not PatientAlias.objects.exists()
        assert not PatientDemographics.objects.filter(patient=first.patient).exists()
        assert not PatientMergeTransfer.objects.exists()
        assert AuditEvent.objects.count() == audit_count
