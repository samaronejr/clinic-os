"""Executable protocols for every interop SQL authority, guard and resolver."""

from interop.test_authority import (
    test_commit_and_rollback_require_this_actors_fresh_step_up,
    test_database_refuses_illegal_edges_stale_sequences_and_history_edits,
    test_import_authority_follows_the_staff_management_bundle,
    test_import_authority_reads_metadata_but_never_clinical_payloads,
    test_org_admin_prepares_and_signs_off_but_cannot_commit_identity,
    test_rollback_window_is_enforced_by_the_database,
    test_stored_source_reaches_only_the_pipeline_of_an_in_flight_job,
    test_unknown_and_foreign_jobs_share_one_denial,
)
from interop.test_external_api import (
    test_foreign_and_unknown_external_ids_return_the_identical_404,
    test_revocation_rotation_and_approver_authority_take_effect_immediately,
    test_scopes_bound_every_resource_and_malformed_tokens_are_identical,
    test_token_endpoint_refuses_with_rfc6749_bodies,
    test_writes_require_idempotency_keys_and_replay_exactly,
)
from interop.test_import import (
    test_provenance_keeps_original_author_and_time_never_the_importer,
    test_rollback_hides_batch_and_retains_used_patients,
)
from interop.test_org_export import (
    test_export_needs_org_wide_authority_and_stops_once_it_is_lost,
    test_http_management_downloads_and_refuses_foreign_exports_alike,
)
from interop.test_posture import (
    test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows,
)
from interop.test_rnds_adapter import (
    test_non_consented_and_revoked_records_are_refused,
    test_unknown_foreign_and_unauthorized_records_are_denied_alike,
)
from interop.test_sources import (
    test_imported_documents_are_readable_only_through_clinical_scope,
)

CASES = {
    case.__name__: case
    for case in (
        test_commit_and_rollback_require_this_actors_fresh_step_up,
        test_database_refuses_illegal_edges_stale_sequences_and_history_edits,
        test_import_authority_follows_the_staff_management_bundle,
        test_import_authority_reads_metadata_but_never_clinical_payloads,
        test_imported_documents_are_readable_only_through_clinical_scope,
        test_org_admin_prepares_and_signs_off_but_cannot_commit_identity,
        test_provenance_keeps_original_author_and_time_never_the_importer,
        test_rollback_hides_batch_and_retains_used_patients,
        test_rollback_window_is_enforced_by_the_database,
        test_stored_source_reaches_only_the_pipeline_of_an_in_flight_job,
        test_unknown_and_foreign_jobs_share_one_denial,
        test_foreign_and_unknown_external_ids_return_the_identical_404,
        test_revocation_rotation_and_approver_authority_take_effect_immediately,
        test_scopes_bound_every_resource_and_malformed_tokens_are_identical,
        test_token_endpoint_refuses_with_rfc6749_bodies,
        test_writes_require_idempotency_keys_and_replay_exactly,
        test_export_needs_org_wide_authority_and_stops_once_it_is_lost,
        test_http_management_downloads_and_refuses_foreign_exports_alike,
        test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows,
        test_non_consented_and_revoked_records_are_refused,
        test_unknown_foreign_and_unauthorized_records_are_denied_alike,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.interop_allowed": (
        "test_import_authority_follows_the_staff_management_bundle",
        "test_unknown_and_foreign_jobs_share_one_denial",
    ),
    "clinic_app.interop_attachment": (
        "test_import_authority_reads_metadata_but_never_clinical_payloads",
        "test_imported_documents_are_readable_only_through_clinical_scope",
    ),
    "clinic_app.interop_attachment_checks": (
        "test_import_authority_reads_metadata_but_never_clinical_payloads",
    ),
    "clinic_app.interop_guard": (
        "test_commit_and_rollback_require_this_actors_fresh_step_up",
        "test_database_refuses_illegal_edges_stale_sequences_and_history_edits",
        "test_org_admin_prepares_and_signs_off_but_cannot_commit_identity",
        "test_rollback_window_is_enforced_by_the_database",
    ),
    "clinic_app.interop_hide_reversible": (
        "test_rollback_hides_batch_and_retains_used_patients",
    ),
    "clinic_app.interop_history": (
        "test_import_authority_reads_metadata_but_never_clinical_payloads",
        "test_provenance_keeps_original_author_and_time_never_the_importer",
    ),
    "clinic_app.interop_record_visible": (
        "test_imported_documents_are_readable_only_through_clinical_scope",
        "test_provenance_keeps_original_author_and_time_never_the_importer",
    ),
    "clinic_app.interop_source": (
        "test_import_authority_reads_metadata_but_never_clinical_payloads",
        "test_stored_source_reaches_only_the_pipeline_of_an_in_flight_job",
    ),
    "clinic_app.interop_authority": (
        "test_export_needs_org_wide_authority_and_stops_once_it_is_lost",
        "test_revocation_rotation_and_approver_authority_take_effect_immediately",
    ),
    "clinic_app.interop_manager": (
        "test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows",
    ),
    "clinic_app.interop_api_session": (
        "test_revocation_rotation_and_approver_authority_take_effect_immediately",
        "test_scopes_bound_every_resource_and_malformed_tokens_are_identical",
    ),
    "clinic_app.interop_api_token": (
        "test_revocation_rotation_and_approver_authority_take_effect_immediately",
        "test_token_endpoint_refuses_with_rfc6749_bodies",
    ),
    "clinic_app.interop_api_read": (
        "test_foreign_and_unknown_external_ids_return_the_identical_404",
    ),
    "clinic_app.interop_api_write": (
        "test_writes_require_idempotency_keys_and_replay_exactly",
    ),
    "clinic_app.interop_manage": (
        "test_export_needs_org_wide_authority_and_stops_once_it_is_lost",
        "test_http_management_downloads_and_refuses_foreign_exports_alike",
    ),
    "clinic_app.interop_export_rows": (
        "test_export_needs_org_wide_authority_and_stops_once_it_is_lost",
    ),
    "clinic_app.interop_rnds_request": (
        "test_unknown_foreign_and_unauthorized_records_are_denied_alike",
        "test_non_consented_and_revoked_records_are_refused",
    ),
}
