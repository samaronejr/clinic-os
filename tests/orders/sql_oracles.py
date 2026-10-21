"""Bind every orders SQL protocol to real-role, source-backed regressions."""

from orders import (
    test_boundaries,
    test_callbacks,
    test_confidentiality,
    test_current_uploader_authority,
    test_delegate_sources,
    test_http,
    test_identity_matrix,
    test_patient_sources,
    test_posture,
    test_rematched_authority,
    test_review,
)

CASES = {
    case.__name__: case
    for case in (
        test_http.test_native_result_lifecycle_downloads_only_the_exact_authorized_source,
        test_callbacks.test_signed_replay_creates_one_source_version_and_review_task,
        test_callbacks.test_forged_hmac_refuses_before_any_scope_query,
        test_callbacks.test_revoked_principal_cannot_ingest_even_previously_signed_event,
        test_callbacks.test_lab_principal_cannot_select_sources_or_acknowledge,
        test_boundaries.test_cross_tenant_ingest_has_identical_payload_free_denial,
        test_boundaries.test_altered_event_replay_has_no_second_effect,
        test_identity_matrix.test_dob_and_identifier_never_override_an_explicit_conflicting_factor,
        test_delegate_sources.test_delegate_requires_its_exact_release_and_revocation_hides_source,
        test_patient_sources.test_patient_sources_require_current_acknowledgment_and_exact_patient_release,
        test_review.test_correction_retains_old_bytes_but_resets_acknowledgment_and_release,
        test_review.test_source_critical_flag_creates_one_urgent_owned_task_before_release,
        test_review.test_nonclinical_actor_cannot_acknowledge_or_release,
        test_review.test_source_rows_refuse_raw_updates_and_deletes,
        test_posture.test_a_null_creator_cannot_forge_the_private_source_task_bridge,
        test_confidentiality.test_routing_upload_does_not_grant_structured_clinical_read,
        test_current_uploader_authority.test_former_uploader_cannot_read_after_current_authority_loss,
        test_rematched_authority.test_proposed_patient_authority_cannot_read_a_different_matched_patient,
        test_rematched_authority.test_matched_patient_reader_keeps_exact_source_access,
        test_rematched_authority.test_stale_proposed_scope_cannot_supersede_a_rematched_source,
        test_rematched_authority.test_matched_patient_clinician_can_correct_without_inheriting_review,
        test_rematched_authority.test_stale_proposed_scope_cannot_replay_a_rematched_source,
        test_rematched_authority.test_raw_correction_guard_rejects_the_historical_proposed_patient,
    )
}

SQL_PROTOCOL_ORACLES = {
    "clinic_app.orders_audit_scope": [
        "test_native_result_lifecycle_downloads_only_the_exact_authorized_source",
        "test_signed_replay_creates_one_source_version_and_review_task",
    ],
    "clinic_app.orders_can_read_version": [
        "test_native_result_lifecycle_downloads_only_the_exact_authorized_source",
        "test_nonclinical_actor_cannot_acknowledge_or_release",
        "test_lab_principal_cannot_select_sources_or_acknowledge",
        "test_routing_upload_does_not_grant_structured_clinical_read",
        "test_former_uploader_cannot_read_after_current_authority_loss",
        "test_proposed_patient_authority_cannot_read_a_different_matched_patient",
        "test_matched_patient_reader_keeps_exact_source_access",
    ],
    "clinic_app.orders_correlated_order": [
        "test_signed_replay_creates_one_source_version_and_review_task",
        "test_revoked_principal_cannot_ingest_even_previously_signed_event",
    ],
    "clinic_app.orders_delegate_allowed": [
        "test_delegate_requires_its_exact_release_and_revocation_hides_source",
    ],
    "clinic_app.orders_emit_task": [
        "test_source_critical_flag_creates_one_urgent_owned_task_before_release",
    ],
    "clinic_app.orders_event_receipt": [
        "test_signed_replay_creates_one_source_version_and_review_task",
        "test_altered_event_replay_has_no_second_effect",
        "test_stale_proposed_scope_cannot_replay_a_rematched_source",
    ],
    "clinic_app.orders_guard": [
        "test_source_rows_refuse_raw_updates_and_deletes",
        "test_nonclinical_actor_cannot_acknowledge_or_release",
        "test_correction_retains_old_bytes_but_resets_acknowledgment_and_release",
        "test_raw_correction_guard_rejects_the_historical_proposed_patient",
        "test_matched_patient_clinician_can_correct_without_inheriting_review",
    ],
    "clinic_app.orders_ingest_allowed": [
        "test_cross_tenant_ingest_has_identical_payload_free_denial",
        "test_lab_principal_cannot_select_sources_or_acknowledge",
    ],
    "clinic_app.orders_ingest_scope": [
        "test_cross_tenant_ingest_has_identical_payload_free_denial",
        "test_signed_replay_creates_one_source_version_and_review_task",
        "test_stale_proposed_scope_cannot_supersede_a_rematched_source",
        "test_matched_patient_clinician_can_correct_without_inheriting_review",
    ],
    "clinic_app.orders_is_current": [
        "test_correction_retains_old_bytes_but_resets_acknowledgment_and_release",
    ],
    "clinic_app.orders_lab_account": [
        "test_signed_replay_creates_one_source_version_and_review_task",
        "test_revoked_principal_cannot_ingest_even_previously_signed_event",
    ],
    "clinic_app.orders_lab_audit": [
        "test_signed_replay_creates_one_source_version_and_review_task",
    ],
    "clinic_app.orders_patient_sources": [
        "test_patient_sources_require_current_acknowledgment_and_exact_patient_release",
        "test_delegate_requires_its_exact_release_and_revocation_hides_source",
    ],
    "clinic_app.orders_resolve_lab": [
        "test_forged_hmac_refuses_before_any_scope_query",
        "test_revoked_principal_cannot_ingest_even_previously_signed_event",
        "test_signed_replay_creates_one_source_version_and_review_task",
    ],
    "clinic_app.orders_system_task_valid": [
        "test_a_null_creator_cannot_forge_the_private_source_task_bridge",
    ],
    "clinic_app.orders_verify_identity": [
        "test_dob_and_identifier_never_override_an_explicit_conflicting_factor",
    ],
}
