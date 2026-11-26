"""Independent exact PSP resolver signatures and least-privilege catalog."""

from typing import Final

TABLES: Final = {
    "billing_psppolicy",
    "billing_pspintent",
    "billing_pspbinding",
    "billing_pspevidence",
    "billing_pspreceipt",
}
TABLE_GRANTS: Final = {(table, "SELECT") for table in TABLES} | {
    ("billing_pspevidence", "INSERT"),
    ("billing_pspbinding", "INSERT"),
}
RESULTS: Final = {
    ("billing_psp_access", "requested_intent uuid"): "boolean",
    (
        "billing_psp_scope",
        "requested_provider text, reference text",
    ): "TABLE(intent_id uuid, organization_id uuid, clinic_id uuid, actor_id uuid)",
    (
        "billing_psp_intent_scope",
        "requested_intent uuid",
    ): "TABLE(intent_id uuid, organization_id uuid, clinic_id uuid, actor_id uuid)",
    (
        "billing_psp_seen",
        "requested_provider text, requested_source text, requested_event text",
    ): "boolean",
    (
        "billing_psp_dispatchable",
        "requested_intent uuid, fresh_session boolean",
    ): "boolean",
    (
        "billing_patient_psp_instructions",
        "requested_invoice uuid",
    ): "TABLE(payment_code bytea, payment_url bytea, installments smallint, "
    "amount_minor bigint, receipt_reference uuid, "
    "receipt_issued_at timestamp with time zone)",
    ("billing_psp_guard", ""): "trigger",
    ("billing_psp_dispatched", "requested uuid"): "uuid",
    (
        "billing_psp_dispatch_state",
        "requested uuid, caller_clinic uuid, proof bytea",
    ): "boolean",
    ("billing_psp_observer_target", "requested uuid"): "uuid",
    (
        "billing_psp_observer_request",
        "requested uuid, proof bytea",
    ): "SETOF billing_pspintent",
    (
        "billing_psp_observer_record",
        "requested uuid, proof bytea, reference text, fact jsonb, code bytea, "
        "url bytea, occurred timestamp with time zone, content_hash bytea",
    ): "text",
    (
        "billing_psp_observer_finish",
        "requested uuid, operation uuid, proof bytea, reference text, "
        "observation text, "
        "occurred timestamp with time zone, content_hash bytea",
    ): "boolean",
    (
        "billing_psp_observed",
        "requested uuid, observed_status text, observed_version bigint, "
        "observed_amount bigint, observed_currency text, observed_fee bigint, "
        "proof bytea",
    ): "boolean",
}
_PRIVATE: Final = {
    "billing_psp_guard",
    "billing_psp_observer_target",
    "billing_psp_dispatched",
}
POSTURE: Final = {
    name: (
        "s" if name == "billing_patient_psp_instructions" else "v",
        name not in _PRIVATE,
        ["clinic_resolver"] if name in _PRIVATE else ["clinic_app", "clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
