"""Executable oracle bindings and reviewed labels for discovered PSP guards."""

from billing import (
    test_psp_inactive_actor,
    test_psp_outbox,
    test_psp_patient,
    test_psp_posture,
    test_psp_refunds,
    test_psp_review,
)

ACTOR_PROTOCOL_ORACLES = dict.fromkeys(
    (
        "clinic_app.billing_psp_access(uuid)",
        "clinic_app.billing_psp_guard()",
        "clinic_app.billing_psp_dispatchable(uuid,boolean)",
    ),
    ("billing.test_psp_inactive_actor.test_psp_sql_requires_current_active_actor",),
)

CASES = {
    case.__name__: case
    for case in (
        test_psp_inactive_actor.test_psp_sql_requires_current_active_actor,
        test_psp_outbox.test_remote_timeout_keeps_in_progress_and_redelivery_uses_lookup_only,
        test_psp_patient.test_survivor_sees_psp_instructions_until_unmerge_without_reparenting_money,
        test_psp_posture.test_no_context_or_foreign_organization_sees_psp_intents,
        test_psp_refunds.test_replayed_refund_callback_requires_finance_and_reverses_repasse_once,
        test_psp_review.test_financial_read_authority_cannot_fabricate_another_actors_evidence,
        test_psp_review.test_corrected_statement_at_the_same_provider_version_can_post,
        test_psp_review.test_newer_authoritative_reversal_still_blocks_prior_verified_fact,
        test_psp_review.test_lookup_only_recovery_retains_original_approved_version_after_rotation,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.billing_psp_access": [
        "test_psp_sql_requires_current_active_actor",
        "test_no_context_or_foreign_organization_sees_psp_intents",
        "test_financial_read_authority_cannot_fabricate_another_actors_evidence",
    ],
    "clinic_app.billing_psp_dispatchable": [
        "test_psp_sql_requires_current_active_actor",
        "test_remote_timeout_keeps_in_progress_and_redelivery_uses_lookup_only",
        "test_lookup_only_recovery_retains_original_approved_version_after_rotation",
    ],
    "clinic_app.billing_psp_guard": sorted(CASES),
}
OBSERVED = (
    "apps.billing.psp_presentation.patient_psp_payment",
    "apps.billing.psp_worker.PspSendAdapter.send",
    "apps.billing.psp_worker._eligible",
)
DELEGATED = (
    "apps.billing.psp_posting.apply_psp_evidence",
    "apps.billing.psp_services.prepare_psp_charge",
    "apps.billing.psp_services.prepare_psp_outgoing",
)
PROVIDER = (
    "apps.billing.psp_gate.require_psp_rehearsal",
    "apps.billing.psp_gate.require_psp_sandbox",
    "apps.billing.psp_storage.DatabasePaymentEventStore.resolve",
    "apps.billing.psp_storage.record_status",
    "apps.billing.psp_storage.resolve_intent",
)
