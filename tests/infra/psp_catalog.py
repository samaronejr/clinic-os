"""Independent exact PSP resolver signatures and least-privilege catalog."""

from typing import Final

TABLES: Final = {
    "billing_psppolicy",
    "billing_pspintent",
    "billing_pspbinding",
    "billing_pspevidence",
    "billing_pspreceipt",
}
TABLE_GRANTS: Final = {(table, "SELECT") for table in TABLES}
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
}
POSTURE: Final = {
    name: (
        "s" if name == "billing_patient_psp_instructions" else "v",
        name != "billing_psp_guard",
        ["clinic_resolver"]
        if name == "billing_psp_guard"
        else ["clinic_app", "clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
