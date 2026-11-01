"""Bind catalog-discovered extraction protocols to real human authority tests."""

from ai import test_extraction_filing, test_extraction_runtime

CASES = {
    case.__name__: case
    for case in (
        test_extraction_runtime.test_five_confirmations_import_exact_values_without_acknowledgment,
        test_extraction_runtime.test_extraction_sql_rechecks_inactive_and_foreign_actor,
        test_extraction_runtime.test_native_extraction_reviews_exact_regions_and_refuses_invalid_commands,
        test_extraction_filing.test_attachment_requires_scan_and_independent_exact_byte_identity_match,
        test_extraction_filing.test_resolver_rejects_another_current_version_even_with_identical_bytes,
        test_extraction_filing.test_resolver_expiry_uses_database_time_at_the_exact_deadline,
        test_extraction_filing.test_resolver_refuses_filing_before_writing_into_an_acknowledged_version,
    )
}
SQL_PROTOCOL_ORACLES = {
    name: list(CASES)
    for name in (
        "clinic_app.ai_extraction_guard",
        "clinic_app.ai_extraction_job_allowed",
        "clinic_app.ai_extraction_source_allowed",
        "clinic_app.ai_imported_observation",
        "clinic_app.ai_review_extraction",
    )
}
ACTOR_PROTOCOL_ORACLES = dict.fromkeys(
    (
        "clinic_app.ai_extraction_guard()",
        "clinic_app.ai_imported_observation(uuid,uuid,uuid)",
        "clinic_app.ai_review_extraction(uuid,uuid,text,bytea,text)",
    ),
    (
        "ai.test_extraction_runtime."
        "test_extraction_sql_rechecks_inactive_and_foreign_actor",
    ),
)
