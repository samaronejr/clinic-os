"""The branch tripwire reuses shipped HTTP workflows and their real fixtures.

This corpus is not the reference set: view source derives that set mechanically,
and any still-unreached exit fails test_workspace_refusal_exits with file:line.
"""

import pytest

from renewal.test_amendments import test_http_finalize_step_up_amend_review_and_close
from renewal.test_attachments import (
    attachment_root,
    test_http_upload_scan_download_and_denials,
)
from renewal.test_billing_views import (
    _synthetic_pix,
    test_another_patients_charge_and_unreleased_charge_stay_hidden,
    test_expired_code_is_refused_and_regenerates_one_successor,
    test_patient_sees_own_instructions_and_receipt_only,
    test_refused_actions_keep_the_charge_and_never_claim_payment,
    test_staff_journey_creates_one_charge_and_one_receipt,
)
from renewal.test_clinic_settings import (
    test_settings_http_prg_csrf_unknown_fields_timezone_and_logo,
)
from renewal.test_clinical_history import test_http_save_stale_invalid_and_body_binding
from renewal.test_consent import (
    test_native_http_explicit_action_receipts_staff_and_cache,
)
from renewal.test_document_artifacts import test_http_document_forms_submit_as_rendered
from renewal.test_document_verification import (
    signed,
    test_http_verify_and_patient_download_journey,
)
from renewal.test_encounters import (
    test_http_explicit_save_reload_stale_invalid_and_failure,
)
from renewal.test_patient_access import (
    test_http_forged_context_and_swapped_ids_change_nothing,
    test_http_issue_redeem_view_and_logout_flow,
    test_http_patient_routes_fail_closed_without_a_session,
    test_http_redeem_failures_are_identical_and_non_enumerating,
)
from renewal.test_patient_contacts import (
    test_contacts_http_denies_foreign_clinic_and_bad_actions,
    test_contacts_http_flow_masks_edits_verifies_and_revokes,
)
from renewal.test_prescribing_workflow import (
    _synthetic_signing,
    test_completed_rehearsal_shows_synthetic_result_and_verification_evidence,
    test_delayed_callback_after_step_up_expiry_names_the_expired_authorization,
    test_provider_failure_is_terminal_and_restart_keeps_the_history,
    test_render_opens_the_review_and_a_repeat_render_reopens_the_same_document,
    test_review_is_issuer_only_and_names_a_moved_draft,
)
from renewal.test_prescription_drafts import (
    test_http_author_resume_errors_and_discard,
    test_http_real_database_failure_retains_unsaved_content,
)
from renewal.test_questionnaires import (
    test_patient_http_save_submit_errors_and_csrf,
    test_staff_http_totp_inspection_and_reopen,
)
from renewal.test_retention import (
    test_http_form_errors_and_revoke,
    test_http_release_export_and_patient_download,
    test_http_workspace_actions_and_denials,
    test_patient_records_gate_and_wrong_action,
)
from renewal.test_signatures import (
    test_http_failed_operation_recovers_through_rendered_controls,
    test_http_signing_flow_and_authenticated_callback,
)
from renewal.test_waitlist import (
    test_native_staff_and_patient_post_states,
    test_scope_denial_and_patient_http_recovery,
)

pytestmark = pytest.mark.django_db(transaction=True)

__all__ = [
    "_synthetic_pix",
    "_synthetic_signing",
    "attachment_root",
    "signed",
    "test_another_patients_charge_and_unreleased_charge_stay_hidden",
    "test_completed_rehearsal_shows_synthetic_result_and_verification_evidence",
    "test_contacts_http_denies_foreign_clinic_and_bad_actions",
    "test_contacts_http_flow_masks_edits_verifies_and_revokes",
    "test_delayed_callback_after_step_up_expiry_names_the_expired_authorization",
    "test_expired_code_is_refused_and_regenerates_one_successor",
    "test_http_author_resume_errors_and_discard",
    "test_http_document_forms_submit_as_rendered",
    "test_http_explicit_save_reload_stale_invalid_and_failure",
    "test_http_failed_operation_recovers_through_rendered_controls",
    "test_http_finalize_step_up_amend_review_and_close",
    "test_http_forged_context_and_swapped_ids_change_nothing",
    "test_http_form_errors_and_revoke",
    "test_http_issue_redeem_view_and_logout_flow",
    "test_http_patient_routes_fail_closed_without_a_session",
    "test_http_real_database_failure_retains_unsaved_content",
    "test_http_redeem_failures_are_identical_and_non_enumerating",
    "test_http_release_export_and_patient_download",
    "test_http_save_stale_invalid_and_body_binding",
    "test_http_signing_flow_and_authenticated_callback",
    "test_http_upload_scan_download_and_denials",
    "test_http_verify_and_patient_download_journey",
    "test_http_workspace_actions_and_denials",
    "test_native_http_explicit_action_receipts_staff_and_cache",
    "test_native_staff_and_patient_post_states",
    "test_patient_http_save_submit_errors_and_csrf",
    "test_patient_records_gate_and_wrong_action",
    "test_patient_sees_own_instructions_and_receipt_only",
    "test_provider_failure_is_terminal_and_restart_keeps_the_history",
    "test_refused_actions_keep_the_charge_and_never_claim_payment",
    "test_render_opens_the_review_and_a_repeat_render_reopens_the_same_document",
    "test_review_is_issuer_only_and_names_a_moved_draft",
    "test_scope_denial_and_patient_http_recovery",
    "test_settings_http_prg_csrf_unknown_fields_timezone_and_logo",
    "test_staff_http_totp_inspection_and_reopen",
    "test_staff_journey_creates_one_charge_and_one_receipt",
]
