"""Execute the orders domain witnesses inside the merged authority census."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from pytest_django.fixtures import SettingsWrapper

from orders import (
    test_current_uploader_authority,
    test_http,
    test_identity_matrix,
    test_order_states,
    test_patient_sources,
    test_posture,
    test_rematched_authority,
    test_review,
)
from workflows import test_inbox_read_model as inbox

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_orders_census(proofs: ExecutedProofs, graph: Callable[[], RbacGraph]) -> None:
    """Require actual boundary entry and passing response/state/authority oracles."""
    settings = SettingsWrapper()
    try:
        for oracle in (
            test_http.test_native_result_lifecycle_downloads_only_the_exact_authorized_source,
            test_http.test_lab_http_boundary_replays_without_borrowing_a_staff_session,
            test_posture.test_every_new_populated_relation_denies_cross_organization_reads,
        ):
            proofs.run(oracle.__name__, partial(oracle, graph(), settings))
        for simple_oracle in (
            test_identity_matrix.test_unknown_order_can_be_resolved_only_against_immutable_source_evidence,
            test_order_states.test_order_progression_is_replayed_once_and_cannot_reverse,
            test_patient_sources.test_patient_sources_require_current_acknowledgment_and_exact_patient_release,
            test_rematched_authority.test_proposed_patient_authority_cannot_read_a_different_matched_patient,
            test_rematched_authority.test_matched_patient_reader_keeps_exact_source_access,
            test_rematched_authority.test_stale_proposed_scope_cannot_supersede_a_rematched_source,
            test_rematched_authority.test_matched_patient_clinician_can_correct_without_inheriting_review,
            test_rematched_authority.test_stale_proposed_scope_cannot_replay_a_rematched_source,
            test_rematched_authority.test_raw_correction_guard_rejects_the_historical_proposed_patient,
            # Clinical Inbox (todo 32) acknowledgment, bulk preview and refusals.
            inbox.test_bulk_acknowledgment_needs_previewed_normal_noncritical_results,
            inbox.test_stale_or_foreign_bulk_confirmation_acknowledges_nothing,
            inbox.test_inbox_page_acknowledges_one_result_and_never_bulk_releases,
            inbox.test_inbox_page_bulk_flow_previews_each_item_then_acknowledges,
        ):
            proofs.run(simple_oracle.__name__, partial(simple_oracle, graph()))
        for role in ("receptionist", "nurse", "clinic_admin", "finance"):
            proofs.run(
                f"orders clinical acknowledgment and release:{role}",
                partial(
                    test_review.test_nonclinical_actor_cannot_acknowledge_or_release,
                    graph(),
                    role,
                ),
            )
        for loss in (
            "role_revoked",
            "moved_clinic",
            "permission_narrowed",
            "registration_revoked",
        ):
            for quarantined in (False, True):
                proofs.run(
                    f"orders current uploader authority:{loss}:{quarantined}",
                    partial(
                        test_current_uploader_authority.test_former_uploader_cannot_read_after_current_authority_loss,
                        graph(),
                        loss,
                        quarantined=quarantined,
                    ),
                )
    finally:
        settings.finalize()
