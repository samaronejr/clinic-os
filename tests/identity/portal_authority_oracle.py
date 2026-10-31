"""Execute the todo 50 portal, messages and data-subject request witnesses."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from comms import test_portal_messages
from orders import test_patient_downloads
from patients import test_dsar, test_portal_reads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_portal_census(
    proofs: ExecutedProofs, graph: Callable[[], RbacGraph], root: Path
) -> None:
    """Require patient, delegate and privacy-staff boundary entry with oracles.

    The provider-registry sharing test stays out: it activates a global
    capability. The patient page oracle executes the same sharing projection.
    """
    for oracle in (
        test_dsar.test_every_kind_is_received_with_database_due_date_task_and_idempotent_replay,
        test_dsar.test_scope_selectors_fail_identically_and_revoked_sessions_cannot_submit,
        test_dsar.test_access_and_portability_reuse_the_released_record_export,
        test_dsar.test_delegate_export_excludes_restricted_records_and_foreign_requests,
        test_dsar.test_delegate_operations_and_purposes_bound_their_requests,
        test_dsar.test_objection_withdraws_only_the_named_accepted_purpose,
        test_dsar.test_staff_lifecycle_is_exact_closes_the_task_and_never_edits_records,
        test_dsar.test_only_management_roles_answer_and_other_roles_match_unknown_requests,
        test_dsar.test_other_tenants_and_other_sessions_see_no_requests,
        test_dsar.test_patient_page_submits_in_the_body_and_refuses_without_side_effects,
        test_portal_reads.test_delegate_options_list_only_active_verified_grants_of_the_original_identity,
        test_portal_reads.test_display_choices_stay_in_the_server_session_and_refusals_change_nothing,
        test_portal_reads.test_home_offers_the_switcher_only_for_verified_active_grants,
        test_portal_reads.test_staff_queue_admits_privacy_staff_and_hides_requests_from_others,
        test_portal_messages.test_patient_reads_accepted_clinic_messages_and_replies_once,
        test_portal_messages.test_a_closed_thread_stays_readable_and_refuses_a_new_reply,
        test_portal_messages.test_other_patients_other_clinics_and_unknown_threads_share_one_denial,
        test_portal_messages.test_a_verified_delegate_with_message_scope_reads_and_replies_for_the_patient,
        test_portal_messages.test_messages_page_refusals_are_one_gate_and_a_closed_thread_keeps_the_text,
    ):
        proofs.run(oracle.__name__, partial(oracle, graph(), root))
    for simple_oracle in (
        test_portal_reads.test_channel_preferences_summarize_channels_without_destinations,
        test_patient_downloads.test_released_source_downloads_exact_bytes_with_one_session_bound_event,
        test_patient_downloads.test_unreleased_unknown_and_staff_contexts_are_one_denial,
    ):
        proofs.run(simple_oracle.__name__, partial(simple_oracle, graph()))
