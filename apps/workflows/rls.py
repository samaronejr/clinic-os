"""Exact workflow posture: no implicit machine access or runtime deletes."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "workflows_task",
        "workflows_taskcomment",
        "workflows_workflowdefinitionversion",
        "workflows_workflowrun",
        "workflows_workflowstep",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"}) for table in CUSTOM_RLS_TABLES
}
COLUMN_GRANTS: Final = frozenset(
    (table, column, "UPDATE")
    for table, columns in {
        "workflows_task": (
            "owner_user_id",
            "owner_role",
            "state",
            "completion_evidence",
            "revision",
            "last_command_key",
            "last_command_digest",
            "escalated_at",
            "updated_at",
        ),
        "workflows_workflowrun": ("state", "updated_at"),
        "workflows_workflowstep": (
            "state",
            "fencing_token",
            "claim_until",
            "wake_at",
            "operation_id",
            "created_task_id",
            "error_code",
            "updated_at",
        ),
    }.items()
    for column in columns
)
