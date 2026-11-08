"""Todo 50 resolver contract; append as one block to shared catalog registries."""

from typing import Final

TABLE_GRANTS: Final = {
    ("intake_datasubjectrequest", "SELECT"),
    ("intake_datasubjectrequest", "INSERT"),
}
COLUMN_GRANTS: Final = {
    *(
        ("intake_datasubjectrequest", column, "UPDATE")
        for column in (
            "state",
            "rejection_reason",
            "closed_at",
            "revision",
            "updated_by_id",
            "updated_at",
        )
    ),
    # The request's SLA task closes with the answer, never through staff UPDATE.
    *(
        ("workflows_task", column, "UPDATE")
        for column in ("state", "completion_evidence", "revision", "updated_at")
    ),
}
_RECEIPT = (
    "TABLE(request_id uuid, kind text, state text, revision integer, "
    "received_at timestamp with time zone, due_at timestamp with time zone, "
    "task_id uuid, delegate_acted boolean, changed boolean)"
)
_SCOPE = "TABLE(session_id uuid, organization_id uuid, clinic_id uuid)"
RESULTS: Final = {
    ("data_request_now", ""): "timestamp with time zone",
    (
        "data_request_withdrawn",
        "requested_session uuid, requested_enrollment uuid, requested_purpose text",
    ): "boolean",
    ("data_request_guard", ""): "trigger",
    ("data_request_task_valid", "row_data jsonb"): "boolean",
    ("data_request_task_close_valid", "old_row jsonb, new_row jsonb"): "boolean",
    ("data_request_close_task", ""): "trigger",
    ("data_request_emit_task", ""): "trigger",
    (
        "submit_data_request",
        "requested_clinic uuid, requested_kind text, requested_purpose text, "
        "details_envelope bytea, request_key uuid, request_fingerprints text[], "
        "withdrawn uuid[]",
    ): _RECEIPT,
    ("patient_data_requests", ""): (
        "TABLE(request_id uuid, kind text, purpose text, state text, "
        "rejection_reason text, received_at timestamp with time zone, "
        "due_at timestamp with time zone, closed_at timestamp with time zone, "
        "delegate_acted boolean)"
    ),
    (
        "transition_data_request",
        "requested_clinic uuid, requested_request uuid, expected_revision integer, "
        "next_state text, reason text",
    ): "TABLE(request_id uuid, state text, revision integer)",
    ("data_request_audit_scope", "record_id uuid, event_name text"): _SCOPE,
    ("patient_delegate_options", "kek text"): (
        "TABLE(grant_id uuid, patient_name character varying, "
        "expires_at timestamp with time zone, current boolean)"
    ),
    ("patient_channel_preferences", ""): (
        "TABLE(purpose text, channel text, opted_in boolean, verified boolean)"
    ),
    ("orders_patient_download_scope", "requested_version uuid"): _SCOPE,
}
_INTERNAL: Final = frozenset(
    {
        "data_request_withdrawn",
        "data_request_guard",
        "data_request_task_valid",
        "data_request_task_close_valid",
        "data_request_close_task",
        "data_request_emit_task",
    }
)
_AUDIT_SCOPES: Final = frozenset(
    {"data_request_audit_scope", "orders_patient_download_scope"}
)
_VOLATILE: Final = frozenset(
    {
        "data_request_guard",
        "data_request_close_task",
        "data_request_emit_task",
        "submit_data_request",
        "transition_data_request",
        "orders_patient_download_scope",
    }
)
POSTURE: Final = {
    name: (
        "v" if name in _VOLATILE else "s",
        name not in _INTERNAL | _AUDIT_SCOPES,
        ["clinic_resolver"]
        if name in _INTERNAL
        else ["clinic_owner", "clinic_resolver"]
        if name in _AUDIT_SCOPES
        else ["clinic_app", "clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
