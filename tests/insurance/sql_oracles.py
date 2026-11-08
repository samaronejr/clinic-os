"""Executable native witnesses for insurance-owned SQL protocol roots."""

from insurance import test_coverage_receipts as coverage
from insurance import test_registered_booking as booking
from insurance import test_revenue_cycle as revenue

CASES = {
    case.__name__: case
    for case in (
        booking.test_payer_refusal_and_unknown_cannot_be_confirmed_or_forge_authorization,
        booking.test_affirmative_checks_bind_the_real_appointment_before_confirmation,
        booking.test_visible_foreign_payer_and_membership_cannot_be_used_in_booking,
        booking.test_existing_registration_is_linked_once_without_rewriting_ciphertext,
        booking.test_unmatched_memberships_stay_null_and_are_reported,
        booking.test_old_authorization_replay_does_not_replace_a_later_refusal,
        coverage.test_issued_receipt_cannot_book_after_coverage_changes,
        coverage.test_database_rejects_an_insured_booking_without_a_receipt,
        coverage.test_failed_validation_keeps_sensitive_values_out_of_owned_outputs,
        coverage.test_runtime_cannot_forge_a_matching_coverage_receipt,
        revenue.test_sealed_bytes_and_history_cannot_be_mutated_or_deleted,
        revenue.test_non_clinician_cannot_approve_new_appeal_assertions,
        revenue.test_v2_subtraction_invalidates_retained_clinical_approval,
        revenue.test_rebilling_requires_a_retained_successor_not_a_second_claim_root,
    )
}

SQL_PROTOCOL_ORACLES = {
    "clinic_app.insurance_appointment_guard": [
        "test_payer_refusal_and_unknown_cannot_be_confirmed_or_forge_authorization",
        "test_affirmative_checks_bind_the_real_appointment_before_confirmation",
        "test_issued_receipt_cannot_book_after_coverage_changes",
        "test_database_rejects_an_insured_booking_without_a_receipt",
    ],
    "clinic_app.insurance_booking_coverage": [
        "test_visible_foreign_payer_and_membership_cannot_be_used_in_booking",
    ],
    "clinic_app.insurance_record_booking_coverage": [
        "test_failed_validation_keeps_sensitive_values_out_of_owned_outputs",
        "test_visible_foreign_payer_and_membership_cannot_be_used_in_booking",
        "test_runtime_cannot_forge_a_matching_coverage_receipt",
    ],
    "clinic_app.insurance_guard": [
        "test_sealed_bytes_and_history_cannot_be_mutated_or_deleted",
        "test_non_clinician_cannot_approve_new_appeal_assertions",
        "test_v2_subtraction_invalidates_retained_clinical_approval",
    ],
    "clinic_app.insurance_guide_source": [
        "test_rebilling_requires_a_retained_successor_not_a_second_claim_root",
    ],
    "clinic_app.insurance_link_registered_payer": [
        "test_existing_registration_is_linked_once_without_rewriting_ciphertext",
        "test_unmatched_memberships_stay_null_and_are_reported",
    ],
    "clinic_app.insurance_payer_check_applied": [
        "test_old_authorization_replay_does_not_replace_a_later_refusal",
        "test_affirmative_checks_bind_the_real_appointment_before_confirmation",
    ],
    "clinic_app.insurance_registration_guard": [
        "test_visible_foreign_payer_and_membership_cannot_be_used_in_booking",
    ],
    "clinic_app.intake_membership_payer_guard": [
        "test_existing_registration_is_linked_once_without_rewriting_ciphertext",
    ],
}
