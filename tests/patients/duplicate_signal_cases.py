"""Deterministic DOB/name and contact hints never imply verification or merges."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.intake import services
from apps.intake.models import PatientMergeTransfer
from apps.scheduling.models import WaitlistEntry
from apps.scheduling.waitlist import add_waitlist_entry
from apps.tenancy.db import tenant_context

from identity.lifecycle_test_support import session_key, verified_client
from patient_service_support import runtime_role
from patients.duplicate_support import _pair
from scheduling.appointment_service_support import seed_appointment_setup

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_dob_and_name_prefix_are_a_retained_human_hint(rbac_graph: RbacGraph) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        first, second = (
            services.create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name=name,
                birth_date=date(1990, 1, 1),
                idempotency_key=uuid4(),
            )
            for name in ("Synthetic prefix alpha", "Synthetic prefix beta")
        )
        hints = services.duplicate_candidates(clinic_id=rbac_graph.clinic_a)
        assert len(hints) == 1
        assert hints[0].signals == ["dob+name_prefix"]
        assert hints[0].state == "open"
        assert first.patient.pk != second.patient.pk


def test_shared_destination_is_a_hint_without_inferred_verification(
    rbac_graph: RbacGraph,
) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registrations = tuple(
            services.create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name=name,
                birth_date=None,
                idempotency_key=uuid4(),
            )
            for name in ("Synthetic contact alpha", "Synthetic contact beta")
        )
        contacts = tuple(
            services.save_contact_destination(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                channel="email",
                destination="synthetic-shared-t18@example.invalid",
                expected_version=None,
            )
            for registration in registrations
        )
        hints = services.duplicate_candidates(clinic_id=rbac_graph.clinic_a)
        assert hints[0].signals == ["contact"]
        assert not any(contact.is_verified for contact in contacts)


def test_forward_waitlist_keeps_integer_source_and_replacement_links(
    rbac_graph: RbacGraph,
) -> None:
    first, second, candidate = _pair(rbac_graph, services)
    setup = replace(
        seed_appointment_setup(rbac_graph), enrollment_id=second.enrollment.pk
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        queued = add_waitlist_entry(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=setup.enrollment_id,
            practitioner_id=rbac_graph.physician,
            start_local="2035-06-02T09:00",
            end_local="2035-06-02T10:00",
        )
    key = session_key(verified_client(rbac_graph))
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ),
    ):
        decision = services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic queue reviewed",
        )
        original = WaitlistEntry.objects.get(pk=queued.pk)
        successor = WaitlistEntry.objects.get(patient_id=first.patient.pk)
        assert original.patient_id == second.patient.pk
        assert original.state == "declined"
        assert successor.state == "waiting"
        assert PatientMergeTransfer.objects.filter(
            merge_decision=decision,
            kind="waitlist",
            original_id=str(original.pk),
            replacement_id=str(successor.pk),
        ).exists()
