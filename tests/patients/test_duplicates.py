"""Human duplicate review on real PostgreSQL roles, never inferred identity."""

from __future__ import annotations

import importlib
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import Invoice
from apps.billing.services import create_invoice
from apps.identity.stepup import StepUpRequired
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_adjudication_cases import (
    test_choices_change_only_a_new_survivor_version_and_keep_source_history,
    test_concurrent_sessions_adjudicate_one_pair_once,
)
from patients.duplicate_authority_cases import (
    test_merge_authority_checks_every_stored_role_and_exact_live_session,
    test_merge_guards_refuse_inactive_actor,
    test_merge_mutation_context_refuses_inactive_actor,
)
from patients.duplicate_forward_cases import (
    test_forward_handler_failure_rolls_back_the_complete_merge,
    test_merge_recreates_forward_rows_with_links_and_fresh_access,
)
from patients.duplicate_history_cases import (
    test_survivor_timeline_preserves_clinical_billing_and_artifact_snapshots,
)
from patients.duplicate_integrity_cases import (
    test_native_merge_decision_holds_both_alias_write_gates,
)
from patients.duplicate_portal_cases import (
    test_survivor_portal_preserves_released_history_and_original_receipts,
)
from patients.duplicate_signal_cases import (
    test_dob_and_name_prefix_are_a_retained_human_hint,
    test_forward_waitlist_keeps_integer_source_and_replacement_links,
    test_shared_destination_is_a_hint_without_inferred_verification,
)
from patients.duplicate_support import _merged_pair, _pair
from patients.duplicate_surface_cases import (
    test_native_encounter_repoint_denied_by_acl_and_owner_by_trigger,
    test_native_review_form_applies_displayed_versions_and_requires_csrf,
    test_search_matches_retained_identity_but_selects_survivor,
    test_wrong_clinic_and_unknown_candidate_have_identical_denials,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

__all__ = (
    "test_choices_change_only_a_new_survivor_version_and_keep_source_history",
    "test_concurrent_sessions_adjudicate_one_pair_once",
    "test_dob_and_name_prefix_are_a_retained_human_hint",
    "test_equal_identifiers_create_an_open_candidate_without_merging",
    "test_forward_handler_failure_rolls_back_the_complete_merge",
    "test_forward_waitlist_keeps_integer_source_and_replacement_links",
    "test_merge_authority_checks_every_stored_role_and_exact_live_session",
    "test_merge_guards_refuse_inactive_actor",
    "test_merge_mutation_context_refuses_inactive_actor",
    "test_merge_recreates_forward_rows_with_links_and_fresh_access",
    "test_merge_requires_step_up_at_the_service_boundary",
    "test_native_encounter_repoint_denied_by_acl_and_owner_by_trigger",
    "test_native_merge_decision_holds_both_alias_write_gates",
    "test_native_review_form_applies_displayed_versions_and_requires_csrf",
    "test_new_charge_for_an_aliased_identity_is_rejected",
    "test_search_matches_retained_identity_but_selects_survivor",
    "test_shared_destination_is_a_hint_without_inferred_verification",
    "test_survivor_portal_preserves_released_history_and_original_receipts",
    "test_survivor_timeline_preserves_clinical_billing_and_artifact_snapshots",
    "test_unmerge_separates_views_without_moving_later_survivor_charges",
    "test_verified_merge_keeps_invoice_identity_and_unifies_the_alias_set",
    "test_wrong_clinic_and_unknown_candidate_have_identical_denials",
)


def test_equal_identifiers_create_an_open_candidate_without_merging(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: two separately registered identities with different names/dates.
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        first = services.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Synthetic separate alpha",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
        second = services.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Synthetic separate beta",
            birth_date=date(1992, 2, 2),
            idempotency_key=uuid4(),
        )
        services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=first.enrollment.pk,
            kind="cpf",
            value="52998224725",
        )
        # When: the second identity records the same valid exact identifier.
        outcome = services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            kind="cpf",
            value="529.982.247-25",
        )
        # Then: registration succeeded and a deterministic hint needs a human.
        candidates = services.duplicate_candidates(clinic_id=rbac_graph.clinic_a)
        assert outcome.matching_enrollment_id == first.enrollment.pk
        assert len(candidates) == 1
        assert candidates[0].state == "open"
        assert tuple(candidates[0].signals) == ("exact_identifier",)
        assert {
            candidates[0].enrollment_a_id,
            candidates[0].enrollment_b_id,
        } == {first.enrollment.pk, second.enrollment.pk}
        assert first.patient.pk != second.patient.pk


