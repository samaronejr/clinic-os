"""Todo 31 resolver contract; append as one block to shared catalog registries."""

from typing import Final

TABLE_GRANTS: Final = {
    (table, "SELECT")
    for table in (
        "orders_labaccount",
        "orders_order",
        "orders_orderitem",
        "orders_orderevent",
        "orders_result",
        "orders_resultversion",
        "orders_resultobservation",
        "orders_resultmatch",
        "orders_resultquarantine",
        "orders_resultrejection",
        "orders_resultacknowledgment",
        "orders_resultrelease",
        "orders_resultworkitem",
    )
} | {
    ("orders_resultmatch", "INSERT"),
    ("orders_resultquarantine", "INSERT"),
    ("orders_resultworkitem", "INSERT"),
    ("workflows_task", "INSERT"),
}
RESULTS: Final = {
    ("orders_lab_account", "clinic uuid"): "uuid",
    ("orders_resolve_lab", "account uuid, proof text"): (
        "TABLE(organization_id uuid, clinic_id uuid, principal_id uuid)"
    ),
    ("orders_ingest_allowed", "clinic uuid, requested_order uuid"): "boolean",
    ("orders_is_current", "version uuid"): "boolean",
    ("orders_can_read_version", "version uuid"): "boolean",
    (
        "orders_ingest_scope",
        "clinic uuid, requested_order uuid, result uuid, predecessor uuid",
    ): ("TABLE(organization_id uuid, lab_account_id uuid, number integer)"),
    ("orders_event_receipt", "clinic uuid, event uuid"): (
        "TABLE(result_id uuid, version_id uuid, number integer, "
        "state text, fingerprint text)"
    ),
    ("orders_delegate_allowed", "version uuid, grant_id uuid"): "boolean",
    ("orders_correlated_order", "account uuid, correlation uuid"): "uuid",
    ("orders_guard", ""): "trigger",
    ("orders_system_task_valid", "row_data jsonb"): "boolean",
    ("orders_emit_task", ""): "trigger",
    (
        "orders_verify_identity",
        "clinic uuid, version uuid, requested_order uuid, kek text",
    ): ("TABLE(state text, created boolean, task_id uuid)"),
    ("orders_audit_scope", "record_id uuid, event_name text"): (
        "TABLE(principal_id uuid, organization_id uuid, clinic_id uuid)"
    ),
    ("orders_patient_sources", "requested_version uuid"): (
        "TABLE(version_id uuid, number integer, created_at timestamp with time zone, "
        "storage_key text, sha256 text, size_bytes integer, content_type text)"
    ),
}
POSTURE: Final = {
    name: (
        "s" if name in {"orders_is_current", "orders_system_task_valid"} else "v",
        name not in {"orders_guard", "orders_emit_task", "orders_system_task_valid"},
        ["clinic_app", "clinic_owner", "clinic_resolver"]
        if name == "orders_audit_scope"
        else ["clinic_resolver"]
        if name in {"orders_guard", "orders_emit_task", "orders_system_task_valid"}
        else ["clinic_app", "clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
