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
        "workflows_careplan",
        "workflows_careaction",
        "workflows_referral",
        "workflows_recall",
        "workflows_proassignment",
        "workflows_prooccurrence",
        "workflows_carerefusal",
        "workflows_releasenoticetemplate",
        "workflows_releasedelivery",
        "workflows_urgentcoveragepolicy",
        "workflows_urgentconcern",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
# Scanner refusals are written by their resolver only; staff read them. Urgent
# concerns are written by the patient bridge only; staff move their state.
READ_ONLY_TABLES: Final = frozenset(
    {"workflows_carerefusal", "workflows_urgentconcern"}
)
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT"} if table in READ_ONLY_TABLES else {"SELECT", "INSERT"})
    for table in CUSTOM_RLS_TABLES
}
_COMMAND: Final = (
    "revision",
    "last_command_key",
    "last_command_digest",
    "updated_at",
)
_COMMAND_STATE: Final = ("state", *_COMMAND)
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
        "workflows_workflowdefinitionversion": (
            "state",
            "steps",
            "fingerprint",
            "validated_at",
            "published_by_id",
            "published_at",
            "retired_at",
            "revision",
            "last_command_key",
            "last_command_digest",
        ),
        "workflows_workflowrun": (
            "state",
            "revision",
            "last_command_key",
            "last_command_digest",
            "updated_at",
        ),
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
        "workflows_careplan": _COMMAND_STATE,
        "workflows_proassignment": _COMMAND_STATE,
        "workflows_careaction": (
            "owner_user_id",
            "owner_role",
            "state",
            "review_state",
            *_COMMAND,
        ),
        "workflows_referral": ("status", "document_id", *_COMMAND),
        "workflows_recall": ("state", "task_id", *_COMMAND),
        "workflows_prooccurrence": (
            "state",
            "answers",
            "concerning",
            "response_task_id",
            "recorded_at",
            *_COMMAND,
        ),
        "workflows_urgentconcern": (
            "state",
            "acknowledged_by_id",
            "acknowledged_at",
            "resolved_by_id",
            "resolved_at",
            "revision",
        ),
    }.items()
    for column in columns
)