def test_merge_requires_step_up_at_the_service_boundary(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: an authorized manager and an open deterministic candidate.
    first, _second, candidate = _pair(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(StepUpRequired),
    ):
        # When: the manager attempts a merge without an exact verified session.
        services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic adjudication",
        )
    # Then: the refused merge left the candidate open.
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        assert (
            services.duplicate_candidates(clinic_id=rbac_graph.clinic_a)[0].state
            == "open"
        )


def test_verified_merge_keeps_invoice_identity_and_unifies_the_alias_set(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: distinct patients, a retained charge and a real verified session.
    first, second, candidate = _pair(rbac_graph, services)
    client = verified_client(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        invoice = create_invoice(
            clinic_id=rbac_graph.clinic_a,
            patient_id=second.patient.pk,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
        before = (
            Invoice.objects.filter(pk=invoice.pk)
            .values_list("patient_id", "reference", "amount_minor", "revision")
            .get()
        )
        # When: the exact verified session explicitly adjudicates the pair.
        with services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=session_key(client)
        ):
            decision = services.adjudicate_duplicate(
                clinic_id=rbac_graph.clinic_a,
                candidate_id=candidate.pk,
                decision="merge",
                survivor_patient_id=first.patient.pk,
                field_choices={},
                reason="Synthetic identity reviewed",
            )
        # Then: alias projection combines history; stored billing identity stays.
        projection = services.patient_alias_set(
            clinic_id=rbac_graph.clinic_a, patient_id=first.patient.pk
        )
        assert set(projection.patient_ids) == {first.patient.pk, second.patient.pk}
        assert projection.survivor_patient_id == first.patient.pk
        assert projection.unified_at is not None
        assert decision.adjudicator_id == rbac_graph.clinic_admin
        assert (
            Invoice.objects.filter(pk=invoice.pk)
            .values_list("patient_id", "reference", "amount_minor", "revision")
            .get()
            == before
        )


def test_new_charge_for_an_aliased_identity_is_rejected(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: a deliberately unified identity.
    _first, second, _decision, _key = _merged_pair(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(services.PatientAliasedError),
    ):
        # When: a new operation targets the old identity rather than the survivor.
        create_invoice(
            clinic_id=rbac_graph.clinic_a,
            patient_id=second.patient.pk,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
    # Then: there is no new charge bound to that aliased identity.
    with owner_context(rbac_graph.organization_a):
        assert not Invoice.objects.filter(patient_id=second.patient.pk).exists()
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        with pytest.raises(DatabaseError) as native, transaction.atomic():
            Invoice.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                patient_id=second.patient.pk,
                amount_minor=12345,
                idempotency_key=uuid4(),
                create_fingerprint=b"n" * 32,
            )
        assert getattr(native.value.__cause__, "sqlstate", None) == "23514"
        diagnostic = getattr(native.value.__cause__, "diag", None)
        assert diagnostic is not None
        assert diagnostic.constraint_name == "intake_patient_aliased_check"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.billing_invoice WHERE patient_id=%s",
                [second.patient.pk],
            )
            assert cursor.fetchone() == (0,)


def test_unmerge_separates_views_without_moving_later_survivor_charges(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: an alias and a later charge deliberately written to its survivor.
    first, second, decision, key = _merged_pair(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        invoice = create_invoice(
            clinic_id=rbac_graph.clinic_a,
            patient_id=first.patient.pk,
            amount_minor=54321,
            idempotency_key=uuid4(),
        )
        # When: the verified human deactivates the lineage edge.
        with services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ):
            alias = services.unmerge(
                clinic_id=rbac_graph.clinic_a,
                merge_decision_id=decision.pk,
                reason="Synthetic reversal reviewed",
            )
        # Then: original views separate; the later charge stays on the survivor.
        assert not alias.active
        assert services.patient_alias_set(
            clinic_id=rbac_graph.clinic_a, patient_id=first.patient.pk
        ).patient_ids == (first.patient.pk,)
        assert services.patient_alias_set(
            clinic_id=rbac_graph.clinic_a, patient_id=second.patient.pk
        ).patient_ids == (second.patient.pk,)
        assert Invoice.objects.get(pk=invoice.pk).patient_id == first.patient.pk
        review = services.merge_review(
            clinic_id=rbac_graph.clinic_a, merge_decision_id=decision.pk
        )
        assert invoice.pk in review.later_invoices
