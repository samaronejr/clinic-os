"""Independent exact catalog for transient retention; no SQL-source-derived sets."""

RESULTS = {
    (
        "retention_principal_scope",
        "requested_principal uuid, requested_clinic uuid",
    ): "uuid",
    ("retention_authorized", "clinic uuid"): "boolean",
    ("retention_sweep_scopes", ""): "TABLE(principal_id uuid, clinic_id uuid)",
    (
        "retention_related_hold",
        "target_class text, target_id uuid, held_class text, held_id uuid",
    ): "boolean",
    ("retention_held", "clinic uuid, record_class text, record_id uuid"): "boolean",
    ("retention_due", "clinic uuid, record_class text, record_id uuid"): (
        "timestamp with time zone"
    ),
    ("retention_purge_visible", "requested uuid"): "boolean",
    ("retention_hold_fence", ""): "trigger",
    ("retention_mutation_allowed", "target_class text, target_id uuid"): "boolean",
    ("retention_shred", "requested uuid"): "void",
    ("retention_purge_guard", ""): "trigger",
    ("retention_schedule", "clinic uuid, record_class text"): (
        "TABLE(dry_run boolean, due bigint, blocked bigint, principal_id uuid, "
        "purge_id uuid, event_state text)"
    ),
    ("retention_prepare", "clinic uuid, requested uuid"): (
        "TABLE(object_keys text[], backup_expires timestamp with time zone, "
        "event_state text, storage_locations text[])"
    ),
    ("retention_confirm", "clinic uuid, requested uuid, object_key text"): "void",
    ("retention_complete", "clinic uuid, requested uuid"): "void",
    ("retention_system_task_valid", "row_data jsonb"): "boolean",
    ("retention_emit_task", ""): "trigger",
    ("retention_failure", "clinic uuid, requested uuid, reason text"): "void",
    ("retention_audit_scope", "record_id uuid, event_name text"): (
        "TABLE(organization_id uuid, clinic_id uuid, principal_id uuid, "
        "record_type text)"
    ),
    ("retention_restore_tombstones", "current_tombstones jsonb"): "jsonb",
}
AGENT_SIGNATURES = {
    signature
    for signature in RESULTS
    if signature[0]
    in {
        "retention_schedule",
        "retention_prepare",
        "retention_confirm",
        "retention_complete",
        "retention_failure",
        "retention_audit_scope",
        "retention_principal_scope",
        "retention_sweep_scopes",
    }
} | {
    (
        "retention_audit",
        "record_id uuid, event_name text, occurred_at_utc timestamp with time zone, "
        "content_hash bytea",
    )
}
POSTURE = {
    name: (
        "s" if name == "retention_related_hold" else "v",
        name == "retention_purge_visible",
        ["clinic_app", "clinic_owner", "clinic_resolver"]
        if name == "retention_purge_visible"
        else ["clinic_owner", "clinic_resolver"]
        if name == "retention_restore_tombstones"
        else ["clinic_agent", "clinic_owner", "clinic_resolver"]
        if name == "retention_audit_scope"
        else ["clinic_agent", "clinic_resolver"]
        if any(signature[0] == name for signature in AGENT_SIGNATURES)
        else ["clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
TABLE_GRANTS = {
    ("retention_artifactpurge", "SELECT"),
    ("retention_artifactpurge", "INSERT"),
    ("retention_artifactpurge", "UPDATE"),
    ("workflows_task", "INSERT"),
}
COLUMN_GRANTS = {
    ("scribe_audiochunk", column, "UPDATE")
    for column in ("state", "wrapped_key", "encrypted_data")
} | {("ai_aiartifact", column, "UPDATE") for column in ("content", "purged_at")}
