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
from interop.test_import import (
    test_provenance_keeps_original_author_and_time_never_the_importer,
    test_rollback_hides_batch_and_retains_used_patients,
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
}
